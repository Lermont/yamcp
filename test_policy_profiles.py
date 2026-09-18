"""Offline business profile and frozen-plan compatibility regressions."""

import json
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from test_bundle import by_channel, source_bundle
from test_executor import FakeApi, readback_payloads
from yadirect_mcp import audit, bundle, campaign_setup, executor, landing, policy, regions


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def pages(rows, **kwargs):
        return [{"url": row["url"], "ok": True, "status_code": 200} for row in rows]

    monkeypatch.setattr(landing, "inspect_pages", pages)
    regions.reset_cache()


def history(channel="search", **changes):
    today = campaign_setup.moscow_today()
    return {
        "client_login": "client",
        "channel": channel,
        "region_ids": [213],
        "goal_id": 77,
        "counter_id": 12345,
        "date_from": (today - timedelta(days=14)).isoformat(),
        "date_to": (today - timedelta(days=1)).isoformat(),
        "conversions": 20,
        "source": "fixture://reviewed-report.tsv",
        "complete": True,
        "reviewed": True,
        "reason": "Проверены бизнес-цель, период, география и качество конверсий",
        **changes,
    }


def source(business="services_b2b", stage="new"):
    raw = source_bundle(office=business == "local_business")
    raw["policy_name"] = f"{business}_{stage}_v1"
    raw["client_budget"] = {
        "amount": 9000 if raw["office"] else 7000,
        "period": "weekly",
        "currency": "RUB",
        "includes_vat": False,
    }
    kind = {
        "services_b2b": "qualified_lead",
        "local_business": "phone_call",
        "ecommerce": "purchase",
    }[business]
    raw["profile_context"] = {
        "measurement_verified": True,
        "goals": [{"goal_id": "77", "kind": kind}],
        "catalog_reviewed": business == "ecommerce",
    }
    if business == "ecommerce":
        raw["semantic_plan"].pop("profile")
    if stage == "established":
        raw["profile_context"]["history"] = [history()]
        raw["allow_unverified_goals"] = False
        raw["goal_catalog_campaign_id"] = 55
    return raw


def strategy(plan, channel="search"):
    branch = "Network" if channel == "network" else "Search"
    return by_channel(plan)[channel]["campaign"]["UnifiedCampaign"]["BiddingStrategy"][branch]


def test_frozen_legacy_plans_and_policy_are_unchanged():
    fixture = Path(__file__).parent / "tests/fixtures/policy_legacy_1_12.json"
    records = json.loads(fixture.read_text(encoding="utf-8"))
    expected_hashes = [
        "23941ba04526f1ebb0c690869ee3cbf607e6912d3702bb74ecbc859f1f7fe212",
        "87501855d2f0cdf26d472c12b06c282f46614ec8fbecdbb0a32363b2d9660901",
        "36dad1c0be071f2ea6032c4d6b18fb31d72a1059b9cb3a16eb91f234be78a445",
    ]
    for record, expected_hash in zip(records, expected_hashes, strict=True):
        actual = bundle.compile_bundle(record["source"], "client", today=date(2026, 9, 18))
        assert actual == record["plan"]
        assert actual["plan_hash"] == expected_hash
        explicit = {**record["source"], "policy_name": "agency_default_v1"}
        assert bundle.compile_bundle(explicit, "client", today=date(2026, 9, 18)) == actual
    assert policy.get() == policy.AGENCY_POLICY_V1


@pytest.mark.parametrize("business", ["services_b2b", "local_business", "ecommerce"])
@pytest.mark.parametrize("stage", ["new", "established"])
def test_all_six_profiles_compile_with_bound_snapshot_and_defaults(business, stage):
    plan = bundle.compile_bundle(source(business, stage), "client")
    assert plan["ready"]
    assert plan["policy"]["name"] == f"{business}_{stage}_v1"
    assert plan["policy"]["version"] == "1.0.0"
    assert policy.for_plan(plan) == policy.get(plan["policy"]["name"])
    assert strategy(plan)["BiddingStrategyType"] == (
        "WB_MAXIMUM_CONVERSION_RATE" if stage == "established" else "WB_MAXIMUM_CLICKS"
    )
    assert strategy(plan, "network")["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"
    assert "ExcludedSites" not in by_channel(plan)["network"]["campaign"]
    assert plan["semantic_review"]["profile"] == (
        "catalogue" if business == "ecommerce" else "small_business"
    )
    executor.validate_plan(plan)


@pytest.mark.parametrize(
    "changes",
    [
        {"complete": False},
        {"reviewed": False},
        {"conversions": 19},
        {"conversions": 0},
        {"region_ids": [2]},
        {"channel": "network"},
        {"date_from": "2025-01-01", "date_to": "2025-01-14"},
    ],
)
def test_ineligible_history_keeps_clicks_with_visible_reason(changes):
    raw = source(stage="established")
    raw["profile_context"]["history"] = [history(**changes)]
    plan = bundle.compile_bundle(raw, "client")
    assert strategy(plan)["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"
    assert "Нет достаточной истории" in plan["profile_decisions"][0]["reason"]


def test_unverified_measurement_and_short_period_keep_clicks():
    raw = source(stage="established")
    raw["profile_context"]["measurement_verified"] = False
    assert (
        strategy(bundle.compile_bundle(raw, "client"))["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"
    )
    raw["profile_context"]["measurement_verified"] = True
    row = raw["profile_context"]["history"][0]
    row["date_from"] = row["date_to"]
    assert (
        strategy(bundle.compile_bundle(raw, "client"))["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"
    )


def test_channel_and_geo_evidence_do_not_leak_between_campaigns():
    raw = source(stage="established")
    raw["channels"]["search"] = [
        {
            **deepcopy(raw["channels"]["search"]),
            "name_suffix": suffix,
            "region_ids": ids,
            "weekly_budget": 1500,
        }
        for suffix, ids in [("Москва", [213]), ("Петербург", [2])]
    ]
    plan = bundle.compile_bundle(raw, "client")
    types = [
        r["campaign"]["UnifiedCampaign"]["BiddingStrategy"]["Search"]["BiddingStrategyType"]
        for r in plan["campaigns"][:2]
    ]
    assert types == ["WB_MAXIMUM_CONVERSION_RATE", "WB_MAXIMUM_CLICKS"]


@pytest.mark.parametrize(
    "changes",
    [
        {"client_login": "other"},
        {"goal_id": 88},
        {"counter_id": 99},
        {"reviewed": "true"},
        {"complete": 1},
        {"conversions": "NaN"},
        {"date_to": "2999-01-01"},
        {"date_from": "2099-01-01"},
        {"source": ""},
        {"conversions": True},
        {"region_ids": []},
    ],
)
def test_invalid_history_is_rejected(changes):
    raw = source(stage="established")
    raw["profile_context"]["history"] = [history(**changes)]
    with pytest.raises(ValueError):
        bundle.compile_bundle(raw, "client")


def test_duplicate_history_and_wrong_stage_are_rejected():
    raw = source(stage="established")
    raw["profile_context"]["history"] *= 2
    with pytest.raises(ValueError, match="неоднозначные"):
        bundle.compile_bundle(raw, "client")
    raw["profile_context"]["history"] = []
    with pytest.raises(ValueError, match="established"):
        bundle.compile_bundle(raw, "client")
    raw = source()
    raw["profile_context"]["history"] = [history()]
    with pytest.raises(ValueError, match="established"):
        bundle.compile_bundle(raw, "client")


def test_local_requires_office_maps_and_business_contacts():
    raw = source("local_business")
    raw["office"] = False
    raw["channels"].pop("maps")
    raw.pop("business_profiles")
    with pytest.raises(ValueError, match="local_business"):
        bundle.compile_bundle(raw, "client")


def test_services_office_does_not_force_maps():
    raw = source()
    raw["office"] = True
    plan = bundle.compile_bundle(raw, "client")
    assert len(plan["campaigns"]) == 2


@pytest.mark.parametrize(
    "business,wrong_kind",
    [
        ("services_b2b", "page_view"),
        ("local_business", "phone_click"),
        ("ecommerce", "add_to_cart"),
    ],
)
def test_business_goal_classification_rejects_microgoals(business, wrong_kind):
    raw = source(business)
    raw["profile_context"]["goals"][0]["kind"] = wrong_kind
    with pytest.raises(ValueError, match="kind"):
        bundle.compile_bundle(raw, "client")


def test_ecommerce_requires_reviewed_catalog():
    raw = source("ecommerce")
    raw["profile_context"]["catalog_reviewed"] = False
    with pytest.raises(ValueError, match="catalog_reviewed"):
        bundle.compile_bundle(raw, "client")


def test_local_structure_range_is_distinct_without_changing_legacy():
    raw = source("local_business")
    for channel in raw["channels"].values():
        channel.pop("group_count_reason")
    plan = bundle.compile_bundle(raw, "client")
    assert not any(r["rule"] == "structure.group_count" for r in plan["findings"])
    raw = source()
    raw["channels"]["search"].pop("group_count_reason")
    assert any(
        r["rule"] == "structure.group_count" and r["status"] == "WARNING"
        for r in bundle.compile_bundle(raw, "client")["findings"]
    )


def test_new_profile_requires_total_budget_and_utm():
    raw = source()
    raw.pop("client_budget")
    with pytest.raises(ValueError, match="client_budget"):
        bundle.compile_bundle(raw, "client")
    raw = source()
    raw["tracking_profile"] = "regulation_v1"
    with pytest.raises(ValueError, match="utm_v1"):
        bundle.compile_bundle(raw, "client")


def test_exclusions_are_explicit_and_require_a_reason():
    raw = source()
    raw["additional_excluded_sites"] = ["bad.example"]
    with pytest.raises(ValueError, match="exclusions_reason"):
        bundle.compile_bundle(raw, "client")
    raw["profile_context"]["exclusions_reason"] = "Нецелевые площадки по проверенному отчёту"
    plan = bundle.compile_bundle(raw, "client")
    assert by_channel(plan)["network"]["campaign"]["ExcludedSites"] == {"Items": ["bad.example"]}


@pytest.mark.parametrize(
    "key,field", [("autotexts_reason", "settings"), ("schedule_reason", "schedule")]
)
def test_optional_changes_require_profile_reason(key, field):
    raw = source()
    raw[field] = (
        {"ALTERNATIVE_TEXTS_ENABLED": True}
        if field == "settings"
        else {"days": [1, 2, 3, 4, 5], "hours": [9, 10, 11]}
    )
    with pytest.raises(ValueError, match=key):
        bundle.compile_bundle(raw, "client")
    raw["profile_context"][key] = "Согласовано по условиям обработки обращений"
    assert bundle.compile_bundle(raw, "client")["ready"]


def test_new_business_without_goals_can_start_with_clicks():
    raw = source()
    raw.pop("counter_ids")
    raw.pop("priority_goals")
    raw["profile_context"] = {}
    plan = bundle.compile_bundle(raw, "client")
    assert strategy(plan)["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"


def test_explicit_clicks_respected_and_conversion_needs_live_goal_catalog():
    raw = source(stage="established")
    raw["channels"]["search"]["strategy"] = "maximum_clicks"
    assert (
        strategy(bundle.compile_bundle(raw, "client"))["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"
    )
    raw["channels"]["search"].pop("strategy")
    raw["allow_unverified_goals"] = True
    with pytest.raises(ValueError, match="live-каталог"):
        bundle.compile_bundle(raw, "client")


@pytest.mark.asyncio
async def test_profile_preflight_checks_live_goals_without_write():
    plan = bundle.compile_bundle(source(stage="established"), "client")
    api = FakeApi(existing=[{"Id": 55, "Name": "Catalog", "State": "OFF"}])
    result = await executor.preflight(api, plan)
    assert result["goals"]["status"] == "PASS"
    assert not any(c[2] == "add" for c in api.calls if c[0] != "v4")


@pytest.mark.parametrize("field", ["version", "snapshot", "fingerprint", "budget", "goal"])
@pytest.mark.asyncio
async def test_tampered_profile_plan_fails_before_any_api_call(field):
    plan = bundle.compile_bundle(source(), "client")
    if field == "version":
        plan["policy"]["version"] = "2.0.0"
    elif field == "snapshot":
        plan["policy_snapshot"]["profile"]["stage"] = "established"
    elif field == "fingerprint":
        plan["policy_fingerprint"] = "fake"
    elif field == "budget":
        plan["client_budget"] = None
    else:
        plan["campaigns"][1]["campaign"]["UnifiedCampaign"]["PriorityGoals"]["Items"] = []
    api = FakeApi()
    with pytest.raises(ValueError):
        await executor.apply(api, plan)
    assert api.calls == []


def test_profile_and_evidence_changes_invalidate_hash():
    first = source()
    second = source("ecommerce")
    assert (
        bundle.compile_bundle(first, "client")["plan_hash"]
        != (bundle.compile_bundle(second, "client")["plan_hash"])
    )
    raw = source(stage="established")
    before = bundle.compile_bundle(raw, "client")
    raw["profile_context"]["history"][0]["source"] = "fixture://another-report.tsv"
    assert bundle.compile_bundle(raw, "client")["plan_hash"] != before["plan_hash"]


@pytest.mark.asyncio
async def test_readback_uses_selected_profile_and_does_not_require_legacy_blacklist(monkeypatch):
    plan = bundle.compile_bundle(source(), "client")
    execution = await executor.apply(FakeApi(), plan)
    settings, groups, ads, ids = readback_payloads(plan, execution)
    captured = {}
    actual_audit = audit.audit_payload

    def audit_with_profile(*args, **kwargs):
        captured.update(policy_name=kwargs.get("policy_name"))
        result = actual_audit(*args, **kwargs)
        captured["result"] = result
        return result

    monkeypatch.setattr(executor.campaigns, "read_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(executor.adgroups, "read", AsyncMock(return_value=groups))
    monkeypatch.setattr(executor.ads, "read", AsyncMock(return_value=ads))
    monkeypatch.setattr(executor.assets, "enrich_ads", AsyncMock(return_value={}))
    monkeypatch.setattr(executor, "_read_created_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(executor.keywords, "read", AsyncMock(return_value={"keywords": []}))
    monkeypatch.setattr(executor.link_checks, "check_live", AsyncMock(return_value=[]))
    monkeypatch.setattr(executor.account, "read", AsyncMock(return_value={}))
    monkeypatch.setattr(
        executor.audience_setup, "readback", AsyncMock(return_value={"verified": True, "rows": []})
    )
    monkeypatch.setattr(executor.audit, "audit_payload", audit_with_profile)
    # Missing objects intentionally fail readback; audit must still use the exact profile.
    result = await executor.readback(FakeApi(), plan, execution)
    assert captured["policy_name"] == plan["policy"]["name"]
    assert result["verified"] is False
    findings = [r for c in captured["result"]["campaigns"] for r in c["findings"]]
    assert all(r["status"] != "BLOCK" for r in findings if r["rule"] == "network.excluded_sites")


@pytest.mark.parametrize("region_ids", [[0], [225, -213]])
def test_history_accepts_worldwide_and_excluded_regions(region_ids):
    raw = source(stage="established")
    raw["region_ids"] = region_ids
    raw["profile_context"]["history"][0]["region_ids"] = region_ids
    plan = bundle.compile_bundle(raw, "client")
    assert strategy(plan)["BiddingStrategyType"] == "WB_MAXIMUM_CONVERSION_RATE"


def test_goal_13_needs_history_for_every_primary_goal():
    raw = source(stage="established")
    raw["priority_goals"].append({"goal_id": 88, "value": 1500})
    raw["profile_context"]["goals"].append({"goal_id": 88, "kind": "qualified_lead"})
    raw["channels"]["search"]["strategy"] = {"type": "maximum_conversion_rate", "goal_id": 13}
    with pytest.raises(ValueError, match="profile.history"):
        bundle.compile_bundle(raw, "client")
    raw["profile_context"]["history"].append(history(goal_id=88))
    plan = bundle.compile_bundle(raw, "client")
    assert strategy(plan)["WbMaximumConversionRate"]["GoalId"] == 13
    raw["channels"]["search"].pop("strategy")
    assert (
        strategy(bundle.compile_bundle(raw, "client"))["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"
    )


def test_mcp_exposes_profile_catalog_and_knowledge(tmp_path):
    from test_server import _in_server

    result = _in_server(
        "import asyncio, json\nimport yadirect_mcp.server as s\n"
        "r = asyncio.run(s.direct_policy('ecommerce_new_v1'))\n"
        "print(r.model_dump_json())",
        str(tmp_path),
        YD_MODE="report",
    )
    data = result["structuredContent"]
    assert len(data["available_profiles"]) == 9
    assert data["policy"]["profile"]["business"] == "ecommerce"
    guidance = data["planning_defaults"]
    assert guidance["stages"] == ["new", "established"]
    assert guidance["budget"] == {
        "amount": 30000, "period": "monthly", "currency": "RUB",
        "scope": "planned_campaigns", "vat_basis": "explicit_client_choice",
        "allocation": "explicit_within_total",
    }
    assert guidance["campaign_mix"] == {
        "services_b2b": ["search", "network"],
        "local_business": ["search", "network"],
        "ecommerce": ["product", "search"],
    }
    assert guidance["compiler_support"]["product"] is True
    assert guidance["additional_campaigns"] == {"local_business": ["maps"]}
    assert guidance["budget_alone_reduces_to_single_campaign"] is False
    assert "planning_defaults" not in data["policy"]
    from yadirect_mcp import knowledge

    assert "policy-profiles" in knowledge.DOCS
    assert (knowledge.KB_DIR / "policy-profiles.md").is_file()


def test_standalone_audit_does_not_claim_history_is_verified():
    from test_policy_audit import campaign

    result = audit.audit_campaign(campaign(), selected_policy=policy.get("services_b2b_new_v1"))
    findings = {row["rule"]: row for row in result["findings"]}
    assert findings["profile.evidence"]["status"] == "MANUAL"
    assert findings["budget.client_total"]["status"] == "MANUAL"


@pytest.mark.parametrize("stage", ["new", "established"])
@pytest.mark.parametrize("business", ["services_b2b", "local_business"])
def test_monthly_30000_keeps_standard_test_within_total(business, stage):
    raw = source(business, stage)
    raw["client_budget"] = {
        "amount": 30000, "period": "monthly", "currency": "RUB", "includes_vat": False,
    }
    raw["weekly_budget"] = (
        {"search": 2500, "network": 2400, "maps": 2000}
        if business == "local_business" else {"search": 3500, "network": 3400}
    )
    plan = bundle.compile_bundle(raw, "client")
    assert plan["ready"]
    assert set(by_channel(plan)) == set(raw["weekly_budget"])
    budget = plan["summary"]["client_budget"]
    assert budget["status"] == "PASS"
    assert budget["scope"] == "planned_campaigns"
    assert budget["weekly_net_total_micros"] == 6900_000000
    assert budget["weekly_net_limit_micros"] == 6923_076923
    executor.validate_plan(plan)
