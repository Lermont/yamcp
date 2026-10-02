"""Offline catalog search and consultative order goals contracts."""
import asyncio
from copy import deepcopy

import pytest

from test_products import ProductAPI, snapshots, source
from yadirect_mcp import audit, bundle, executor


def catalog_source():
    raw = source()
    raw["policy_name"] = "ecommerce_new_v4"
    raw["allow_unverified_goals"] = False
    raw["goal_catalog_campaign_id"] = 55
    raw["profile_context"]["goals"][0]["kind"] = "order"
    raw["profile_context"]["conversion_goal_reason"] = "Заказ оборудования после консультации"
    raw["channels"]["product"]["strategy"] = {
        "type": "maximum_conversion_rate", "goal_id": 77, "placements": "search_only",
    }
    return raw


def test_catalog_search_exact_placements_and_goals():
    raw = catalog_source()
    plan = bundle.compile_bundle(raw, "client")
    executor.validate_plan(plan)
    product = next(c for c in plan["campaigns"] if c["channel"] == "product")
    strategy = product["campaign"]["UnifiedCampaign"]["BiddingStrategy"]
    assert strategy["Network"] == {
        "BiddingStrategyType": "SERVING_OFF", "PlacementTypes": {"Network": "NO", "Maps": "NO"},
    }
    assert strategy["Search"]["PlacementTypes"] == {
        "SearchResults": "YES", "ProductGallery": "YES", "DynamicPlaces": "YES",
        "Maps": "NO", "SearchOrganizationList": "NO",
    }
    assert strategy["Search"]["WbMaximumConversionRate"]["GoalId"] == 77
    assert audit.classify_channel({"bidding_strategy": strategy})["channel"] == "product"
    changed = deepcopy(raw)
    changed["channels"]["product"]["strategy"].pop("placements")
    assert bundle.compile_bundle(changed, "client")["plan_hash"] != plan["plan_hash"]


@pytest.mark.parametrize("kind", ["cart", "phone_click", "unknown"])
def test_catalog_goal_exception_does_not_admit_microconversions(kind):
    raw = catalog_source()
    raw["profile_context"]["goals"][0]["kind"] = kind
    with pytest.raises(ValueError, match="goals.kind"):
        bundle.compile_bundle(raw, "client")


@pytest.mark.parametrize("version", ["v1", "v2", "v3"])
def test_consultative_exception_cannot_change_older_profiles(version):
    raw = catalog_source()
    raw["policy_name"] = f"ecommerce_new_{version}"
    with pytest.raises(ValueError, match="conversion_goal_reason"):
        bundle.compile_bundle(raw, "client")


def test_consultative_goals_require_reason():
    raw = catalog_source()
    raw["profile_context"].pop("conversion_goal_reason")
    with pytest.raises(ValueError, match="goals.kind"):
        bundle.compile_bundle(raw, "client")


def test_placement_choice_is_product_only():
    raw = catalog_source()
    raw["channels"]["search"]["strategy"] = {
        "type": "maximum_conversion_rate", "goal_id": 77, "placements": "search_only",
    }
    with pytest.raises(ValueError, match="placements"):
        bundle.compile_bundle(raw, "client")


def test_search_catalog_readback_rejects_network_drift():
    plan = bundle.compile_bundle(catalog_source(), "client")
    execution = asyncio.run(executor.apply(ProductAPI(), plan))
    settings, groups, actual, keys = snapshots(plan, execution)
    assert executor.compare_readback(plan, execution, settings, groups, actual, keys)["verified"]
    settings["campaigns"][-1]["bidding_strategy"]["Network"] = {
        "BiddingStrategyType": "NETWORK_DEFAULT",
        "PlacementTypes": {"Network": "YES", "Maps": "NO"},
    }
    check = executor.compare_readback(plan, execution, settings, groups, actual, keys)
    assert not check["verified"]


def test_auxiliary_cart_lower_value():
    raw = catalog_source()
    raw["priority_goals"].append({"goal_id": 88, "value": 100})
    raw["profile_context"]["goals"].append({"goal_id": 88, "kind": "cart"})
    plan = bundle.compile_bundle(raw, "client")
    executor.validate_plan(plan)
    raw["priority_goals"][-1]["value"] = 1500
    with pytest.raises(ValueError, match="меньшую ценность"):
        bundle.compile_bundle(raw, "client")


def test_removed_geo_switch_is_rejected_in_new_plans():
    raw = catalog_source()
    raw["settings"] = {"ENABLE_AREA_OF_INTEREST_TARGETING": False}
    with pytest.raises(ValueError, match="ENABLE_AREA_OF_INTEREST_TARGETING"):
        bundle.compile_bundle(raw, "client")


def test_product_search_and_network_share_one_budget_and_verify():
    raw = catalog_source()
    raw["channels"]["product"]["strategy"]["placements"] = "search_and_network"
    plan = bundle.compile_bundle(raw, "client")
    executor.validate_plan(plan)
    product = next(c for c in plan["campaigns"] if c["channel"] == "product")
    strategy = product["campaign"]["UnifiedCampaign"]["BiddingStrategy"]
    assert strategy["Network"] == {
        "BiddingStrategyType": "NETWORK_DEFAULT",
        "PlacementTypes": {"Network": "YES", "Maps": "NO"},
    }
    execution = asyncio.run(executor.apply(ProductAPI(), plan))
    settings, groups, actual, keys = snapshots(plan, execution)
    assert executor.compare_readback(plan, execution, settings, groups, actual, keys)["verified"]
    settings["campaigns"][-1]["bidding_strategy"]["Network"].pop("PlacementTypes")
    assert executor.compare_readback(plan, execution, settings, groups, actual, keys)["verified"]
    settings["campaigns"][-1]["bidding_strategy"]["Network"]["PlacementTypes"] = {
        "Network": "YES", "Maps": "YES"}
    check = executor.compare_readback(plan, execution, settings, groups, actual, keys)
    assert not check["verified"]
