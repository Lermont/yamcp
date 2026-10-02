"""Read-only keyword inventory including explicit autotargeting settings."""

from __future__ import annotations

import asyncio

import pytest

from yadirect_mcp import keywords


class FakeApi:
    def __init__(self):
        self.params = None

    async def call_v501(self, service, method, params, *, client_login=None):
        assert (service, method, client_login) == ("keywords", "get", "client")
        self.params = params
        return {
            "Keywords": [
                {
                    "Id": 1,
                    "CampaignId": 10,
                    "AdGroupId": 20,
                    "Keyword": "купить стол",
                    "State": "ON",
                },
                {
                    "Id": 2,
                    "CampaignId": 10,
                    "AdGroupId": 20,
                    "Keyword": "---autotargeting",
                    "AutotargetingSettings": {
                        "Categories": {"Exact": "YES", "Broader": "NO"},
                        "BrandOptions": {"WithoutBrands": "YES"},
                    },
                },
            ]
        }


def test_keyword_reader_shapes_autotargeting_and_counts():
    api = FakeApi()
    result = asyncio.run(keywords.read(api, "client", campaign_ids=[10]))
    assert result["count"] == 2
    assert result["manual_count"] == 1
    assert result["autotargeting_count"] == 1
    assert result["keywords"][1]["autotargeting"]["categories"]["Exact"] == "YES"
    assert "WithCompetitorsBrand" in api.params[
        "AutotargetingSettingsBrandOptionsFieldNames"
    ]


def test_keyword_reader_requires_a_filter():
    with pytest.raises(ValueError, match="хотя бы один фильтр"):
        asyncio.run(keywords.read(FakeApi(), "client"))
