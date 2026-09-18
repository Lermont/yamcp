"""Guarded repair plans for existing UnifiedCampaign objects."""

from __future__ import annotations

import asyncio

import pytest

from yadirect_mcp import repair


@pytest.fixture(autouse=True)
def offline_links(monkeypatch):
    async def check(pages):
        for page in pages:
            page["site_check"] = {"ok": True, "checked": True}
        return pages
    monkeypatch.setattr(repair.link_checks, "check_pages", check)


def source():
    return {
        "campaigns": [{"id": 10, "negative_keywords": ["бесплатно", "вакансия"]}],
        "ads": [{
            "id": 20,
            "titles": [
                "Профессиональная услуга для развития вашего бизнеса",
                "Комплексное решение задачи для вашей компании",
                "Консультация и расчёт проекта для вашего бизнеса",
            ],
            "texts": [
                "Первый вариант текста",
                "Второй вариант текста",
                "Третий вариант текста",
            ],
            "href": "https://example.test/catalog/",
            "sitelink_set_id": 41,
            "ad_extension_ids": [51, 52],
        }],
        "autotargetings": [{
            "id": 30,
            "categories": {"exact": True, "narrow": True},
            "brand_options": {"without_brands": True},
        }],
    }


class FakeApi:
    def __init__(self):
        self.calls = []

    async def call_v501(self, service, method, params, *, client_login=None):
        if method == "get" and service == "ads":
            ad = source()["ads"][0]
            return {"Ads": [{"Id": i, "CampaignId": 10, "AdGroupId": 11,
                             "Type": "RESPONSIVE_AD", "State": "OFF",
                             "Status": "DRAFT",
                             "ResponsiveAd": {"Titles": ad["titles"], "Texts": ad["texts"],
                                              "Href": ad["href"], "SitelinkSetId": 41,
                                              "AdExtensions": [{"AdExtensionId": 51},
                                                               {"AdExtensionId": 52}]}}
                            for i in params["SelectionCriteria"]["Ids"]]}
        if method == "get" and service == "campaigns":
            return {"Campaigns": [{"Id": i, "Type": "UNIFIED_CAMPAIGN", "State": "OFF",
                                   "UnifiedCampaign": {
                                       "TrackingParams": "utm_campaign={campaign_id}"}}
                                  for i in params["SelectionCriteria"]["Ids"]]}
        if method == "get" and service == "adgroups":
            return {"AdGroups": [{"Id": i, "CampaignId": 10, "RegionIds": [213],
                                  "TrackingParams": "utm_content={ad_id}"}
                                 for i in params["SelectionCriteria"]["Ids"]]}
        if method == "get" and service == "keywords":
            return {"Keywords": [{"Id": i, "CampaignId": 10, "AdGroupId": 11,
                                  "Keyword": "---autotargeting", "State": "OFF"}
                                 for i in params["SelectionCriteria"]["Ids"]]}
        if service == "adextensions" and method == "get":
            return {"AdExtensions": [{"Id": i, "State": "ON"}
                                     for i in params["SelectionCriteria"]["Ids"]]}
        if service == "sitelinks" and method == "get":
            return {"SitelinksSets": [{"Id": i, "Sitelinks": [
                {"Href": "https://example.test/info"}] * 8}
                                     for i in params["SelectionCriteria"]["Ids"]]}
        self.calls.append((service, method, params, client_login))
        rows = next(iter(params.values()))
        return {"UpdateResults": [{"Id": row["Id"]} for row in rows]}


def test_repair_plan_is_hashed_and_uses_safe_autotargeting_defaults():
    plan = repair.normalize(source(), "client")
    assert len(plan["plan_hash"]) == 64
    assert plan["ads"][0]["ResponsiveAd"]["Titles"][0] == (
        "Профессиональная услуга для развития вашего бизнеса"
    )
    assert plan["ads"][0]["ResponsiveAd"]["CalloutSetting"] == {
        "AdExtensions": [
            {"AdExtensionId": 51, "Operation": "SET"},
            {"AdExtensionId": 52, "Operation": "SET"},
        ]
    }
    settings = plan["autotargetings"][0]["AutotargetingSettings"]
    assert settings["Categories"]["Broader"] == "NO"
    assert settings["BrandOptions"]["WithCompetitorsBrand"] == "NO"


def test_repair_rejects_incomplete_responsive_ad():
    value = source()
    value["ads"][0]["titles"] = ["Только один"]
    with pytest.raises(ValueError, match="от 3 до 7"):
        repair.normalize(value, "client")


def test_repair_updates_only_declared_services_and_never_resumes():
    api = FakeApi()
    plan = repair.normalize(source(), "client")
    result = asyncio.run(repair.apply(api, plan))
    assert result["status"] == "complete"
    assert result["activated"] is False
    assert [(row[0], row[1]) for row in api.calls] == [
        ("campaigns", "update"),
        ("ads", "update"),
        ("keywords", "update"),
    ]
