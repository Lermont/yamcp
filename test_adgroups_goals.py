"""Чтение групп v501 и каталога целей GetStatGoals."""

from __future__ import annotations

import asyncio

import pytest

from yadirect_mcp import adgroups, goals


class FakeApi:
    def __init__(self, v501=None, v4=None):
        self.v501 = v501 or {}
        self.v4 = v4
        self.calls = []

    async def call_v501(self, service, method, params, *, client_login=None):
        self.calls.append(("v501", service, method, params, client_login))
        value = self.v501.get(service, {})
        return value(params) if callable(value) else value

    async def call_v4(self, method, param=None):
        self.calls.append(("v4", method, param))
        return self.v4


def read_groups(api, **kwargs):
    return asyncio.run(adgroups.read(api, "client", **kwargs))


def test_campaign_ids_are_chunked_for_adgroups_get():
    api = FakeApi({"adgroups": {"AdGroups": []}})
    read_groups(api, campaign_ids=list(range(1, 26)))
    sent = [
        call[3]["SelectionCriteria"]["CampaignIds"]
        for call in api.calls
        if call[1] == "adgroups"
    ]
    assert [len(chunk) for chunk in sent] == [10, 10, 5]


def test_ad_group_shape_preserves_regions_negatives_and_tracking():
    api = FakeApi({"adgroups": {"AdGroups": [{
        "Id": 5,
        "CampaignId": 7,
        "Name": "Группа",
        "RegionIds": [213, -219],
        "NegativeKeywords": {"Items": ["работа"]},
        "NegativeKeywordSharedSetIds": {"Items": [9]},
        "TrackingParams": "?group=5",
        "UnifiedAdGroup": {"OfferRetargeting": "NO"},
    }]}})
    item = read_groups(api, campaign_ids=[7])["groups"][0]
    assert item["region_ids"] == [213, -219]
    assert item["negative_keywords"] == ["работа"]
    assert item["negative_keyword_shared_set_ids"] == [9]
    assert item["tracking_params"] == "?group=5"


def test_auto_campaign_scan_is_bounded_and_reported():
    total = adgroups.MAX_AUTO_CAMPAIGNS + 3
    api = FakeApi({
        "campaigns": {"Campaigns": [{"Id": i + 1} for i in range(total)]},
        "adgroups": {"AdGroups": []},
    })
    result = read_groups(api)
    assert result["campaigns_total"] == total
    assert result["campaigns_scanned"] == adgroups.MAX_AUTO_CAMPAIGNS
    assert result["truncated"] is True


@pytest.mark.parametrize("limit", [0, adgroups.MAX_LIMIT + 1])
def test_groups_validate_limit(limit):
    api = FakeApi()
    with pytest.raises(ValueError, match="limit"):
        read_groups(api, campaign_ids=[1], limit=limit)
    assert api.calls == []


def test_goal_catalog_calls_documented_v4_shape():
    api = FakeApi(v4=[{"GoalID": 11, "Name": "Отправка формы"}])
    result = asyncio.run(goals.read(api, 123))
    assert api.calls == [("v4", "GetStatGoals", {"CampaignID": 123})]
    assert result["goals"] == [{"id": 11, "name": "Отправка формы"}]
    assert len(result["limitations"]) == 2


def test_goal_catalog_rejects_invalid_id_without_api_call():
    api = FakeApi()
    with pytest.raises(ValueError, match="положительным"):
        asyncio.run(goals.read(api, 0))
    assert api.calls == []
