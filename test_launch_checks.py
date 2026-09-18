"""Measurement changes: offline contract tests, no real Direct or landing requests."""

from copy import deepcopy

import pytest

from test_bundle import source_bundle
from test_executor import FakeApi, readback_payloads
from yadirect_mcp import (
    bundle,
    executor,
    landing,
    policy,
    preflight_refs,
    regions,
    report_context,
    tracking,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def checked(pages, **kwargs):
        return [{"url": row["url"], "ok": True, "status_code": 200} for row in pages]
    monkeypatch.setattr(landing, "inspect_pages", checked)
    regions.reset_cache()


def budget(amount=7000, **kwargs):
    return {"amount": amount, "period": "weekly", "currency": "RUB",
            "includes_vat": False, **kwargs}


def source_with_budget(**kwargs):
    source = source_bundle()
    source["client_budget"] = budget(**kwargs)
    return source


def test_new_utm_profile_preserves_bi_and_overrides_href_without_duplicate_keys():
    plan = bundle.compile_bundle(source_bundle(), "client")
    params = plan["campaigns"][0]["campaign"]["UnifiedCampaign"]["TrackingParams"]
    actual = tracking.parse_params(params)
    assert actual == policy.TRACKING_PARAMS_UTM_V1
    assert all(actual[key] == value for key, value in policy.TRACKING_PARAMS_V1.items())
    effective = tracking.effective_params("https://example.test/?utm_source=old", params)
    assert effective["effective_params"]["utm_source"] == "yandex"
    assert effective["conflicts"]["utm_source"] == {"href": "old", "campaign": "yandex"}
    assert plan["summary"]["tracking_profile"] == "utm_v1"


def test_legacy_profile_is_explicit_and_hash_bound():
    source = source_bundle()
    current = bundle.compile_bundle(source, "client")
    source["tracking_profile"] = "regulation_v1"
    legacy = bundle.compile_bundle(source, "client")
    actual = legacy["campaigns"][0]["campaign"]["UnifiedCampaign"]["TrackingParams"]
    assert tracking.parse_params(actual) == policy.TRACKING_PARAMS_V1
    assert current["plan_hash"] != legacy["plan_hash"]


def test_alternative_texts_are_explicit_and_opt_in_changes_hash():
    source = source_bundle()
    stopped = bundle.compile_bundle(source, "client")
    source["settings"] = {"ALTERNATIVE_TEXTS_ENABLED": True}
    enabled = bundle.compile_bundle(source, "client")
    for plan, value in [(stopped, "NO"), (enabled, "YES")]:
        for campaign in plan["campaigns"]:
            settings = campaign["campaign"]["UnifiedCampaign"]["Settings"]
            assert {r["Option"]: r["Value"] for r in settings}["ALTERNATIVE_TEXTS_ENABLED"] == value
    assert stopped["plan_hash"] != enabled["plan_hash"]


def test_weekly_budget_exact_boundary_and_overrun():
    plan = bundle.compile_bundle(source_with_budget(), "client")
    assert plan["summary"]["client_budget"]["weekly_net_remaining_micros"] == 0
    with pytest.raises(ValueError, match="превышает"):
        bundle.compile_bundle(source_with_budget(amount="6999.999999"), "client")


def test_budget_counts_every_campaign_variant():
    source = source_with_budget()
    source["channels"]["search"] = [
        {**deepcopy(source["channels"]["search"]), "weekly_budget": 3000, "name_suffix": suffix}
        for suffix in ["one", "two"]
    ]
    with pytest.raises(ValueError, match="превышает"):
        bundle.compile_bundle(source, "client")


def test_monthly_budget_is_explicit_average_not_calendar_spend_cap():
    source = source_with_budget(amount=30000, period="monthly")
    source["weekly_budget"] = {"search": 3900, "network": 3000}
    plan = bundle.compile_bundle(source, "client")
    check = plan["summary"]["client_budget"]
    assert check["weekly_net_limit_micros"] == 6_923_076_923
    assert check["weekly_net_total_micros"] == 6_900_000_000
    assert check["calendar_month_spend_cap"] is False
    assert check["scope"] == "planned_campaigns"


def test_vat_is_explicit_and_converted_without_changing_campaign_amounts():
    plan = bundle.compile_bundle(
        source_with_budget(amount=8400, includes_vat=True, vat_percent=20), "client")
    assert plan["summary"]["client_budget"]["weekly_net_limit_micros"] == 7_000_000_000
    assert plan["summary"]["weekly_budget_total"] == 7000
    with pytest.raises(ValueError, match="vat_percent"):
        bundle.compile_bundle(source_with_budget(includes_vat=True), "client")


@pytest.mark.parametrize("amount", [True, "NaN", "Infinity", -1, 0, "0.0000001", "1e100"])
def test_invalid_budget_never_reaches_write(amount):
    with pytest.raises(ValueError, match="client_budget"):
        bundle.compile_bundle(source_with_budget(amount=amount), "client")


@pytest.mark.asyncio
async def test_currency_mismatch_blocks_preflight_without_writes():
    plan = bundle.compile_bundle(source_with_budget(currency="USD"), "client")
    api = FakeApi()
    with pytest.raises(ValueError, match="валютой"):
        await executor.preflight(api, plan)
    assert not any(call[2] == "add" for call in api.calls if call[0] != "v4")


@pytest.mark.asyncio
async def test_existing_campaign_budgets_are_outside_plan_scope():
    plan = bundle.compile_bundle(source_with_budget(), "client")
    api = FakeApi(existing=[{"Id": 80, "Name": "Existing", "State": "ON",
                             "UnifiedCampaign": {"BiddingStrategy": {
                                 "Search": {"WeeklySpendLimit": 999_000_000_000}}}}])
    result = await executor.preflight(api, plan)
    assert result["client_budget"]["weekly_net_total_micros"] == 7_000_000_000


def test_budget_changes_hash_even_when_net_limit_is_identical():
    net = bundle.compile_bundle(source_with_budget(), "client")
    gross = bundle.compile_bundle(
        source_with_budget(amount=8400, includes_vat=True, vat_percent=20), "client")
    assert net["client_budget"]["weekly_net_limit_micros"] == (
        gross["client_budget"]["weekly_net_limit_micros"])
    assert net["plan_hash"] != gross["plan_hash"]


@pytest.mark.asyncio
async def test_budget_mutation_blocks_executor_before_any_api_call():
    plan = bundle.compile_bundle(source_with_budget(), "client")
    plan["client_budget"]["weekly_net_limit_micros"] += 1
    api = FakeApi()
    with pytest.raises(ValueError, match="целостность"):
        await executor.apply(api, plan)
    assert api.calls == []


@pytest.mark.asyncio
async def test_budget_is_checked_again_on_actual_readback():
    plan = bundle.compile_bundle(source_with_budget(), "client")
    execution = await executor.apply(FakeApi(), plan)
    actual, *_ = deepcopy(readback_payloads(plan, execution))
    actual["campaigns"][0]["bidding_strategy"]["Search"]["WbMaximumClicks"][
        "WeeklySpendLimit"] += 1
    result = await executor.readback_launch_checks(FakeApi(), plan, actual["campaigns"])
    assert result["verified"] is False
    assert "превышает" in result["error"]


def conversion_source():
    source = source_bundle()
    source["channels"]["search"]["strategy"] = {"type": "maximum_conversion_rate", "goal_id": 13}
    source["goal_catalog_campaign_id"] = 55
    source["allow_unverified_goals"] = False
    return source


@pytest.mark.asyncio
async def test_goal_13_uses_real_goals_in_catalog_and_reports():
    source = conversion_source()
    source["priority_goals"].append({"goal_id": 12, "value": 1})
    plan = bundle.compile_bundle(source, "client")
    api = FakeApi(existing=[{"Id": 55, "Name": "Catalog", "State": "OFF"}])
    result = await executor.preflight(api, plan)
    assert result["goals"]["goal_ids"] == [77]
    unified = plan["campaigns"][0]["campaign"]["UnifiedCampaign"]
    assert unified["BiddingStrategy"]["Search"]["WbMaximumConversionRate"]["GoalId"] == 13
    resolved, origin = report_context.campaign_goals({
        "bidding_strategy": unified["BiddingStrategy"],
        "priority_goals": unified["PriorityGoals"]["Items"],
    })
    assert resolved == ["12", "77"]
    assert origin == "strategy_priority_goals"


@pytest.mark.parametrize("goals", [[], [{"goal_id": 12, "value": 1}],
                                  [{"goal_id": 13, "value": 1}]])
def test_goal_13_rejects_missing_or_system_only_priority_goals(goals):
    source = conversion_source()
    source["priority_goals"] = goals
    with pytest.raises(ValueError, match="13"):
        bundle.compile_bundle(source, "client")


@pytest.mark.asyncio
async def test_goal_13_still_checks_real_goal_existence():
    class WrongGoals(FakeApi):
        async def call_v4(self, method, param=None):
            return [{"GoalID": 88}]
    plan = bundle.compile_bundle(conversion_source(), "client")
    with pytest.raises(ValueError, match="77"):
        await executor.preflight(WrongGoals(existing=[{"Id": 55}]), plan)


@pytest.mark.asyncio
async def test_strategy_goal_drift_is_a_readback_failure():
    plan = bundle.compile_bundle(conversion_source(), "client")
    execution = await executor.apply(FakeApi(), plan)
    settings, groups, ads, ids = deepcopy(readback_payloads(plan, execution))
    settings["campaigns"][0]["bidding_strategy"]["Search"]["WbMaximumConversionRate"]["GoalId"] = 77
    result = executor.compare_readback(plan, execution, settings, groups, ads, ids)
    assert result["verified"] is False
    assert any(row["rule"] == "readback.strategy_goal" and row["status"] == "BLOCK"
               for row in result["findings"])


def test_business_id_requires_declared_expected_contacts():
    source = source_bundle(office=True)
    source.pop("business_profiles")
    with pytest.raises(ValueError, match="business_profiles"):
        bundle.compile_bundle(source, "client")


@pytest.mark.asyncio
async def test_business_contacts_are_normalized_and_checked_live():
    source = source_bundle(office=True)
    source["business_profiles"][0]["phone"] = "8 (495) 123-45-67"
    source["business_profiles"][0]["address"] = "  МОСКВА,   Тестовая 1 "
    plan = bundle.compile_bundle(source, "client")
    result = await executor.preflight(FakeApi(), plan)
    assert result["assets"]["business_contacts"]["status"] == "PASS"
    assert result["assets"]["business_contacts"]["checked"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("Phone", "74950000000"), ("Address", "Other address"),
                                         ("HasOffice", "NO"), ("IsPublished", "NO")])
async def test_contact_drift_blocks_preflight_and_independent_readback(field, value):
    class ChangedBusiness(FakeApi):
        async def call_v501(self, service, method, params, *, client_login=None):
            response = await super().call_v501(service, method, params, client_login=client_login)
            if service == "businesses":
                response["Businesses"][0][field] = value
            return response
    plan = bundle.compile_bundle(source_bundle(office=True), "client")
    with pytest.raises(ValueError):
        await executor.preflight(ChangedBusiness(), plan)
    result = await executor.readback_launch_checks(ChangedBusiness(), plan, [])
    assert result["verified"] is False


@pytest.mark.asyncio
async def test_duplicate_business_read_is_not_verified():
    class DuplicateBusiness(FakeApi):
        async def call_v501(self, service, method, params, *, client_login=None):
            response = await super().call_v501(service, method, params, client_login=client_login)
            if service == "businesses":
                response["Businesses"] *= 2
            return response
    plan = bundle.compile_bundle(source_bundle(office=True), "client")
    with pytest.raises(ValueError, match="неоднозначная"):
        await preflight_refs.assets(DuplicateBusiness(), "client", [{"BusinessId": 999}],
                                    business_profiles=plan["business_profiles"])


def test_business_contact_change_invalidates_hash():
    source = source_bundle(office=True)
    before = bundle.compile_bundle(source, "client")
    source["business_profiles"][0]["phone"] = "+74950000000"
    after = bundle.compile_bundle(source, "client")
    assert before["plan_hash"] != after["plan_hash"]


def test_legacy_missing_client_budget_is_not_reported_as_pass():
    plan = bundle.compile_bundle(source_bundle(), "client")
    assert plan["summary"]["client_budget"]["status"] == "not_configured"


def test_system_only_goal_13_is_not_resolved_as_business_conversions():
    goals, reason = report_context.campaign_goals({
        "bidding_strategy": {"Search": {"WbMaximumConversionRate": {"GoalId": 13}}},
        "priority_goals": [{"GoalId": 12}],
    })
    assert goals is None
    assert reason == "priority_goals_unavailable"


@pytest.mark.parametrize("goal_id", [True, 13.0, 13.9, "13.0", "1.3e1"])
def test_goal_selector_rejects_lossy_ids(goal_id):
    source = conversion_source()
    source["channels"]["search"]["strategy"]["goal_id"] = goal_id
    with pytest.raises(ValueError, match="goal_id"):
        bundle.compile_bundle(source, "client")


@pytest.mark.parametrize("goal_id", [True, 77.0, 77.9, "77.0"])
def test_priority_goals_reject_lossy_ids(goal_id):
    source = conversion_source()
    source["priority_goals"][0]["goal_id"] = goal_id
    with pytest.raises(ValueError, match="goal_id"):
        bundle.compile_bundle(source, "client")


def test_decimal_string_goal_ids_are_supported():
    source = conversion_source()
    source["channels"]["search"]["strategy"]["goal_id"] = "13"
    source["priority_goals"][0]["goal_id"] = "77"
    plan = bundle.compile_bundle(source, "client")
    unified = plan["campaigns"][0]["campaign"]["UnifiedCampaign"]
    assert unified["BiddingStrategy"]["Search"]["WbMaximumConversionRate"]["GoalId"] == 13
    assert unified["PriorityGoals"]["Items"][0]["GoalId"] == 77
