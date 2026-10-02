"""Политика, v501-чтение и детерминированный аудит кампаний."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from yadirect_mcp import audit, campaigns, policy, tracking
from yadirect_mcp.client import API_V501_URL, DirectClient
from yadirect_mcp.config import Settings


class FakeApi:
    def __init__(self, response=None):
        self.response = response or {"Campaigns": []}
        self.calls = []

    async def call_v501(self, service, method, params, *, client_login=None):
        self.calls.append((service, method, params, client_login))
        return self.response


def tracking_string() -> str:
    return "?" + "&".join(
        f"{key}={value}" for key, value in policy.TRACKING_PARAMS_V1.items()
    )


def campaign(**overrides):
    required_negatives = sorted(
        {value.lower() for value in policy.load_snapshot("search_negative_keywords")}
        - policy.RISKY_NEGATIVES
    )
    base = {
        "id": 10,
        "name": "Поиск | Москва",
        "type": "UNIFIED_CAMPAIGN",
        "state": "OFF",
        "status": "ACCEPTED",
        "time_targeting": {
            "Schedule": {
                "Items": [{
                    "Days": [1, 2, 3, 4, 5, 6, 7],
                    "Hours": list(range(24)),
                    "BidPercent": 100,
                }]
            }
        },
        "daily_budget_micros": None,
        "negative_keywords": required_negatives,
        "excluded_sites": [],
        "settings": {"ENABLE_AREA_OF_INTEREST_TARGETING": "NO"},
        "counter_ids": [123],
        "priority_goals": [{"GoalId": 7, "Value": 1_000_000}],
        "tracking_params": tracking_string(),
        "bidding_strategy": {
            "Search": {
                "BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                "PlacementTypes": {
                    "SearchResults": "YES",
                    "Maps": "NO",
                    "SearchOrganizationList": "NO",
                },
                "WbMaximumClicks": {"WeeklySpendLimit": 3_000_000_000},
            },
            "Network": {"BiddingStrategyType": "SERVING_OFF"},
        },
    }
    base.update(overrides)
    return base


def findings(result):
    return {row["rule"]: row for row in result["findings"]}


def test_policy_is_copied_and_unknown_name_is_rejected():
    first = policy.get()
    first["budget"]["minimum"] = 1
    assert policy.get()["budget"]["minimum"] == 2000
    assert policy.get()["campaign_structure"]["required_channels"] == ["search"]
    assert policy.get()["campaign_structure"]["optional_channels"] == ["network"]
    with pytest.raises(ValueError, match="Неизвестная политика"):
        policy.get("unknown")


def test_embedded_snapshots_match_manifest_and_are_deduplicated():
    negatives = policy.load_snapshot("search_negative_keywords")
    sites = policy.load_snapshot("network_excluded_sites")
    assert len(negatives) == len(set(negatives)) == 249
    assert len(sites) == len(set(sites)) == 248


def test_negative_readback_accepts_direct_normalization():
    assert policy.negative_phrase_equivalent("second-hand", "second hand")
    assert policy.negative_phrase_equivalent("кто такой", "!кто !такой")
    assert policy.negative_phrase_equivalent("вакансии", "вакансия")
    assert policy.negative_phrase_equivalent("wallpapers", "wallpaper")
    assert not policy.negative_phrase_equivalent("вакансия", "вакцина")


def test_tracking_keeps_custom_params_distinct_from_utm_and_reports_conflicts():
    parsed = tracking.parse_params("https://site.ru/?type={source_type}&utm_source=yandex")
    assert parsed == {"type": "{source_type}", "utm_source": "yandex"}

    effective = tracking.effective_params(
        "https://site.ru/?source=manual",
        "?source={source}&campaign={campaign_id}",
        "?group={gbid}",
    )
    assert effective["effective_params"]["source"] == "manual"
    assert effective["effective_params"]["group"] == "{gbid}"
    assert effective["conflicts"]["source"]["campaign"] == "{source}"


def test_campaign_settings_uses_v501_and_requests_audit_fields():
    api = FakeApi({
        "Campaigns": [{
            "Id": 1,
            "Name": "ЕПК",
            "Type": "UNIFIED_CAMPAIGN",
            "TimeTargeting": {"Schedule": {"Items": []}},
            "NegativeKeywords": {"Items": ["бесплатно"]},
            "UnifiedCampaign": {
                "TrackingParams": "?a=1",
                "Settings": [{"Option": "ADD_METRICA_TAG", "Value": "YES"}],
                "CounterIds": {"Items": [77]},
                "BiddingStrategy": {"Search": {"BiddingStrategyType": "SERVING_OFF"}},
            },
        }]
    })

    result = asyncio.run(campaigns.read_settings(api, "client", campaign_ids=[1]))
    _, method, params, login = api.calls[0]
    assert method == "get"
    assert login == "client"
    assert params["SelectionCriteria"] == {"Ids": [1]}
    assert "TrackingParams" in params["UnifiedCampaignFieldNames"]
    assert "TimeTargeting" in params["FieldNames"]
    assert "Maps" in params["UnifiedCampaignSearchStrategyPlacementTypesFieldNames"]
    assert result["campaigns"][0]["counter_ids"] == [77]
    assert result["campaigns"][0]["negative_keywords"] == ["бесплатно"]
    assert result["campaigns"][0]["time_targeting"] == {
        "Schedule": {"Items": []}
    }


@pytest.mark.parametrize("limit", [0, campaigns.MAX_LIMIT + 1])
def test_campaign_settings_validates_limit_before_api(limit):
    api = FakeApi()
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(campaigns.read_settings(api, "client", limit=limit))
    assert api.calls == []


def test_compliant_search_has_no_blocks_but_keeps_manual_checks_visible():
    result = audit.audit_campaign(campaign(), selected_policy=policy.get())
    by_rule = findings(result)
    assert by_rule["structure.separate_channels"]["status"] == policy.PASS
    assert by_rule["budget.weekly_range"]["status"] == policy.PASS
    assert by_rule["tracking.campaign_profile"]["status"] == policy.PASS
    assert by_rule["goals.explicit_business_selection"]["status"] == policy.PASS
    assert result["ready"] is True


@pytest.mark.parametrize("legacy_value", [None, "YES", "NO"])
def test_retired_extended_geo_readback_does_not_create_findings(legacy_value):
    settings = (
        {} if legacy_value is None else {"ENABLE_AREA_OF_INTEREST_TARGETING": legacy_value}
    )
    result = audit.audit_campaign(campaign(settings=settings), selected_policy=policy.get())
    baseline = audit.audit_campaign(campaign(settings={}), selected_policy=policy.get())
    assert result["findings"] == baseline["findings"]
    assert not any(row["rule"].startswith("geo.area_of_interest") for row in result["findings"])
    assert not any(row["id"].startswith("geo.area_of_interest") for row in policy.get()["rules"])


def test_missing_group_regions_still_block_audit():
    result = audit.audit_campaign(
        campaign(settings={"ENABLE_AREA_OF_INTEREST_TARGETING": "YES"}),
        selected_policy=policy.get(),
        groups=[{"id": 20, "campaign_id": 10, "region_ids": []}],
    )
    assert findings(result)["groups.region_ids"]["status"] == policy.BLOCK
    assert result["ready"] is False


def test_audit_blocks_incomplete_responsive_assets_and_flags_autotargeting():
    item = campaign()
    groups = [{"id": 20, "campaign_id": 10, "region_ids": [213]}]
    ad_rows = [{
        "id": 30,
        "campaign_id": 10,
        "ad_group_id": 20,
        "type": "RESPONSIVE_AD",
        "titles": ["Один"],
        "texts": ["Один"],
        "href": "https://example.test",
        "sitelinks": False,
        "extensions": False,
    }]
    keyword_rows = [
        {
            "id": 40,
            "campaign_id": 10,
            "ad_group_id": 20,
            "keyword": "купить услугу",
        },
        {
            "id": 41,
            "campaign_id": 10,
            "ad_group_id": 20,
            "keyword": "---autotargeting",
            "autotargeting": {
                "categories": {
                    "Exact": "YES",
                    "Narrow": "YES",
                    "Alternative": "YES",
                    "Accessory": "YES",
                    "Broader": "YES",
                },
                "brand_options": {"WithCompetitorsBrand": "YES"},
            },
        },
    ]
    result = audit.audit_campaign(
        item,
        selected_policy=policy.get(),
        groups=groups,
        ad_rows=ad_rows,
        keyword_rows=keyword_rows,
    )
    by_rule = findings(result)
    assert by_rule["ads.responsive_assets"]["status"] == policy.BLOCK
    assert by_rule["ads.responsive_title_utilization"]["status"] == policy.WARNING
    assert by_rule["ads.responsive_title_utilization"]["evidence"]["ads"][0][
        "lengths"
    ] == [4]
    assert by_rule["autotargeting.expanded_categories"]["status"] == policy.WARNING
    assert by_rule["autotargeting.competitor_brands"]["status"] == policy.WARNING
    assert result["ready"] is False


def test_mixed_channels_and_target_cpa_are_blocked():
    item = campaign()
    item["bidding_strategy"]["Network"] = {
        "BiddingStrategyType": "AVERAGE_CPA",
        "AverageCpa": {"AverageCpa": 500_000_000, "WeeklySpendLimit": 3_000_000_000},
    }
    result = audit.audit_campaign(item, selected_policy=policy.get())
    by_rule = findings(result)
    assert by_rule["structure.separate_channels"]["status"] == policy.BLOCK
    assert by_rule["strategy.no_target_cpa"]["status"] == policy.BLOCK
    assert result["ready"] is False


def test_missing_network_exclusions_and_tracking_are_blocked():
    item = campaign(
        negative_keywords=[],
        tracking_params=None,
        bidding_strategy={
            "Search": {"BiddingStrategyType": "SERVING_OFF"},
            "Network": {
                "BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                "WbMaximumClicks": {"WeeklySpendLimit": 4_000_000_000},
            },
        },
    )
    result = audit.audit_campaign(item, selected_policy=policy.get())
    by_rule = findings(result)
    assert by_rule["network.excluded_sites"]["status"] == policy.BLOCK
    assert by_rule["tracking.campaign_profile"]["status"] == policy.BLOCK


def test_risky_negative_is_warning_not_silently_accepted_or_blocked():
    item = campaign()
    item["negative_keywords"].append("как")
    result = audit.audit_campaign(
        item,
        selected_policy=policy.get(),
    )
    finding = findings(result)["search.risky_negative_keywords"]
    assert finding["status"] == policy.WARNING
    assert finding["evidence"]["items"] == ["как"]


def test_audit_artifact_is_utf8_json(tmp_path):
    payload = audit.audit_payload(
        {"client_login": "client/unsafe", "campaigns": [campaign()]}
    )
    path = audit.persist(payload, tmp_path)
    assert path.parent == tmp_path
    assert "client_unsafe" in path.name
    assert json.loads(path.read_text(encoding="utf-8"))["policy"]["name"] == (
        "agency_default_v1"
    )


@pytest.mark.asyncio
@respx.mock
async def test_client_v501_uses_documented_json_endpoint(tmp_path):
    settings = Settings(
        token="secret",
        agency_login=None,
        allowed_logins=frozenset(),
        out_dir=tmp_path,
        sandbox=False,
        max_inflight=1,
        inline_rows=1,
        report_deadline=10,
        lang="ru",
    )
    route = respx.post(f"{API_V501_URL}/campaigns").mock(
        return_value=httpx.Response(200, json={"result": {"Campaigns": []}})
    )
    async with DirectClient(settings) as api:
        result = await api.call_v501(
            "campaigns", "get", {"FieldNames": ["Id"]}, client_login="client"
        )
    assert result == {"Campaigns": []}
    assert route.called
