"""Offline consolidation tests. No credentials or external APIs are used."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from yadirect_mcp import draft_group_merge as mod
from yadirect_mcp import jobs, repair


class Api:
    def __init__(self):
        self.campaign = {"id": 1, "type": "UNIFIED_CAMPAIGN", "state": "OFF", "status": "DRAFT",
                         "bidding_strategy": {"Search": {"PlacementTypes": {"SearchResults": "YES"},
                                                          "WeeklySpendLimit": 1000},
                                              "Network": {"BiddingStrategyType": "SERVING_OFF"}}}
        self.inv = {"groups": [], "ads": [], "keywords": []}
        for gid in (10, 20):
            self.inv["groups"].append({"id": gid, "campaign_id": 1, "name": str(gid),
                "status": "DRAFT", "type": "UNIFIED_AD_GROUP", "subtype": "NONE",
                "offer_retargeting": "NO", "region_ids": [2], "negative_keywords": []})
            self.inv["ads"].append({"id": gid + 1, "ad_group_id": gid, "campaign_id": 1,
                "state": "OFF", "status": "DRAFT", "type": "RESPONSIVE_AD", "href": "https://example.org"})
            self.inv["keywords"].extend([
                {"id": gid + 2, "ad_group_id": gid, "campaign_id": 1, "state": "ON",
                 "keyword": str(gid), "autotargeting": None},
                {"id": gid + 3, "ad_group_id": gid, "campaign_id": 1, "state": "ON",
                 "keyword": "---autotargeting", "autotargeting": {"Exact": "YES"}}])
        self.targets, self.modifiers, self.writes = [], [], []
        self.fail = None
        self.corrupt_add = False

    async def call_v501(self, service, method, params, *, client_login):
        assert client_login == "client"
        if method == "get":
            if service == "bidmodifiers":
                assert params["SelectionCriteria"]["Levels"] == ["CAMPAIGN", "AD_GROUP"]
            collection, rows = ({"audiencetargets": ("AudienceTargets", self.targets),
                                  "bidmodifiers": ("BidModifiers", self.modifiers)})[service]
            return {collection: deepcopy(rows)}
        self.writes.append((service, method, deepcopy(params)))
        if self.fail == (service, method):
            return {}
        if method == "delete":
            ids = params["SelectionCriteria"]["Ids"]
            key = "groups" if service == "adgroups" else service
            self.inv[key] = [r for r in self.inv[key] if r["id"] not in ids]
            if service == "adgroups":
                self.inv["keywords"] = [r for r in self.inv["keywords"]
                                        if r["ad_group_id"] not in ids]
            return {"DeleteResults": [{"Id": x} for x in ids]}
        if service == "keywords" and method == "add":
            rows = [{"id": 100 + i, "ad_group_id": r["AdGroupId"], "campaign_id": 1,
                     "keyword": "wrong" if self.corrupt_add else r["Keyword"],
                     "autotargeting": None, "state": "ON"}
                    for i, r in enumerate(params["Keywords"])]
            self.inv["keywords"].extend(rows)
            return {"AddResults": [{"Id": r["id"]} for r in rows]}
        assert service == "adgroups" and method == "update"
        r = params["AdGroups"][0]
        g = next(g for g in self.inv["groups"] if g["id"] == r["Id"])
        g.update(name=r["Name"], negative_keywords=r["NegativeKeywords"]["Items"])
        return {"UpdateResults": [{"Id": r["Id"]}]}


@pytest.fixture
def setup(monkeypatch):
    api = Api()
    monkeypatch.setattr(mod.campaigns, "read_settings", AsyncMock(
        side_effect=lambda *a, **k: {"campaigns": [deepcopy(api.campaign)]}))
    monkeypatch.setattr(mod.group_append, "inventory", AsyncMock(
        side_effect=lambda *a, **k: deepcopy(api.inv)))
    raw = {"draft_group_merge": {"campaign_id": 1, "group_ids": [10, 20], "keep_group_id": 10,
        "name": "Competitors", "keywords": ["brand bureau", '"[brand com]"'],
        "negative_keywords": ["jobs"], "research_note": "Wordstat reviewed"}}
    return api, repair.normalize(raw, "client"), raw


@pytest.mark.asyncio
async def test_merge_and_independent_readback(setup):
    api, plan, _ = setup
    before = await repair.preflight(api, plan)
    assert not api.writes
    result = await repair.apply(api, plan, expected_preflight=before)
    assert result["status"] == "complete", result
    assert not result["activated"]
    check = await repair.readback(api, plan, before=before["before"], added=result["added"])
    assert check["verified"] and check["counts"] == {"groups": 1, "ads": 1, "manual_keywords": 2}
    assert [(s, m) for s, m, _ in api.writes] == [
        ("keywords", "delete"), ("keywords", "add"), ("adgroups", "update"),
        ("ads", "delete"), ("keywords", "delete"), ("adgroups", "delete")]
    api.campaign["bidding_strategy"]["Search"]["WeeklySpendLimit"] = 2000
    check = await repair.readback(api, plan, before=before["before"], added=result["added"])
    assert not check["verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["on", "accepted", "different_ad", "different_geo",
    "missing_group", "extra_group", "foreign", "audience", "group_modifier", "different_auto"])
async def test_unsafe_inventory_blocks_without_writes(setup, change):
    api, plan, _ = setup
    if change == "on":
        api.campaign["state"] = "ON"
    elif change == "accepted":
        api.inv["ads"][0]["status"] = "ACCEPTED"
    elif change == "different_ad":
        api.inv["ads"][0]["href"] = "https://other.example"
    elif change == "different_geo":
        api.inv["groups"][0]["region_ids"] = [3]
    elif change == "missing_group":
        api.inv["groups"].pop()
    elif change == "extra_group":
        api.inv["groups"].append({**api.inv["groups"][0], "id": 30})
    elif change == "foreign":
        api.inv["ads"][0]["campaign_id"] = 9
    elif change == "audience":
        api.targets = [{"Id": 1}]
    elif change == "group_modifier":
        api.modifiers = [{"Id": 1, "AdGroupId": 10}]
    else:
        api.inv["keywords"][-1]["autotargeting"] = {"Exact": "NO"}
    with pytest.raises(ValueError):
        await repair.preflight(api, plan)
    assert not api.writes


@pytest.mark.asyncio
async def test_changed_snapshot_blocks(setup):
    api, plan, _ = setup
    checked = await repair.preflight(api, plan)
    api.campaign["tracking_params"] = "changed"
    with pytest.raises(ValueError, match="changed"):
        await repair.apply(api, plan, expected_preflight=checked)
    assert not api.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["failed_add", "corrupt_add", "failed_delete"])
async def test_partial_never_replays_and_retains_evidence(setup, mode):
    api, plan, _ = setup
    checked = await repair.preflight(api, plan)
    if mode == "failed_add":
        api.fail = ("keywords", "add")
    elif mode == "failed_delete":
        api.fail = ("ads", "delete")
    else:
        api.corrupt_add = True
    result = await repair.apply(api, plan, expected_preflight=checked)
    assert result["status"] == "partial" and result["updates"] and result["error"]
    assert len(api.inv["groups"]) == 2
    assert not any(s == "adgroups" and m == "delete" for s, m, _ in api.writes)


def test_strict_normalization_and_hash(setup):
    _, plan, raw = setup
    raw["draft_group_merge"]["keywords"].append("another brand")
    assert repair.normalize(raw, "client")["plan_hash"] != plan["plan_hash"]
    raw["draft_group_merge"]["resume"] = True
    with pytest.raises(ValueError):
        repair.normalize(raw, "client")


def test_negative_order_is_not_a_change():
    assert mod.stable({"negative_keywords": ["z", "a"]}) == mod.stable(
        {"negative_keywords": ["a", "z"]})


@pytest.mark.asyncio
async def test_prepared_cleanup_never_recreates_verified_keywords(setup):
    api, plan, raw = setup
    checked = await repair.preflight(api, plan)
    api.fail = ("ads", "delete")
    partial = await repair.apply(api, plan, expected_preflight=checked)
    assert partial["status"] == "partial"
    raw["draft_group_merge"]["prepared_only"] = True
    cleanup = repair.normalize(raw, "client")
    verified = await repair.preflight(api, cleanup)
    api.fail, api.writes = None, []
    result = await repair.apply(api, cleanup, expected_preflight=verified)
    assert result["status"] == "complete", result
    assert [(s, m) for s, m, _ in api.writes] == [
        ("ads", "delete"), ("keywords", "delete"), ("adgroups", "delete")]
    assert result["added"]["reused_keywords"]
    assert (await repair.readback(api, cleanup, before=verified["before"],
                                 added=result["added"]))["verified"]


@pytest.mark.asyncio
async def test_prepared_cleanup_blocks_wrong_target(setup):
    api, _, raw = setup
    raw["draft_group_merge"]["prepared_only"] = True
    with pytest.raises(ValueError, match="Prepared"):
        await repair.preflight(api, repair.normalize(raw, "client"))
    assert not api.writes


@pytest.mark.asyncio
async def test_final_empty_groups_only_native_autotargeting_is_not_deleted(setup):
    api, plan, raw = setup
    checked = await repair.preflight(api, plan)
    api.fail = ("adgroups", "delete")
    partial = await repair.apply(api, plan, expected_preflight=checked)
    assert partial["status"] == "partial"
    assert len(api.inv["ads"]) == 1
    assert all(r["ad_group_id"] == 10 or r["keyword"] == "---autotargeting"
               for r in api.inv["keywords"])
    raw["draft_group_merge"].update(prepared_only=True, empty_groups_only=True)
    final = repair.normalize(raw, "client")
    checked = await repair.preflight(api, final)
    api.fail, api.writes = None, []
    result = await repair.apply(api, final, expected_preflight=checked)
    assert result["status"] == "complete", result
    assert [(s, m) for s, m, _ in api.writes] == [("adgroups", "delete")]
    assert (await repair.readback(api, final, before=checked["before"],
                                 added=result["added"]))["verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response,uncertain", [
    ({"DeleteResults": [{"Id": 10}, {"Id": 20}]}, False),
    ({"DeleteResults": [{"Id": 10}]}, True),
    ({"DeleteResults": [{"Id": 10}, {"Id": 10}]}, True),
    ({"DeleteResults": [{"Id": 10}, {"Id": 99}]}, True)])
async def test_delete_journal_response_validation(tmp_path, response, uncertain):
    class Journal:
        payload = {"events": [], "uncertain": False}

        def event(self, **kw):
            self.payload["events"].append(kw)
    journal = Journal()
    api = AsyncMock()
    api.call_v501.return_value = response
    await jobs.JournalAPI(api, journal).call_v501("keywords", "delete",
        {"SelectionCriteria": {"Ids": [10, 20]}}, client_login="client")
    assert journal.payload["uncertain"] is uncertain


@pytest.mark.asyncio
async def test_unknown_delete_reconciliation_is_ids_scoped():
    api = AsyncMock()
    api.call_v501.return_value = {"Keywords": []}
    result = await jobs.reconcile(api, "keywords", "delete",
                                 {"SelectionCriteria": {"Ids": [10, 20]}}, "client")
    assert not result["resolved"]
    assert api.call_v501.call_args.args[2]["SelectionCriteria"] == {"Ids": [10, 20]}


@pytest.mark.asyncio
@pytest.mark.parametrize("value,valid", [({}, True), ({"LimitedBy": 2}, False),
                                       ({"wrong": []}, False)])
async def test_raw_empty_contract(value, valid):
    api = AsyncMock()
    api.call_v501.return_value = value
    if valid:
        assert await mod._raw(api, "client", "audiencetargets", "AudienceTargets", 1, ["Id"]) == []
    else:
        with pytest.raises(ValueError):
            await mod._raw(api, "client", "audiencetargets", "AudienceTargets", 1, ["Id"])
