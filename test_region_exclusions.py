"""Offline guards for append-only geography changes; no external API calls."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from yadirect_mcp import region_exclusions as geo
from yadirect_mcp import repair


def test_metrica_tag_patch_is_explicit_and_preserves_other_settings():
    p = repair.normalize({"campaigns": [{"id": 1, "add_metrica_tag": True,
                                        "alternative_texts_enabled": True}]}, "client")
    assert p["campaigns"][0]["UnifiedCampaign"]["Settings"] == [
        {"Option": "ALTERNATIVE_TEXTS_ENABLED", "Value": "YES"},
        {"Option": "ADD_METRICA_TAG", "Value": "YES"}]
    with pytest.raises(ValueError):
        repair.normalize({"campaigns": [{"id": 1, "add_metrica_tag": "YES"}]}, "client")


@pytest.fixture
def setup(monkeypatch):
    data = {"campaigns": [{"id": 1, "state": "OFF", "type": "UNIFIED_CAMPAIGN"}]}
    groups = {"groups": [{"id": 2, "campaign_id": 1, "region_ids": [10, -20],
                          "name": "Keep", "type": "UNIFIED_AD_GROUP"}]}
    monkeypatch.setattr(geo.campaigns, "read_settings",
                        AsyncMock(side_effect=lambda *a, **k: deepcopy(data)))
    monkeypatch.setattr(geo.adgroups, "read",
                        AsyncMock(side_effect=lambda *a, **k: deepcopy(groups)))
    monkeypatch.setattr(geo.regions, "lookup", AsyncMock(side_effect=lambda *a, ids, **k:
        {"resolved": [{"id": i, "parent_id": 10 if i == 30 else None} for i in ids]}))
    api = AsyncMock()
    api.call_v501.return_value = {"UpdateResults": [{"Id": 2}]}
    plan = repair.normalize({"region_exclusions": {"campaign_id": 1, "group_ids": [2],
                                                   "region_ids": [30]}}, "client")
    return api, plan, data, groups


@pytest.mark.asyncio
async def test_exclusions_append_preserve_and_readback(setup):
    api, plan, data, groups = setup
    preview = await repair.preflight(api, plan)
    result = await repair.apply(api, plan, expected_preflight=preview)
    assert result["status"] == "complete"
    assert api.call_v501.call_args.args[2] == {"AdGroups": [{"Id": 2, "RegionIds": [-30, -20, 10]}]}
    groups["groups"][0]["region_ids"].append(-30)
    assert (await repair.readback(api, plan, before=preview["before"]))["verified"]
    groups["groups"][0]["name"] = "Unexpected"
    assert not (await repair.readback(api, plan, before=preview["before"]))["verified"]


@pytest.mark.parametrize("fault", ["active", "foreign", "duplicate", "truncated", "world",
                                  "conflict", "unknown", "outside", "cycle"])
@pytest.mark.asyncio
async def test_exclusion_preflight_blocks_without_writes(setup, fault):
    api, plan, data, groups = setup
    if fault == "active":
        data["campaigns"][0]["state"] = "ON"
    elif fault == "foreign":
        groups["groups"][0]["campaign_id"] = 3
    elif fault == "duplicate":
        groups["groups"].append(deepcopy(groups["groups"][0]))
    elif fault == "truncated":
        groups["truncated"] = True
    elif fault == "world":
        groups["groups"][0]["region_ids"] = [0]
    elif fault == "conflict":
        groups["groups"][0]["region_ids"].append(30)
    elif fault == "outside":
        groups["groups"][0]["region_ids"] = [40]
    elif fault == "cycle":
        geo.regions.lookup.side_effect = lambda *a, ids, **k: {
            "resolved": [{"id": i, "parent_id": 10 if i == 30 else 30} for i in ids]}
    else:
        geo.regions.lookup.side_effect = None
        geo.regions.lookup.return_value = {"unknown_ids": [30]}
    with pytest.raises(ValueError):
        await repair.preflight(api, plan)
    api.call_v501.assert_not_called()


@pytest.mark.asyncio
async def test_changed_snapshot_cannot_apply(setup):
    api, plan, data, groups = setup
    preview = await repair.preflight(api, plan)
    groups["groups"][0]["region_ids"].append(40)
    with pytest.raises(ValueError, match="changed"):
        await repair.apply(api, plan, expected_preflight=preview)
    api.call_v501.assert_not_called()


@pytest.mark.asyncio
async def test_partial_response_is_not_complete(setup):
    api, plan, _, _ = setup
    preview = await repair.preflight(api, plan)
    api.call_v501.return_value = {"UpdateResults": [{"Errors": [{"Code": 1}]}]}
    assert (await repair.apply(api, plan, expected_preflight=preview))["status"] == "partial"
