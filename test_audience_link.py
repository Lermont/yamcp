"""Audience-link repair safety tests; all external calls are fake."""
from copy import deepcopy

import pytest

from yadirect_mcp import audience_link, repair


class Api:
    def __init__(self):
        self.data = {
            "adgroups": {"AdGroups": [{"Id": 1, "CampaignId": 2, "Type": "UNIFIED_AD_GROUP",
                "Status": "DRAFT", "UnifiedAdGroup": {"OfferRetargeting": "NO"}}]},
            "campaigns": {"Campaigns": [{"Id": 2, "Type": "UNIFIED_CAMPAIGN", "State": "OFF",
                "UnifiedCampaign": {"BiddingStrategy": {
                    "Search": {"BiddingStrategyType": "SERVING_OFF"},
                    "Network": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS"}}}}]},
            "retargetinglists": {"RetargetingLists": [{"Id": 3, "Type": "RETARGETING",
                "IsAvailable": "YES", "Scope": "FOR_TARGETS_AND_ADJUSTMENTS",
                "Rules": [{"Operator": "ANY", "Arguments": [
                    {"ExternalId": 4, "MembershipLifeSpan": 540}]}]}]},
            "keywords": {"Keywords": [{"Id": 5, "Keyword": "---autotargeting",
                                       "State": "SUSPENDED"}]},
            "audiencetargets": {"AudienceTargets": []},
        }
        self.catalog = [{"GoalID": 4, "Type": "segment", "Login": "client"}]
        self.writes = []
        self.fail = False

    async def call_v4(self, method, param):
        assert method == "GetRetargetingGoals" and param == {"Logins": ["client"]}
        return deepcopy(self.catalog)

    async def call_v501(self, service, method, params, *, client_login):
        assert client_login == "client"
        if method == "get":
            return deepcopy(self.data[service])
        self.writes.append((service, method, deepcopy(params)))
        assert service == "audiencetargets" and method == "add"
        if self.fail:
            return {"AddResults": [{"Errors": [{"Code": 6000}]}]}
        self.data["audiencetargets"]["AudienceTargets"] = [
            {"Id": 6, **params["AudienceTargets"][0], "State": "ON"}]
        return {"AddResults": [{"Id": 6}]}


def plan():
    return repair.normalize({"audience_link": {
        "ad_group_id": 1, "retargeting_list_id": 3, "segment_id": 4}}, "client")


@pytest.mark.asyncio
async def test_link_preserves_snapshot_and_readback_and_refuses_repeat():
    api, p = Api(), plan()
    checked = await repair.preflight(api, p)
    result = await repair.apply(api, p, expected_preflight=checked)
    assert result["status"] == "complete" and result["activated"] is False
    assert (await repair.readback(api, p, before=checked["before"],
                                  added=result["added"]))["verified"]
    with pytest.raises(ValueError, match="already"):
        await repair.apply(api, p, expected_preflight=checked)
    assert len(api.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["owner", "running", "keyword", "goal", "rules", "unavailable",
                                "truncated", "changed"])
async def test_fail_closed_before_write(bad):
    api, p = Api(), plan()
    checked = await repair.preflight(api, p)
    if bad == "owner":
        api.data["adgroups"]["AdGroups"] = []
    elif bad == "running":
        api.data["campaigns"]["Campaigns"][0]["State"] = "ON"
    elif bad == "keyword":
        api.data["keywords"]["Keywords"][0]["State"] = "ON"
    elif bad == "goal":
        api.catalog[0]["Type"] = "goal"
    elif bad == "rules":
        api.data["retargetinglists"]["RetargetingLists"][0]["Rules"][0]["Operator"] = "NONE"
    elif bad == "unavailable":
        api.data["retargetinglists"]["RetargetingLists"][0]["IsAvailable"] = "NO"
    elif bad == "truncated":
        api.data["audiencetargets"]["LimitedBy"] = 10000
    else:
        api.data["adgroups"]["AdGroups"][0]["Status"] = "ACCEPTED"
    with pytest.raises(ValueError):
        await repair.apply(api, p, expected_preflight=checked)
    assert not api.writes


@pytest.mark.asyncio
async def test_partial_add_retains_error_and_never_retries():
    api, p = Api(), plan()
    api.fail = True
    result = await repair.apply(api, p, expected_preflight=await repair.preflight(api, p))
    assert result["status"] == "partial"
    assert result["added"]["audience_targets"][0]["Errors"]
    assert len(api.writes) == 1


def test_id_binding_and_no_combined_operations():
    p = plan()
    other = audience_link.normalize({
        "ad_group_id": 1, "retargeting_list_id": 30, "segment_id": 4}, "client")
    assert p["plan_hash"] != other["plan_hash"]
    with pytest.raises(ValueError):
        repair.normalize({"audience_link": p["audience_link"], "campaigns": []}, "client")


@pytest.mark.asyncio
async def test_documented_empty_structure_means_no_targets():
    api = Api()
    api.data["audiencetargets"] = {}
    checked = await repair.preflight(api, plan())
    assert checked["before"]["targets"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"AudienceTargets": None}, {"unexpected": []}])
async def test_malformed_nonempty_structure_blocks(bad):
    api = Api()
    api.data["audiencetargets"] = bad
    with pytest.raises(ValueError, match="Incomplete"):
        await repair.preflight(api, plan())
