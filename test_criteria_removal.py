"""Offline inventory and write-scope tests; no external calls."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from yadirect_mcp import criteria_removal as removal
from yadirect_mcp import repair


@pytest.fixture
def env(monkeypatch):
    data = {
        "campaigns": [{"id": 1, "state": "OFF", "status": "DRAFT",
                       "type": "UNIFIED_CAMPAIGN", "budget": 100}],
        "groups": [{"id": 2, "campaign_id": 1, "region_ids": [25]}],
        "ads": [{"id": 3, "campaign_id": 1, "ad_group_id": 2, "text": "Keep"}],
        "keywords": [{"id": i, "campaign_id": 1, "ad_group_id": 2, "keyword": k}
                     for i, k in [(4, "remove"), (5, "keep"), (6, "---autotargeting")]],
        "items": [{"id": 7, "campaign_id": 1, "type": "REGIONAL_ADJUSTMENT"},
                  {"id": 8, "campaign_id": 1, "type": "DEMOGRAPHICS_ADJUSTMENT"}],
    }
    for module, method, key in [(removal.campaigns, "read_settings", "campaigns"),
                                (removal.adgroups, "read", "groups"),
                                (removal.ads, "read", "ads"),
                                (removal.keywords, "read", "keywords"),
                                (removal.account, "_bid_modifiers", "items")]:
        async def read(*a, key=key, **kw):
            return {key: deepcopy(data[key]), "truncated": data.get("truncated", False)}
        monkeypatch.setattr(module, method, read)
    plan = repair.normalize({"remove_criteria": {"campaign_ids": [1], "keyword_ids": [4],
                                                "regional_modifier_ids": [7]}}, "client")
    api = AsyncMock()
    api.call_v501.side_effect = lambda service, method, params, **kw: {
        "DeleteResults": [{"Id": i} for i in params["SelectionCriteria"]["Ids"]]}
    return data, plan, api


@pytest.mark.asyncio
async def test_exact_removal_and_preservation(env):
    data, plan, api = env
    preview = await repair.preflight(api, plan)
    result = await repair.apply(api, plan, expected_preflight=preview)
    assert result["status"] == "complete" and not result["activated"]
    assert [(c.args[0], c.args[1]) for c in api.call_v501.call_args_list] == [
        ("keywords", "delete"), ("bidmodifiers", "delete")]
    data["keywords"] = data["keywords"][1:]
    data["items"] = data["items"][1:]
    assert (await repair.readback(api, plan, before=preview["before"]))["verified"]
    data["campaigns"][0]["budget"] = 101
    assert not (await repair.readback(api, plan, before=preview["before"]))["verified"]


@pytest.mark.parametrize("fault", ["active", "foreign", "duplicate", "truncated", "auto",
                                  "last", "demographic", "missing", "changed"])
@pytest.mark.asyncio
async def test_fail_closed(env, fault):
    data, plan, api = env
    preview = await repair.preflight(api, plan)
    if fault == "active":
        data["campaigns"][0]["state"] = "ON"
    elif fault == "foreign":
        data["keywords"][0]["campaign_id"] = 99
    elif fault == "duplicate":
        data["keywords"].append(deepcopy(data["keywords"][0]))
    elif fault == "truncated":
        data["truncated"] = True
    elif fault == "auto":
        plan["remove_criteria"]["keyword_ids"] = [6]
    elif fault == "last":
        plan["remove_criteria"]["keyword_ids"] = [4, 5]
    elif fault == "demographic":
        plan["remove_criteria"]["regional_modifier_ids"] = [8]
    elif fault == "missing":
        plan["remove_criteria"]["keyword_ids"] = [99]
    else:
        data["ads"][0]["text"] = "Changed"
    with pytest.raises(ValueError):
        await repair.apply(api, plan, expected_preflight=preview)
    api.call_v501.assert_not_called()


@pytest.mark.asyncio
async def test_partial_does_not_continue(env):
    _, plan, api = env
    preview = await repair.preflight(api, plan)
    api.call_v501.side_effect = None
    api.call_v501.return_value = {"DeleteResults": [{"Errors": [{"Code": 1}]}]}
    assert (await repair.apply(api, plan, expected_preflight=preview))["status"] == "partial"
    assert api.call_v501.call_count == 1


@pytest.mark.parametrize("value", [[], [1, 1], [0], [True]])
def test_bad_scope(value):
    with pytest.raises(ValueError):
        repair.normalize({"remove_criteria": {"campaign_ids": value, "keyword_ids": [4],
                                             "regional_modifier_ids": []}}, "client")
