"""Offline end-to-end checks for Network append, creative defaults and native 24/7."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_approval_compatibility import host
from test_group_append import FakeAPI, run, source
from test_group_append import offline as offline  # noqa: F401 - shared autouse fixture
from test_start_defaults import current
from yadirect_mcp import approval, bundle, creative, executor, group_append, repair, workflow


def network_source():
    raw = source()
    group = raw["new_groups"][0]
    group["channel"] = "network"
    group.pop("autotargeting")
    for index, ad in enumerate(group["ads"]):
        ad["ad_image_hashes"] = [f"image-{index}-{n}" for n in range(3)]
        ad["action_button"] = {"text": "Подробнее", "reason": "Условия услуги"}
    return raw


class NetworkAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.missing_image = False
        self.rows["campaigns"][0]["TimeTargeting"] = {
            "Schedule": {"Items": [",".join(map(str, [1] + [0] * 24))]},
            "ConsiderWorkingWeekends": "YES", "HolidaysSchedule": {"SuspendOnHolidays": "YES"},
        }
        self.rows["campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"] = {
            "Search": {"BiddingStrategyType": "SERVING_OFF"},
            "Network": {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                        "PlacementTypes": {"Network": "YES", "Maps": "NO"},
                        "WbMaximumConversionRate": {"WeeklySpendLimit": 5000000000, "GoalId": 77}},
        }

    async def call_v501(self, service, method, params, **kwargs):
        if (service, method) == ("adimages", "get"):
            hashes = params["SelectionCriteria"]["AdImageHashes"]
            return {"AdImages": [{"AdImageHash": h, "Type": "REGULAR"}
                                 for h in (hashes[1:] if self.missing_image else hashes)]}
        if (service, method) == ("campaigns", "update"):
            previous = {row["Id"]: deepcopy(row["UnifiedCampaign"])
                        for row in self.rows["campaigns"]}
            result = await super().call_v501(service, method, params, **kwargs)
            for row in params["Campaigns"]:
                target = next(r for r in self.rows["campaigns"] if r["Id"] == row["Id"])
                patch = row.get("UnifiedCampaign", {})
                typed = {**previous[row["Id"]], **patch}
                if "Settings" in patch:
                    flags = {r["Option"]: r["Value"]
                             for r in previous[row["Id"]].get("Settings", [])}
                    flags.update({r["Option"]: r["Value"] for r in patch["Settings"]})
                    typed["Settings"] = [{"Option": k, "Value": v} for k, v in flags.items()]
                target["UnifiedCampaign"] = typed
            return result
        result = await super().call_v501(service, method, params, **kwargs)
        if (service, method) == ("ads", "get"):
            for row in result["Ads"]:
                value = row["ResponsiveAd"]
                value["AdImages"] = {"Items": [{"ImageHash": h}
                                              for h in value.get("AdImageHashes", [])]}
        return result


def test_network_append_roundtrip_preserves_budget_and_binds_ui_work():
    api = NetworkAPI()
    old = deepcopy(api.rows)
    plan = repair.normalize(network_source(), "client")
    assert plan["summary"]["new_keywords"] == 1
    assert plan["summary"]["new_autotargetings"] == 0
    result = run(repair.apply(api, plan))
    assert result["status"] == "complete" and not result["activated"]
    result["readback"] = run(repair.readback(
        api, plan, before=result["preflight"]["before"], added=result["added"],
    ))
    assert result["readback"]["verified"], result["readback"]
    assert api.rows["campaigns"] == old["campaigns"]
    assert api.rows["adgroups"][0] == old["adgroups"][0]
    actions = result["required_manual_actions"]
    assert {a["rule"] for a in actions} == {
        "ads.neuro_ad", "ads.action_button", "network.carousel"}
    assert all(a["group_id"] == str(result["added"]["groups"][0]["result"]["Id"]) for a in actions)
    assert workflow.states(result, scope="repair")["setup"] == "pending_verification"
    assert all(m == "add" for _, m, _ in api.writes)


@pytest.mark.parametrize("fault", ["images", "button", "channel", "targeting"])
def test_network_append_requires_its_own_creatives_and_targeting(fault):
    raw = network_source()
    group = raw["new_groups"][0]
    if fault == "images":
        group["ads"][0]["ad_image_hashes"] = ["same"] * 3
    elif fault == "button":
        group["ads"][0].pop("action_button")
    elif fault == "channel":
        group["channel"] = "product"
    else:
        group["keywords"] = []
    with pytest.raises(ValueError):
        repair.normalize(raw, "client")


@pytest.mark.parametrize("fault", [
    "channel", "mixed", "maps", "missing_maps", "package", "ON", "image", "truncated"])
def test_network_preflight_blocks_unsafe_target_without_writes(fault):
    api, raw = NetworkAPI(), network_source()
    campaign = api.rows["campaigns"][0]
    strategy = campaign["UnifiedCampaign"]["BiddingStrategy"]
    if fault == "channel":
        raw["new_groups"][0]["channel"] = "search"
        raw["new_groups"][0]["autotargeting"] = bundle.SAFE_AUTOTARGETING
    elif fault == "mixed":
        strategy["Search"]["BiddingStrategyType"] = "WB_MAXIMUM_CLICKS"
    elif fault == "maps":
        strategy["Network"]["PlacementTypes"]["Maps"] = "YES"
    elif fault == "missing_maps":
        strategy["Network"]["PlacementTypes"].pop("Maps")
    elif fault == "package":
        campaign["UnifiedCampaign"]["PackageBiddingStrategy"] = {"StrategyId": 1}
    elif fault == "ON":
        campaign["State"] = "ON"
    elif fault == "image":
        api.missing_image = True
    else:
        api.truncate = "ads"
    with pytest.raises(ValueError):
        run(repair.apply(api, repair.normalize(raw, "client")))
    assert api.writes == []


def test_network_readback_detects_missing_images_and_partial_add_stays_unverified():
    api, plan = NetworkAPI(), repair.normalize(network_source(), "client")
    result = run(repair.apply(api, plan))
    api.rows["ads"][0]["ResponsiveAd"]["AdImageHashes"].pop()
    check = run(repair.readback(api, plan, before=result["preflight"]["before"],
                                added=result["added"]))
    assert not check["verified"]
    assert any(r["reason"] == "ad_image_hashes" for r in check["mismatches"])
    api = NetworkAPI()
    api.fail = "ads"
    result = run(repair.apply(api, plan))
    assert result["status"] == "partial"
    assert [(s, m) for s, m, _ in api.writes] == [("adgroups", "add"), ("ads", "add")]
    assert all("ad_id" not in a for a in result["required_manual_actions"])


@pytest.mark.asyncio
async def test_network_confirmation_drift_decline_replay_and_reverification(tmp_path, monkeypatch):
    from yadirect_mcp import server, verification

    api = NetworkAPI()
    api.last_units = None
    monkeypatch.setattr(server, "SETTINGS", SimpleNamespace(
        mode="campaign_setup", approval_mode="elicitation", out_dir=tmp_path,
        check_login=lambda login: None,
    ))
    monkeypatch.setattr(server, "_client", api)
    monkeypatch.setattr(approval, "REGISTRY", approval.ApprovalRegistry())
    raw = network_source()
    preview = await server.direct_campaign_repair("client", raw)
    assert not preview.is_error, preview
    token = preview.structured_content["confirmation_required"]
    changed = deepcopy(raw)
    changed["new_groups"][0]["ads"][0]["ad_image_hashes"][0] = "changed"
    for login, value in [("other", raw), ("client", changed)]:
        assert (await server.direct_campaign_repair(login, value, token, ctx=host())).is_error
    assert (await server.direct_campaign_repair("client", raw, token, ctx=host("decline"))).is_error
    api.rows["campaigns"][0]["Name"] += " changed externally"
    refused = await server.direct_campaign_repair("client", raw, token, ctx=host())
    assert api.writes == [], refused
    preview = await server.direct_campaign_repair("client", raw)
    token = preview.structured_content["confirmation_required"]
    result = await server.direct_campaign_repair("client", raw, token, ctx=host())
    assert not result.is_error, result
    saved = result.structured_content
    assert saved["readback"]["verified"], saved["readback"]
    assert saved["workflow"]["scope"] == "repair" and saved["setup_complete"] is False
    assert (await server.direct_campaign_repair("client", raw, token, ctx=host())).is_error
    count = len(api.writes)
    check = await verification.run(api, tmp_path, "client", saved["job_id"])
    assert check["readback"]["verified"]
    assert check["workflow"]["ui_verification"] == "pending"
    assert len(api.writes) == count


@pytest.mark.parametrize("business", ["services_b2b", "local_business", "ecommerce"])
@pytest.mark.parametrize("stage", ["new", "established"])
def test_v4_defaults_enable_both_features_without_changing_v3(business, stage):
    raw = current(business, stage)
    old = bundle.compile_bundle(raw, "client")
    raw["policy_name"] = f"{business}_{stage}_v4"
    plan = bundle.compile_bundle(raw, "client")
    executor.validate_plan(plan)
    for campaign in plan["campaigns"]:
        flags = {r["Option"]: r["Value"]
                 for r in campaign["campaign"]["UnifiedCampaign"]["Settings"]}
        assert flags["ALTERNATIVE_TEXTS_ENABLED"] == "YES"
        assert all(g.get("neuro_ad") for g in campaign["groups"])
    assert sum(a["rule"] == "ads.neuro_ad" for a in plan["required_manual_actions"]) == sum(
        len(c["groups"]) for c in plan["campaigns"])
    assert all(not g.get("neuro_ad") for c in old["campaigns"] for g in c["groups"])
    assert old["policy_snapshot"]["alternative_texts_default"] is False


def test_explicit_creative_opt_out_is_preserved_and_neuro_never_enters_api_payload():
    raw = current()
    raw["policy_name"] = "services_b2b_new_v4"
    raw["settings"] = {"ALTERNATIVE_TEXTS_ENABLED": False}
    for channel in raw["channels"].values():
        for group in channel["groups"]:
            group["neuro_ad"] = False
    plan = bundle.compile_bundle(raw, "client")
    assert not any(a["rule"] == "ads.neuro_ad" for a in plan["required_manual_actions"])
    assert all(not g.get("neuro_ad") for c in plan["campaigns"] for g in c["groups"])
    raw = network_source()
    plan = repair.normalize(raw, "client")
    assert plan["new_groups"][0]["neuro_ad"]["href"] == (
        raw["new_groups"][0]["semantic"]["landing_url"])
    assert "neuro_ad" not in plan["new_groups"][0]["ad_group"]
    assert all(set(a) == {"ResponsiveAd"} for a in plan["new_groups"][0]["ads"])
    raw["new_groups"][0]["neuro_ad"] = False
    assert repair.normalize(raw, "client")["plan_hash"] != plan["plan_hash"]


@pytest.mark.parametrize("consider", [False, True])
def test_full_week_and_full_holidays_compile_to_native_schedule(consider):
    assert bundle._time_targeting({
        "days": list(range(1, 8)), "hours": list(range(24)),
        "consider_working_weekends": consider,
        "holidays": {"suspend": False, "start_hour": 0, "end_hour": 24},
    }) is None


@pytest.mark.parametrize("holidays", [
    {"suspend": True}, {"suspend": False, "start_hour": 9, "end_hour": 19},
    {"suspend": False, "start_hour": 0, "end_hour": 24, "bid_percent": 120},
])
def test_holiday_restrictions_and_bid_adjustments_are_not_erased(holidays):
    assert bundle._time_targeting({"holidays": holidays}) is not None


def test_campaign_repair_resets_schedule_and_merges_autotext_setting_with_other_changes():
    api = NetworkAPI()
    typed = api.rows["campaigns"][0]["UnifiedCampaign"]
    typed["Settings"] = [{"Option": "ALTERNATIVE_TEXTS_ENABLED", "Value": "NO"},
                         {"Option": "ENABLE_SITE_MONITORING", "Value": "YES"}]
    old_strategy = deepcopy(typed["BiddingStrategy"])
    plan = repair.normalize({"campaigns": [{"id": 10, "schedule": "always_on",
        "alternative_texts_enabled": True, "priority_goals": []}]}, "client")
    change = plan["campaigns"][0]
    assert change["TimeTargeting"]["Schedule"]["Items"] == []
    assert change["TimeTargeting"]["HolidaysSchedule"] is None
    assert change["UnifiedCampaign"]["PriorityGoals"] is None
    result = run(repair.apply(api, plan))
    check = run(repair.readback(api, plan, before=result["preflight"]["before"]))
    assert check["verified"], check
    assert api.rows["campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"] == old_strategy
    assert not result["activated"]
    for field in ("settings", "holiday"):
        saved = deepcopy(api.rows)
        if field == "settings":
            api.rows["campaigns"][0]["UnifiedCampaign"]["Settings"].pop()
        else:
            api.rows["campaigns"][0]["TimeTargeting"]["HolidaysSchedule"] = {
                "SuspendOnHolidays": "YES"}
        assert not run(repair.readback(api, plan, before=result["preflight"]["before"]))["verified"]
        api.rows = saved


def test_ui_neuro_actions_bind_one_per_created_group():
    raw = current()
    raw["policy_name"] = "services_b2b_new_v4"
    plan = bundle.compile_bundle(raw, "client")
    results = [{"plan_index": 0, "id": "10", "channel": "search", "groups": [
        {"plan_index": 0, "id": "20", "ads": [{"Id": "30"}, {"Errors": [{"Code": 1}]}]},
    ]}]
    actions = creative.executed_actions(plan, results)
    neuro = [a for a in actions if a["rule"] == "ads.neuro_ad"]
    assert len(neuro) == 1 and neuro[0]["group_id"] == "20"
    assert "ad_id" not in neuro[0]
    assert group_append.manual_actions(plan.get("new_groups", []), {}) == []


@pytest.mark.parametrize("with_keywords", [False, True])
def test_network_explicit_autotargeting_and_criteria_counts(with_keywords):
    raw = network_source()
    group = raw["new_groups"][0]
    group["autotargeting"] = {}
    if not with_keywords:
        group["keywords"] = []
        group["semantic"]["candidate_ids"] = []
        raw["semantic_plan"]["candidates"] = []
    plan = repair.normalize(raw, "client")
    assert plan["summary"]["new_autotargetings"] == 1
    assert plan["summary"]["new_keywords"] == int(with_keywords)
    api = NetworkAPI()
    result = run(repair.apply(api, plan))
    assert result["status"] == "complete"
    assert run(repair.readback(api, plan, before=result["preflight"]["before"],
                               added=result["added"]))["verified"]


@pytest.mark.parametrize("field,value", [
    ("alternative_texts_enabled", "YES"), ("alternative_texts_enabled", 1),
    ("schedule", None), ("schedule", {"hours": list(range(24))}),
])
def test_campaign_settings_repair_rejects_ambiguous_input(field, value):
    with pytest.raises(ValueError):
        repair.normalize({"campaigns": [{"id": 10, field: value}]}, "client")


@pytest.mark.asyncio
async def test_neuro_landing_is_in_http_preflight_inventory(monkeypatch):
    from yadirect_mcp import link_checks

    raw = current()
    raw["policy_name"] = "services_b2b_new_v4"
    plan = bundle.compile_bundle(raw, "client")
    # Exercise a group without a normal ad Href, as in a feed-based product ad.
    group = plan["campaigns"][0]["groups"][0]
    for ad in group["ads"]:
        ad["ResponsiveAd"].pop("Href")
    group["action_buttons"] = [None] * len(group["ads"])
    group["neuro_ad"]["href"] = "https://example.test/neuro-source"
    result = await link_checks.check_plan(NetworkAPI(), plan)
    assert {p["device"] for p in result["pages"] if p["base_url"] == group["neuro_ad"]["href"]} == {
        "desktop", "mobile"}
