"""Maximum clicks may omit Metrika; conversion and selected-goal checks remain strict."""

from __future__ import annotations

import asyncio

import pytest

from test_bundle import compact_geo_bundle, source_bundle
from test_campaign_setup import plan as raw_plan
from test_executor import FakeApi, readback_payloads
from test_policy_audit import campaign, findings
from yadirect_mcp import audit, bundle, campaign_setup, executor, landing, policy, regions


def without_metrika(source=None):
    source = source_bundle() if source is None else source
    source.pop("counter_ids")
    source.pop("priority_goals")
    source.pop("allow_unverified_goals")
    source["manual_checks"].pop("goals_reviewed")
    return source


@pytest.mark.parametrize("empty_arrays", [False, True])
@pytest.mark.parametrize("strategy", [
    None, "maximum_clicks", "WB_MAXIMUM_CLICKS", {},
    {"type": "maximum_clicks", "bid_ceiling": 80},
])
def test_clicks_compile_without_metrika_or_goal_review(empty_arrays, strategy):
    source = without_metrika()
    if empty_arrays:
        source.update(counter_ids=[], priority_goals=[])
    for channel in source["channels"].values():
        channel["strategy"] = strategy
    plan = bundle.compile_bundle(source, "client")
    assert plan["ready"] is True
    assert plan["allow_unverified_goals"] is False
    assert "goals_reviewed" not in plan["manual_checks"]
    assert plan["summary"]["weekly_budget_total"] == 7000
    for item in plan["campaigns"]:
        unified = item["campaign"]["UnifiedCampaign"]
        assert "CounterIds" not in unified
        assert "PriorityGoals" not in unified
        active = unified["BiddingStrategy"][item["channel"].title()]
        assert active["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"
        assert "GoalId" not in active["WbMaximumClicks"]


def test_clicks_variants_without_metrika_keep_individual_budgets():
    plan = bundle.compile_bundle(without_metrika(compact_geo_bundle()), "client")
    assert plan["ready"] is True
    assert plan["summary"]["weekly_budget_total"] == 20000
    assert all("CounterIds" not in item["campaign"]["UnifiedCampaign"]
               for item in plan["campaigns"])


def test_conversion_variant_cannot_use_the_clicks_exception():
    source = without_metrika(compact_geo_bundle())
    source["campaign_variants"][1]["strategy"] = {
        "type": "maximum_conversion_rate", "goal_id": 77,
    }
    with pytest.raises(ValueError, match="priority_goals"):
        bundle.compile_bundle(source, "client")


def test_clicks_with_counter_without_goals_is_supported():
    source = without_metrika()
    source["counter_ids"] = [12345]
    plan = bundle.compile_bundle(source, "client")
    assert plan["ready"] is True
    for item in plan["campaigns"]:
        unified = item["campaign"]["UnifiedCampaign"]
        assert unified["CounterIds"] == {"Items": [12345]}
        assert "PriorityGoals" not in unified


@pytest.mark.parametrize("channel", ["search", "network"])
@pytest.mark.parametrize("missing", ["counter_ids", "priority_goals"])
def test_conversion_in_mixed_plan_still_requires_counter_and_goal(channel, missing):
    source = source_bundle()
    source.pop(missing)
    source["channels"][channel]["strategy"] = {
        "type": "maximum_conversion_rate", "goal_id": 77,
    }
    with pytest.raises(ValueError, match=missing):
        bundle.compile_bundle(source, "client")


def test_clicks_with_selected_goals_still_requires_counter_and_review():
    source = source_bundle()
    source.pop("counter_ids")
    with pytest.raises(ValueError, match="counter_ids"):
        bundle.compile_bundle(source, "client")
    source["counter_ids"] = [12345]
    source["manual_checks"].pop("goals_reviewed")
    with pytest.raises(ValueError, match="goals_reviewed"):
        bundle.compile_bundle(source, "client")


@pytest.mark.parametrize("field, invalid", [
    ("counter_ids", [0]), ("counter_ids", [-1]), ("counter_ids", None),
    ("counter_ids", "12345"), ("priority_goals", None),
    ("priority_goals", {}), ("priority_goals", ""), ("priority_goals", 0),
])
def test_optional_metrika_fields_still_validate_provided_values(field, invalid):
    source = without_metrika()
    source[field] = invalid
    with pytest.raises(ValueError, match=field):
        bundle.compile_bundle(source, "client")


@pytest.fixture
def checked_links(monkeypatch):
    async def checked(pages, **kwargs):
        return [{"url": page["url"], "ok": True, "status_code": 200} for page in pages]
    monkeypatch.setattr(landing, "inspect_pages", checked)
    regions.reset_cache()


@pytest.mark.parametrize("counter_ids", [[], [12345]])
def test_preflight_without_goals_needs_no_catalog_or_override(checked_links, counter_ids):
    source = without_metrika()
    source["counter_ids"] = counter_ids
    plan = bundle.compile_bundle(source, "client")
    api = FakeApi(existing=[{
        "Id": 1, "Name": "Existing", "State": "OFF",
        "UnifiedCampaign": {"CounterIds": {"Items": [12345]}},
    }])
    result = asyncio.run(executor.preflight(api, plan))
    assert result["status"] == "PASS"
    assert result["goals"]["status"] == "PASS"
    assert result["goals"]["goal_ids"] == []
    assert not any(call[0] == "v4" for call in api.calls)
    assert all(call[2] == "get" for call in api.calls)


def test_clicks_with_selected_goals_still_requires_catalog_or_override(checked_links):
    source = source_bundle()
    source.pop("allow_unverified_goals")
    plan = bundle.compile_bundle(source, "client")
    with pytest.raises(ValueError, match="Нельзя проверить приоритетные цели"):
        asyncio.run(executor.preflight(FakeApi(), plan))


def test_fake_creation_and_readback_without_metrika_preserve_empty_selection():
    plan = bundle.compile_bundle(without_metrika(), "client")
    api = FakeApi()
    execution = asyncio.run(executor.apply(api, plan))
    assert execution["status"] == "complete"
    assert execution["activated"] is False
    assert all(call[2] == "add" for call in api.calls)
    payloads = readback_payloads(plan, execution)
    result = executor.compare_readback(plan, execution, *payloads)
    assert result["verified"] is True
    assert result["summary"]["BLOCK"] == 0
    payloads[0]["campaigns"][0]["counter_ids"] = [999]
    payloads[0]["campaigns"][0]["priority_goals"] = [{"GoalId": 12, "Value": 1000000}]
    changed = executor.compare_readback(plan, execution, *payloads)
    blocked = {row["rule"] for row in changed["findings"] if row["status"] == "BLOCK"}
    assert {"readback.counters", "readback.priority_goals"} <= blocked


@pytest.mark.parametrize("counter_ids", [[], [123]])
def test_audit_does_not_block_clicks_without_goals(counter_ids):
    result = audit.audit_campaign(
        campaign(counter_ids=counter_ids, priority_goals=[]), selected_policy=policy.get(),
    )
    finding = findings(result)["goals.explicit_business_selection"]
    assert finding["status"] == "PASS"
    assert "необязательны" in finding["message"]


@pytest.mark.parametrize("search, network", [
    ("WB_MAXIMUM_CONVERSION_RATE", "SERVING_OFF"),
    ("WB_MAXIMUM_CLICKS", "WB_MAXIMUM_CONVERSION_RATE"),
    ("WB_MAXIMUM_CLICKS", "NETWORK_DEFAULT"),
    ("SERVING_OFF", "SERVING_OFF"),
    ("WB_MAXIMUM_CLICKS", None),
])
def test_audit_never_assumes_unknown_or_conversion_strategy_is_clicks(search, network):
    item = campaign(counter_ids=[], priority_goals=[])
    item["bidding_strategy"]["Search"]["BiddingStrategyType"] = search
    item["bidding_strategy"]["Network"]["BiddingStrategyType"] = network
    result = audit.audit_campaign(item, selected_policy=policy.get())
    assert findings(result)["goals.explicit_business_selection"]["status"] == "BLOCK"


def test_raw_legacy_clicks_plan_can_omit_metrika():
    item, groups = raw_plan()
    raw = item["TextCampaign"]
    raw.pop("CounterIds")
    raw.pop("PriorityGoals")
    raw["BiddingStrategy"]["Search"] = {
        "BiddingStrategyType": "WB_MAXIMUM_CLICKS",
        "WbMaximumClicks": {"WeeklySpendLimit": 3000000000},
    }
    normalized, _ = campaign_setup.normalize_plan(item, groups)
    assert "CounterIds" not in normalized["TextCampaign"]
    assert "PriorityGoals" not in normalized["TextCampaign"]
    raw["BiddingStrategy"]["Search"] = {
        "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
        "WbMaximumConversionRate": {"WeeklySpendLimit": 3000000000, "GoalId": 77},
    }
    with pytest.raises(ValueError, match="CounterIds"):
        campaign_setup.normalize_plan(item, groups)
