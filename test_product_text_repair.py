"""Offline safety tests; external reads and writes are mocked."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from yadirect_mcp import product_text_repair as product
from yadirect_mcp import repair


@pytest.fixture
def setup(monkeypatch):
    ad = {"id": 11, "campaign_id": 1, "ad_group_id": 2, "type": "SHOPPING_AD",
          "state": "ON", "status": "ACCEPTED", "feed_id": 3, "texts": ["Old"],
          "text": "Old", "feed_filter_conditions": [{"Arguments": ["4", "5"]}],
          "sitelink_set_id": 6, "title_sources": [], "text_sources": []}
    ads = {"ads": [ad]}
    campaigns = {"campaigns": [{"id": 1, "type": "UNIFIED_CAMPAIGN", "state": "ON",
                                "bidding_strategy": {"weekly_budget": 1000}}]}
    monkeypatch.setattr(product.ads, "read", AsyncMock(side_effect=lambda *a, **k: deepcopy(ads)))
    monkeypatch.setattr(product.campaigns, "read_settings",
                        AsyncMock(side_effect=lambda *a, **k: deepcopy(campaigns)))
    api = AsyncMock()
    api.call_v501.return_value = {"UpdateResults": [{"Id": 11}]}
    plan = repair.normalize({"product_ad_texts": [{"id": 11, "feed_id": 3,
                                                 "text": "Portable stations"}]}, "client")
    return api, plan, ads, campaigns


@pytest.mark.asyncio
async def test_exact_update_and_independent_readback(setup):
    api, plan, data, campaigns = setup
    preview = await repair.preflight(api, plan)
    api.call_v501.assert_not_called()
    assert preview["updates"] == [{"Id": 11, "ShoppingAd": {
        "DefaultTexts": ["Portable stations"]}}]
    result = await repair.apply(api, plan, expected_preflight=preview)
    assert result["status"] == "complete" and not result["activated"]
    api.call_v501.assert_awaited_once_with("ads", "update", {"Ads": preview["updates"]},
                                          client_login="client")
    assert not (await repair.readback(api, plan, before=preview["before"]))["verified"]
    data["ads"][0].update(text="Portable stations", texts=["Portable stations"])
    assert (await repair.readback(api, plan, before=preview["before"]))["verified"]
    campaigns["campaigns"][0]["bidding_strategy"]["weekly_budget"] = 2000
    assert not (await repair.readback(api, plan, before=preview["before"]))["verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["feed", "type", "duplicate", "missing", "truncated"])
async def test_block_unsafe_snapshot(setup, change):
    api, plan, data, _ = setup
    if change == "feed":
        data["ads"][0]["feed_id"] = 999
    elif change == "type":
        data["ads"][0]["type"] = "RESPONSIVE_AD"
    elif change == "duplicate":
        data["ads"] *= 2
    elif change == "missing":
        data["ads"] = []
    else:
        data["truncated"] = True
    with pytest.raises(ValueError):
        await repair.preflight(api, plan)
    api.call_v501.assert_not_called()


@pytest.mark.asyncio
async def test_changed_preview_and_partial_write(setup):
    api, plan, data, _ = setup
    preview = await repair.preflight(api, plan)
    data["ads"][0]["sitelink_set_id"] = 9
    with pytest.raises(ValueError, match="changed"):
        await repair.apply(api, plan, expected_preflight=preview)
    api.call_v501.assert_not_called()
    preview = await repair.preflight(api, plan)
    api.call_v501.return_value = {"UpdateResults": [{"Errors": [{"Code": 1}]}]}
    assert (await repair.apply(api, plan, expected_preflight=preview))["status"] == "partial"


def test_rejects_extra_fields_and_duplicates():
    row = {"id": 1, "feed_id": 2, "text": "Text"}
    for rows in [[], [row, row], [{**row, "resume": True}], [{**row, "text": "a" * 82}]]:
        with pytest.raises(ValueError):
            repair.normalize({"product_ad_texts": rows}, "client")
