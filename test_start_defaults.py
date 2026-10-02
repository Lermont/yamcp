"""Start defaults and delegated consent regressions; no live advertising API."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from test_policy_profiles import source, strategy
from yadirect_mcp import approval, bundle, config, executor, launch_checks, planning, policy


def current(business="services_b2b", stage="new"):
    raw = source(business, stage)
    raw["policy_name"] = f"{business}_{stage}_v3"
    raw.pop("client_budget")
    raw["allow_unverified_goals"] = False
    return raw


@pytest.mark.parametrize("business", ["services_b2b", "local_business", "ecommerce"])
@pytest.mark.parametrize("stage", ["new", "established"])
def test_conversion_start_without_sufficient_history_and_gross_fortnight(business, stage):
    raw = current(business, stage)
    for row in raw["profile_context"].get("history", []):
        row["conversions"] = 0
    plan = bundle.compile_bundle(raw, "client")
    for campaign in plan["campaigns"]:
        for branch in campaign["campaign"]["UnifiedCampaign"]["BiddingStrategy"].values():
            if branch["BiddingStrategyType"] != "SERVING_OFF":
                assert branch["BiddingStrategyType"] == "WB_MAXIMUM_CONVERSION_RATE"
                assert branch["WbMaximumConversionRate"]["GoalId"] == 77
    budget = plan["summary"]["client_budget"]
    assert budget["amount"] == "30000"
    assert budget["includes_vat"] is True
    assert budget["period"] == "two_weeks"
    assert budget["weekly_net_limit_micros"] == 12295_081967
    executor.validate_plan(plan)


@pytest.mark.parametrize("has_counter", [False, True])
def test_missing_goals_use_clicks_without_fabricating_measurement(has_counter):
    raw = current()
    raw["counter_ids"] = [12345] if has_counter else []
    raw["priority_goals"] = []
    raw["profile_context"] = {}
    plan = bundle.compile_bundle(raw, "client")
    assert strategy(plan)["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"


def test_multiple_verified_goals_use_selector_13_without_history():
    raw = current()
    raw["priority_goals"].append({"goal_id": 88, "value": 1000})
    raw["profile_context"]["goals"].append({"goal_id": 88, "kind": "lead"})
    plan = bundle.compile_bundle(raw, "client")
    assert strategy(plan)["WbMaximumConversionRate"]["GoalId"] == 13
    executor.validate_plan(plan)


@pytest.mark.parametrize("field", ["measurement_verified", "allow_unverified_goals"])
def test_unverified_measurement_blocks_instead_of_silent_click_fallback(field):
    raw = current()
    if field == "measurement_verified":
        raw["profile_context"][field] = False
    else:
        raw[field] = True
    with pytest.raises(ValueError, match="проверьте|live-каталог"):
        bundle.compile_bundle(raw, "client")


def test_explicit_budget_overrides_and_overallocation_are_checked():
    raw = current()
    raw["client_budget"] = {"amount": 15000}
    with pytest.raises(ValueError, match="превышает"):
        bundle.compile_bundle(raw, "client")
    raw["client_budget"] = {"amount": 14000, "includes_vat": False}
    plan = bundle.compile_bundle(raw, "client")
    assert plan["client_budget"]["weekly_net_limit_micros"] == 7000_000000
    assert plan["client_budget"]["vat_percent"] == "0"
    raw["client_budget"] = {"amount": 30000, "period": "monthly", "includes_vat": False}
    raw["weekly_budget"] = {"search": 3500, "network": 3400}
    assert bundle.compile_bundle(raw, "client")["client_budget"][
        "weekly_net_limit_micros"] == 6923_076923


def test_fortnight_boundary_is_floor_not_round_and_tamper_is_rejected():
    contract = launch_checks.normalize_budget(planning.initial_budget(None))
    campaign = {"bidding_strategy": {"Search": {
        "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
        "WbMaximumConversionRate": {"WeeklySpendLimit": 12295_081967}}}}
    assert launch_checks.check_budget(contract, [campaign])["status"] == "PASS"
    campaign["bidding_strategy"]["Search"]["WbMaximumConversionRate"][
        "WeeklySpendLimit"] += 1
    with pytest.raises(ValueError, match="превышает"):
        launch_checks.check_budget(contract, [campaign])
    contract["weekly_net_limit_micros"] += 1
    with pytest.raises(ValueError, match="целостность"):
        launch_checks.check_budget(contract, [campaign])


def test_non_rub_tax_needs_explicit_rate_and_old_profiles_stay_frozen():
    with pytest.raises(ValueError, match="вне RUB"):
        planning.initial_budget({"currency": "USD"})
    assert planning.initial_budget({"currency": "USD", "vat_percent": 0})["vat_percent"] == 0
    assert "conversion_history_required" not in policy.get("services_b2b_new_v1")["profile"]
    old = bundle.compile_bundle(source(), "client")
    assert strategy(old)["BiddingStrategyType"] == "WB_MAXIMUM_CLICKS"


@pytest.mark.asyncio
async def test_task_authorized_has_honest_receipt_no_form_and_one_time_token(monkeypatch):
    ask = AsyncMock(side_effect=AssertionError("must not ask in delegated mode"))
    monkeypatch.setattr(approval, "elicit", ask)
    registry = approval.ApprovalRegistry()
    plan = {"client_login": "client", "plan_hash": "a" * 64}
    token = registry.issue(**plan).phrase
    receipt = await registry.authorize(None, plan, "campaigns", token, mode="task_authorized")
    assert receipt["mechanism"] == "configured_task_authorization"
    assert receipt["identity_assurance"] == "task_scope_delegated"
    assert receipt["plan_hash"] == plan["plan_hash"]
    ask.assert_not_called()
    with pytest.raises(ValueError, match="использовано"):
        await registry.authorize(None, plan, "campaigns", token, mode="task_authorized")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["login", "hash", "expired", "unknown_mode"])
async def test_delegated_mode_preserves_binding_expiry_and_mode_validation(monkeypatch, change):
    registry = approval.ApprovalRegistry()
    plan = {"client_login": "client", "plan_hash": "a" * 64}
    grant = registry.issue(**plan)
    changed = deepcopy(plan)
    mode = "task_authorized"
    if change == "login":
        changed["client_login"] = "another"
    elif change == "hash":
        changed["plan_hash"] = "b" * 64
    elif change == "expired":
        monkeypatch.setattr(approval.time, "monotonic", lambda: grant.expires_at + 1)
    else:
        mode = "unknown"
    with pytest.raises(ValueError):
        await registry.authorize(None, changed, "campaigns", grant.phrase, mode=mode)


def test_mode_is_operator_config_not_tool_input(monkeypatch, tmp_path):
    monkeypatch.setenv("YD_TOKEN", "offline-fixture")
    monkeypatch.setenv("YD_OUT_DIR", str(tmp_path))
    monkeypatch.delenv("YD_APPROVAL_MODE", raising=False)
    assert config.load().approval_mode == "elicitation"
    monkeypatch.setenv("YD_APPROVAL_MODE", "task_authorized")
    assert config.load().approval_mode == "task_authorized"
    monkeypatch.setenv("YD_APPROVAL_MODE", "anything")
    with pytest.raises(RuntimeError, match="YD_APPROVAL_MODE"):
        config.load()


def test_product_v3_uses_one_shared_budget_and_conversions_without_history():
    from test_products import source as product_source

    raw = product_source()
    raw["policy_name"] = "ecommerce_new_v3"
    raw.pop("client_budget")
    raw["allow_unverified_goals"] = False
    plan = bundle.compile_bundle(raw, "client")
    product = next(c for c in plan["campaigns"] if c["channel"] == "product")
    strategy = product["campaign"]["UnifiedCampaign"]["BiddingStrategy"]
    assert strategy["Search"]["BiddingStrategyType"] == "WB_MAXIMUM_CONVERSION_RATE"
    assert strategy["Network"]["BiddingStrategyType"] == "NETWORK_DEFAULT"
    assert plan["summary"]["client_budget"]["weekly_net_total_micros"] == 6900_000000
    executor.validate_plan(plan)
