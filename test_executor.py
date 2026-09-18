"""Live preflight, одноразовое подтверждение и v501 executor."""

from __future__ import annotations

import asyncio

import pytest

from test_bundle import source_bundle
from yadirect_mcp import approval, bundle, executor, landing, regions


@pytest.fixture(autouse=True)
def mock_preflight_http(monkeypatch):
    async def checked(pages, **kwargs):
        return [{"url": p["url"], "ok": True, "status_code": 200} for p in pages]
    monkeypatch.setattr(landing, "inspect_pages", checked)


class FakeApi:
    def __init__(self, existing=None, unknown_region=False, fail_service=None):
        self.existing = existing or []
        self.unknown_region = unknown_region
        self.fail_service = fail_service
        self.calls = []
        self.next_id = 100

    def units_for(self, login):
        return getattr(self, "last_units", None)

    async def call(self, service, method, params, *, client_login=None):
        self.calls.append(("v5", service, method, params, client_login))
        if service == "clients":
            return {"Clients": [{"Login": client_login, "Currency": "RUB"}]}
        if params.get("DictionaryNames") == ["Currencies"]:
            return {"Currencies": [{"Currency": "RUB", "Properties": [
                {"Name": "MinimumWeeklySpendLimit", "Value": "300000000"}]}]}
        if service != "dictionaries":
            raise AssertionError(service)
        rows = [] if self.unknown_region else [{
            "GeoRegionId": 213,
            "GeoRegionName": "Москва",
            "GeoRegionType": "City",
            "ParentId": None,
        }]
        return {"GeoRegions": rows}

    async def call_v501(self, service, method, params, *, client_login=None):
        self.calls.append(("v501", service, method, params, client_login))
        if method == "get":
            ids = params.get("SelectionCriteria", {}).get("Ids", [])
            if service == "adimages":
                return {"AdImages": [{"AdImageHash": h} for h in
                                     params["SelectionCriteria"]["AdImageHashes"]]}
            if service == "adextensions":
                return {"AdExtensions": [{"Id": i, "State": "ON"} for i in ids]}
            if service == "businesses":
                return {"Businesses": [{"Id": i, "IsPublished": "YES", "Phone": "74951234567",
                                       "Address": "Москва, Тестовая 1", "HasOffice": "YES"}
                                      for i in ids]}
            if service == "creatives":
                return {"Creatives": [{"Id": i, "Type": "VIDEO_EXTENSION_CREATIVE"} for i in ids]}
            if service == "sitelinks":
                return {"SitelinksSets": [{"Id": i, "Sitelinks": [
                    {"Href": f"https://example.test/link-{n}"} for n in range(8)]}
                                         for i in params["SelectionCriteria"]["Ids"]]}
            return {"Campaigns": self.existing}
        if service == self.fail_service:
            raise RuntimeError(f"failed {service}")
        collection = next(iter(params.values()))
        actions = []
        for _ in collection:
            self.next_id += 1
            actions.append({"Id": self.next_id})
        return {"AddResults": actions}

    async def call_v4(self, method, param=None):
        self.calls.append(("v4", method, param))
        return [{"GoalID": 77, "Name": "Заявка"}]


def compiled():
    return bundle.compile_bundle(source_bundle(), "client")


def test_preflight_checks_regions_and_duplicate_names():
    regions.reset_cache()
    api = FakeApi()
    result = asyncio.run(executor.preflight(api, compiled()))
    assert result["status"] == "PASS"
    assert result["regions_checked"] == [213]
    assert result["regions_resolved"] == 1


def test_preflight_reports_estimated_write_units_when_balance_is_known():
    regions.reset_cache()
    api = FakeApi()
    api.last_units = type("UnitBalance", (), {"rest": 1_000, "daily": 2_000})()
    result = asyncio.run(executor.preflight(api, compiled()))
    assert result["api_units"] == {
        "estimated_write_units": 243,
        "available_after_preflight": 1_000,
        "enough_for_estimated_write": True,
    }


def test_preflight_blocks_write_when_estimated_units_exceed_balance():
    regions.reset_cache()
    api = FakeApi()
    api.last_units = type("UnitBalance", (), {"rest": 100, "daily": 2_000})()
    with pytest.raises(ValueError, match="Директ Коммандер"):
        asyncio.run(executor.preflight(api, compiled()))


def test_preflight_blocks_unknown_region():
    regions.reset_cache()
    with pytest.raises(ValueError, match="213"):
        asyncio.run(executor.preflight(FakeApi(unknown_region=True), compiled()))


def test_preflight_blocks_duplicate_even_if_archived():
    regions.reset_cache()
    name = compiled()["campaigns"][0]["campaign"]["Name"]
    api = FakeApi(existing=[{"Id": 7, "Name": name, "State": "ARCHIVED"}])
    with pytest.raises(ValueError, match="уже существуют"):
        asyncio.run(executor.preflight(api, compiled()))


def test_preflight_fails_closed_on_truncated_campaign_list():
    regions.reset_cache()

    class TruncatedApi(FakeApi):
        async def call_v501(self, service, method, params, *, client_login=None):
            if service == "campaigns" and method == "get":
                return {"Campaigns": [], "LimitedBy": 10000}
            return await super().call_v501(
                service, method, params, client_login=client_login
            )

    with pytest.raises(ValueError, match="усечённый"):
        asyncio.run(executor.preflight(TruncatedApi(), compiled()))


def test_preflight_validates_explicit_goal_catalog_campaign():
    regions.reset_cache()
    plan = compiled()
    plan["goal_catalog_campaign_id"] = 55
    api = FakeApi(existing=[{"Id": 55, "Name": "Старая", "State": "OFF"}])
    result = asyncio.run(executor.preflight(api, plan))
    assert result["goals"] == {
        "status": "PASS",
        "catalog_campaign_id": 55,
        "goal_ids": [77],
    }
    assert any(call[0] == "v4" for call in api.calls)


def test_preflight_blocks_goal_missing_from_explicit_catalog():
    regions.reset_cache()
    plan = compiled()
    plan["goal_catalog_campaign_id"] = 55

    class MissingGoalApi(FakeApi):
        async def call_v4(self, method, param=None):
            return [{"GoalID": 88, "Name": "Другая цель"}]

    api = MissingGoalApi(existing=[{"Id": 55, "Name": "Старая", "State": "OFF"}])
    with pytest.raises(ValueError, match="77"):
        asyncio.run(executor.preflight(api, plan))


def test_approval_is_single_use_and_bound_to_hash():
    registry = approval.ApprovalRegistry(ttl_seconds=60)
    grant = registry.issue("client", "a" * 64)
    with pytest.raises(ValueError, match="другому"):
        registry.consume(grant.phrase, "client", "b" * 64)
    grant = registry.issue("client", "a" * 64)
    registry.consume(grant.phrase, "client", "a" * 64)
    with pytest.raises(ValueError, match="использовано"):
        registry.consume(grant.phrase, "client", "a" * 64)


def test_approval_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(approval.time, "monotonic", lambda: now[0])
    registry = approval.ApprovalRegistry(ttl_seconds=10)
    grant = registry.issue("client", "a" * 64)
    now[0] = 111.0
    with pytest.raises(ValueError, match="истекло"):
        registry.consume(grant.phrase, "client", "a" * 64)


def test_executor_uses_only_v501_add_and_never_resumes():
    api = FakeApi()
    result = asyncio.run(executor.apply(api, compiled()))
    writes = [(call[1], call[2]) for call in api.calls]
    assert writes == [
        ("campaigns", "add"),
        ("bidmodifiers", "add"),
        ("adgroups", "add"),
        ("ads", "add"),
        ("keywords", "add"),
    ]
    assert result["status"] == "complete"
    assert result["activated"] is False
    assert result["summary"]["campaigns"] == {"requested": 2, "created": 2}
    assert result["summary"]["bid_modifiers"] == {"requested": 2, "created": 2}


def test_campaign_add_uses_documented_ten_object_batch_limit():
    api = FakeApi()
    objects = [{"Name": str(index)} for index in range(11)]
    actions = asyncio.run(
        executor._add_v501(api, "campaigns", "Campaigns", objects, "client")
    )
    batches = [call[3]["Campaigns"] for call in api.calls]
    assert len(actions) == 11
    assert list(map(len, batches)) == [10, 1]


def test_keyword_readback_includes_current_autotargeting_settings():
    class KeywordApi:
        def __init__(self):
            self.params = None

        async def call_v501(self, service, method, params, *, client_login=None):
            assert (service, method, client_login) == ("keywords", "get", "client")
            self.params = params
            return {"Keywords": [{"Id": 7, "Keyword": "---autotargeting"}]}

    api = KeywordApi()
    rows = asyncio.run(executor._read_created_keywords(api, [7], "client"))
    assert rows == [{"Id": 7, "Keyword": "---autotargeting"}]
    assert "Keyword" in api.params["FieldNames"]
    assert "Exact" in api.params[
        "AutotargetingSettingsCategoriesFieldNames"
    ]


def test_executor_preserves_parent_ids_on_partial_failure():
    api = FakeApi(fail_service="ads")
    result = asyncio.run(executor.apply(api, compiled()))
    assert result["status"] == "partial"
    assert result["fatal_error"]["stage"] == "ads.add"
    assert all(item["id"] is not None for item in result["campaigns"])
    assert result["summary"]["ad_groups"]["created"] == 2
    assert result["summary"]["ads"]["created"] == 0
    assert not any(call[1] == "keywords" for call in api.calls)


def test_execution_journal_contains_hash_and_created_ids(tmp_path):
    result = asyncio.run(executor.apply(FakeApi(), compiled()))
    path = executor.persist(result, tmp_path)
    assert result["plan_hash"][:12] in path.name
    assert path.is_file()


def readback_payloads(plan, execution):
    settings = []
    groups = []
    ad_rows = []
    keyword_ids = set()
    for executed in execution["campaigns"]:
        planned = plan["campaigns"][executed["plan_index"]]
        campaign = planned["campaign"]
        unified = campaign["UnifiedCampaign"]
        settings.append({
            "id": executed["id"],
            "name": campaign["Name"],
            "state": "OFF",
            "time_zone": campaign["TimeZone"],
            "attribution_model": unified["AttributionModel"],
            "settings": {row["Option"]: row["Value"] for row in unified["Settings"]},
            "time_targeting": campaign.get("TimeTargeting", {
                "Schedule": {"Items": []},
                "ConsiderWorkingWeekends": "YES",
                "HolidaysSchedule": None,
            }),
            "bidding_strategy": unified["BiddingStrategy"],
            "tracking_params": unified["TrackingParams"],
            "counter_ids": unified.get("CounterIds", {}).get("Items", []),
            "priority_goals": unified.get("PriorityGoals", {}).get("Items", []),
            "negative_keywords": planned["campaign"].get(
                "NegativeKeywords", {}
            ).get("Items", []),
            "excluded_sites": planned["campaign"].get("ExcludedSites", {}).get(
                "Items", []
            ),
        })
        for group in executed["groups"]:
            groups.append({
                "id": group["id"],
                "name": planned["groups"][group["plan_index"]]["ad_group"]["Name"],
                "campaign_id": executed["id"],
                "region_ids": [213],
            })
            planned_group = planned["groups"][group["plan_index"]]
            for expected_ad, action in zip(
                planned_group["ads"], group["ads"], strict=False
            ):
                responsive = expected_ad["ResponsiveAd"]
                ad_rows.append({
                    "id": action["Id"],
                    "campaign_id": executed["id"],
                    "type": "RESPONSIVE_AD",
                    "titles": responsive["Titles"],
                    "texts": responsive["Texts"],
                    "href": responsive.get("Href"),
                    "display_url_path": responsive.get("DisplayUrlPath"),
                    "sitelink_set_id": responsive.get("SitelinkSetId"),
                    "sitelink_count": 8,
                    "ad_image_hashes": responsive.get("AdImageHashes", []),
                })
            keyword_ids.update(action["Id"] for action in group["keywords"])
    return (
        {"campaigns": settings},
        {"groups": groups},
        {
            "ads": ad_rows,
            "counts_by_campaign": {
                str(item["id"]): sum(
                    row["campaign_id"] == item["id"] for row in ad_rows
                )
                for item in execution["campaigns"]
            },
        },
        keyword_ids,
    )


def test_readback_comparison_verifies_exact_plan_and_stopped_state():
    plan = compiled()
    execution = asyncio.run(executor.apply(FakeApi(), plan))
    settings, groups, ad_rows, keyword_ids = readback_payloads(plan, execution)
    result = executor.compare_readback(
        plan, execution, settings, groups, ad_rows, keyword_ids
    )
    assert result["verified"] is True
    assert result["summary"]["BLOCK"] == 0


def test_readback_blocks_an_activated_campaign():
    plan = compiled()
    execution = asyncio.run(executor.apply(FakeApi(), plan))
    settings, groups, ad_rows, keyword_ids = readback_payloads(plan, execution)
    settings["campaigns"][0]["state"] = "ON"
    result = executor.compare_readback(
        plan, execution, settings, groups, ad_rows, keyword_ids
    )
    finding = next(
        item for item in result["findings"]
        if item["rule"] == "readback.not_activated"
    )
    assert finding["status"] == "BLOCK"
    assert result["verified"] is False


@pytest.mark.parametrize("actual", [None, {
    "Schedule": {"Items": ["1," + ",".join(["0"] * 9 + ["100"] * 10 + ["0"] * 5)]},
    "ConsiderWorkingWeekends": "NO",
}])
def test_readback_blocks_missing_or_restricted_native_schedule(actual):
    plan = compiled()
    execution = asyncio.run(executor.apply(FakeApi(), plan))
    settings, groups, ad_rows, keyword_ids = readback_payloads(plan, execution)
    settings["campaigns"][0]["time_targeting"] = actual
    result = executor.compare_readback(plan, execution, settings, groups, ad_rows, keyword_ids)
    assert next(item for item in result["findings"]
                if item["rule"] == "readback.time_targeting")["status"] == "BLOCK"
