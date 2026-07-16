"""Тесты preview/confirmation и связки ID при создании кампании с нуля."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from yadirect_mcp import campaign_setup


def plan():
    campaign = {
        "Name": "Поиск | Москва",
        "StartDate": (date.today() + timedelta(days=1)).isoformat(),
        "TextCampaign": {
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            }
        },
    }
    groups = [
        {
            "Name": "Услуга А",
            "RegionIds": [213],
            "Ads": [
                {
                    "TextAd": {
                        "Title": "Услуга А в Москве",
                        "Text": "Оставьте заявку на сайте",
                        "Href": "https://example.test/a",
                        "Mobile": "NO",
                    }
                }
            ],
            "Keywords": ["заказать услугу а", {"Keyword": "услуга а цена"}],
        },
        {
            "Name": "Услуга Б",
            "RegionIds": [213],
            "Ads": [
                {
                    "TextAd": {
                        "Title": "Услуга Б",
                        "Text": "Узнайте стоимость онлайн",
                        "Href": "https://example.test/b",
                        "Mobile": "YES",
                    }
                }
            ],
            "Keywords": [],
        },
    ]
    return campaign, groups


class FakeApi:
    def __init__(self):
        self.calls = []

    async def call(self, service, method, params, *, client_login):
        self.calls.append((service, method, params, client_login))
        ids = {
            "campaigns": [101],
            "adgroups": [201, 202],
            "ads": [301, 302],
            "keywords": [401, 402],
        }[service]
        return {"AddResults": [{"Id": value} for value in ids]}


def test_preview_normalizes_keywords_and_requires_confirmation():
    campaign, groups = plan()
    out = campaign_setup.preview("client", campaign, groups)

    assert out["executed"] is False
    assert out["summary"] == {
        "campaigns": 1, "ad_groups": 2, "ads": 2, "keywords": 2
    }
    assert out["ad_groups"][0]["Keywords"][0] == {"Keyword": "заказать услугу а"}
    assert out["confirmation_required"] == "CREATE CAMPAIGN client"
    # Нормализация не меняет входной объект вызывающей стороны.
    assert groups[0]["Keywords"][0] == "заказать услугу а"


@pytest.mark.asyncio
async def test_apply_links_created_ids_without_activating():
    campaign, groups = plan()
    api = FakeApi()
    out = await campaign_setup.apply(api, "client", campaign, groups)

    assert [call[0] for call in api.calls] == [
        "campaigns", "adgroups", "ads", "keywords"
    ]
    group_payloads = api.calls[1][2]["AdGroups"]
    assert [item["CampaignId"] for item in group_payloads] == [101, 101]
    assert all("Ads" not in item and "Keywords" not in item for item in group_payloads)
    ad_payloads = api.calls[2][2]["Ads"]
    assert [item["AdGroupId"] for item in ad_payloads] == [201, 202]
    keyword_payloads = api.calls[3][2]["Keywords"]
    assert [item["AdGroupId"] for item in keyword_payloads] == [201, 201]
    assert out["status"] == "complete"
    assert out["campaign_id"] == 101
    assert out["activated"] is False


@pytest.mark.asyncio
async def test_group_item_error_is_reported_as_partial_and_children_are_skipped():
    campaign, groups = plan()

    class PartialApi(FakeApi):
        async def call(self, service, method, params, *, client_login):
            self.calls.append((service, method, params, client_login))
            if service == "campaigns":
                return {"AddResults": [{"Id": 101}]}
            if service == "adgroups":
                return {"AddResults": [
                    {"Id": 201},
                    {"Errors": [{"Code": 1, "Message": "bad group"}]},
                ]}
            if service == "ads":
                return {"AddResults": [{"Id": 301}]}
            if service == "keywords":
                return {"AddResults": [{"Id": 401}, {"Id": 402}]}
            raise AssertionError(service)

    out = await campaign_setup.apply(PartialApi(), "client", campaign, groups)
    assert out["status"] == "partial"
    assert out["summary"]["ad_groups"] == {"requested": 2, "created": 1}
    assert out["summary"]["ads"] == {"requested": 2, "created": 1}
    assert out["groups"][1]["errors"][0]["Message"] == "bad group"


@pytest.mark.asyncio
async def test_fatal_child_call_keeps_created_parent_ids():
    campaign, groups = plan()

    class FailingAdsApi(FakeApi):
        async def call(self, service, method, params, *, client_login):
            if service == "ads":
                raise RuntimeError("transport failed")
            return await super().call(service, method, params, client_login=client_login)

    out = await campaign_setup.apply(FailingAdsApi(), "client", campaign, groups)
    assert out["status"] == "partial"
    assert out["campaign_id"] == 101
    assert [group["id"] for group in out["groups"]] == [201, 202]
    assert out["fatal_error"] == {
        "stage": "ads.add", "message": "transport failed"
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c, g: c.__setitem__("StartDate", "2020-01-01"), "в прошлом"),
        (lambda c, g: g[0].__setitem__("CampaignId", 9), "CampaignId"),
        (lambda c, g: g[0]["Ads"][0].__setitem__("AdGroupId", 9), "AdGroupId"),
    ],
)
def test_validation_blocks_unsafe_parent_ids_and_past_start(mutate, message):
    campaign, groups = plan()
    mutate(campaign, groups)
    with pytest.raises(ValueError, match=message):
        campaign_setup.normalize_plan(campaign, groups)
