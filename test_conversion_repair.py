"""Conversion repairs use only a fake API; no external requests."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from yadirect_mcp import conversion_repair, repair

PLACEMENTS = {k: "YES" if k == "SearchResults" else "NO"
              for k in conversion_repair.PLACEMENTS}


def source():
    return {"campaigns": [{"id": 10, "priority_goals": [{"goal_id": 200, "value": 1000}],
                           "conversion_strategy": {"weekly_budget": 1234.56, "goal_id": 13,
                               "search_placements": PLACEMENTS,
                               "network_mode": "NETWORK_DEFAULT"}}]}


class Api:
    def __init__(self):
        self.writes = []
        self.row = {"Id": 10, "Type": "UNIFIED_CAMPAIGN", "State": "ON",
                    "Name": "Unchanged", "Status": "ACCEPTED", "UnifiedCampaign": {
                        "CounterIds": {"Items": [100]}, "AttributionModel": "AUTO",
                        "BiddingStrategy": {
                            "Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                                       "PlacementTypes": deepcopy(PLACEMENTS),
                                       "WbMaximumClicks": {"WeeklySpendLimit": 1000000000}},
                            "Network": {"BiddingStrategyType": "NETWORK_DEFAULT"}}}}

    async def call_v501(self, service, method, params, *, client_login=None):
        assert service == "campaigns" and client_login == "client"
        if method == "get":
            return {"Campaigns": [deepcopy(self.row)]}
        assert method == "update"
        assert all(g["Operation"] == "SET" for g in
                   params["Campaigns"][0]["UnifiedCampaign"]["PriorityGoals"]["Items"])
        self.writes.append(deepcopy(params))
        self.row["UnifiedCampaign"].update(deepcopy(params["Campaigns"][0]["UnifiedCampaign"]))
        return {"UpdateResults": [{"Id": 10}]}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(repair.goals, "read", AsyncMock(return_value={
        "goals": [{"id": 200, "name": "Successful request"}], "count": 1,
    }))


@pytest.mark.asyncio
async def test_conversion_apply_readback_and_changed_state_guard():
    api = Api()
    plan = repair.normalize(source(), "client")
    checked = await repair.preflight(api, plan)
    api.row["Name"] = "Someone changed it"
    with pytest.raises(ValueError, match="после preview"):
        await repair.apply(api, plan, expected_preflight=checked)
    assert not api.writes
    api.row["Name"] = "Unchanged"
    result = await repair.apply(api, plan, expected_preflight=checked)
    assert result["status"] == "complete" and len(api.writes) == 1
    assert api.row["State"] == "ON"
    params = api.row["UnifiedCampaign"]["BiddingStrategy"]["Search"]["WbMaximumConversionRate"]
    params["BudgetType"] = "WEEKLY_BUDGET"
    params["BidCeiling"] = None
    assert (await repair.readback(api, plan, before=checked["before"]))["verified"]
    params["BidCeiling"] = 5000000
    assert not (await repair.readback(api, plan, before=checked["before"]))["verified"]
    params["BidCeiling"] = None
    params["GoalId"] = 999
    assert not (await repair.readback(api, plan, before=checked["before"]))["verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["counter", "package", "placements", "network", "goals"])
async def test_preflight_rejects_unverified_or_unrelated_changes(bad):
    api = Api()
    raw = source()
    unified = api.row["UnifiedCampaign"]
    if bad == "counter":
        unified["CounterIds"] = None
    elif bad == "package":
        unified["PackageBiddingStrategy"] = {"StrategyId": 456}
    elif bad == "placements":
        unified["BiddingStrategy"]["Search"]["PlacementTypes"]["Maps"] = "YES"
    elif bad == "network":
        unified["BiddingStrategy"]["Network"]["BiddingStrategyType"] = "SERVING_OFF"
    else:
        raw["campaigns"][0]["priority_goals"][0]["goal_id"] = 999
    with pytest.raises(ValueError):
        await repair.preflight(api, repair.normalize(raw, "client"))
    assert not api.writes


@pytest.mark.parametrize("change", [
    {"weekly_budget": 0}, {"weekly_budget": -1}, {"weekly_budget": "nan"},
    {"goal_id": 999}, {"target_cpa": 500}, {"network_mode": "WB_MAXIMUM_CLICKS"},
])
def test_invalid_contract_and_hash_binding(change):
    raw = source()
    raw["campaigns"][0]["conversion_strategy"].update(change)
    with pytest.raises(ValueError):
        repair.normalize(raw, "client")
    original = repair.normalize(source(), "client")["plan_hash"]
    raw = source()
    raw["campaigns"][0]["conversion_strategy"]["weekly_budget"] = 1000
    assert repair.normalize(raw, "client")["plan_hash"] != original


def test_empty_goals_rejected():
    raw = source()
    raw["campaigns"][0]["priority_goals"] = []
    with pytest.raises(ValueError):
        repair.normalize(raw, "client")


def network_case():
    api = Api()
    placements = dict.fromkeys(PLACEMENTS, "NO")
    api.row["UnifiedCampaign"]["BiddingStrategy"] = {
        "Search": {"BiddingStrategyType": "SERVING_OFF", "PlacementTypes": placements},
        "Network": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                    "WbMaximumClicks": {"WeeklySpendLimit": 1000000000}}}
    raw = source()
    raw["campaigns"][0]["conversion_strategy"].update(
        channel="network", search_placements=placements,
        network_mode="WB_MAXIMUM_CONVERSION_RATE")
    return api, raw


@pytest.mark.asyncio
async def test_network_conversion_guard_apply_and_readback():
    api, raw = network_case()
    api.row.update(State="OFF", Status="DRAFT")
    plan = repair.normalize(raw, "client")
    checked = await repair.preflight(api, plan)
    api.row["Name"] = "Concurrent change"
    with pytest.raises(ValueError, match="после preview"):
        await repair.apply(api, plan, expected_preflight=checked)
    assert not api.writes
    api.row["Name"] = "Unchanged"
    result = await repair.apply(api, plan, expected_preflight=checked)
    assert result["status"] == "complete" and len(api.writes) == 1
    assert api.row["State"] == "OFF" and api.row["Status"] == "DRAFT"
    strategy = api.row["UnifiedCampaign"]["BiddingStrategy"]
    assert strategy["Search"]["BiddingStrategyType"] == "SERVING_OFF"
    params = strategy["Network"]["WbMaximumConversionRate"]
    assert params == {"GoalId": 13, "WeeklySpendLimit": 1234560000}
    params["BudgetType"] = "WEEKLY_BUDGET"
    params["BidCeiling"] = None
    assert (await repair.readback(api, plan, before=checked["before"]))["verified"]
    for field, bad in [("GoalId", 999), ("WeeklySpendLimit", 999),
                       ("BudgetType", "DAILY_BUDGET"), ("BidCeiling", 5000000)]:
        original = params[field]
        params[field] = bad
        assert not (await repair.readback(api, plan, before=checked["before"]))["verified"]
        params[field] = original


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "search", "network_off", "shared", "placements", "counter", "goals",
])
async def test_network_conversion_rejects_unrelated_or_unverified_changes(bad):
    api, raw = network_case()
    unified = api.row["UnifiedCampaign"]
    if bad == "search":
        unified["BiddingStrategy"]["Search"]["BiddingStrategyType"] = "WB_MAXIMUM_CLICKS"
    elif bad == "network_off":
        unified["BiddingStrategy"]["Network"]["BiddingStrategyType"] = "SERVING_OFF"
    elif bad == "shared":
        unified["BiddingStrategy"]["Network"]["BiddingStrategyType"] = "NETWORK_DEFAULT"
    elif bad == "placements":
        unified["BiddingStrategy"]["Network"]["PlacementTypes"] = {"Network": "YES"}
    elif bad == "counter":
        unified["CounterIds"] = None
    else:
        raw["campaigns"][0]["priority_goals"][0]["goal_id"] = 999
    with pytest.raises(ValueError):
        await repair.preflight(api, repair.normalize(raw, "client"))
    assert not api.writes


@pytest.mark.parametrize("change", [
    {"channel": "invalid"}, {"network_mode": "NETWORK_DEFAULT"},
    {"search_placements": PLACEMENTS}, {"target_cpa": 500},
])
def test_network_conversion_rejects_invalid_contract(change):
    _, raw = network_case()
    raw["campaigns"][0]["conversion_strategy"].update(change)
    with pytest.raises(ValueError):
        repair.normalize(raw, "client")


@pytest.mark.asyncio
@pytest.mark.parametrize("counter", [100, 101])
async def test_empty_stats_catalog_fallback_requires_same_counter(monkeypatch, counter):
    api = Api()
    read = repair.goals.read

    async def catalog(unused_api, cid):
        if cid == 10:
            return {"goals": [], "count": 0}
        return await read(unused_api, cid)

    original = repair.campaigns.read_settings

    async def inventory(unused_api, login, **kwargs):
        if kwargs.get("campaign_ids"):
            return await original(unused_api, login, **kwargs)
        return {"campaigns": [{"id": 11, "state": "ON", "counter_ids": [counter]}]}

    monkeypatch.setattr(repair.goals, "read", catalog)
    monkeypatch.setattr(repair.campaigns, "read_settings", inventory)
    plan = repair.normalize(source(), "client")
    if counter == 100:
        assert (await repair.preflight(api, plan))["status"] == "PASS"
    else:
        with pytest.raises(ValueError, match="verified campaign goals"):
            await repair.preflight(api, plan)
