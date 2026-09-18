"""Regressions for the audit: large scopes, API evidence, money and upload binding."""

import asyncio
import base64
import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from test_bundle import source_bundle
from test_executor import FakeApi, compiled, readback_payloads
from test_report_context import Api as ReportApi
from test_report_context import contract
from test_server import _in_server
from yadirect_mcp import (
    ads,
    api_units,
    approval,
    artifacts,
    assets,
    audit,
    bundle,
    executor,
    policy,
    regions,
    repair,
    report_context,
    store,
)
from yadirect_mcp.client import DirectClient


@pytest.mark.asyncio
async def test_preflight_resolves_more_than_200_regions():
    class Api(FakeApi):
        async def call(self, *args, **kwargs):
            if args[0] == "clients" or args[2].get("DictionaryNames") == ["Currencies"]:
                return await super().call(*args, **kwargs)
            return {"GeoRegions": [{"GeoRegionId": i, "GeoRegionName": f"Region {i}",
                                    "GeoRegionType": "City", "ParentId": None}
                                   for i in range(1, 351)]}

    regions.reset_cache()
    plan = compiled()
    plan["campaigns"][0]["groups"][0]["ad_group"]["RegionIds"] = list(range(1, 351))
    result = await executor.preflight(Api(), plan)
    assert result["regions_resolved"] == 350


@pytest.mark.asyncio
async def test_goal_catalog_uses_matching_counter():
    regions.reset_cache()
    api = FakeApi(existing=[
        {"Id": 1, "Name": "unrelated", "State": "OFF",
         "UnifiedCampaign": {"CounterIds": {"Items": [999]}}},
        {"Id": 2, "Name": "matching", "State": "OFF",
         "UnifiedCampaign": {"CounterIds": {"Items": [12345]}}},
    ])
    result = await executor.preflight(api, compiled())
    assert result["goals"]["catalog_campaign_id"] == 2


@pytest.mark.asyncio
async def test_unrelated_catalog_cannot_verify_goals():
    regions.reset_cache()
    plan = compiled()
    plan["allow_unverified_goals"] = False
    api = FakeApi(existing=[{"Id": 1, "Name": "other", "State": "ON",
                            "TextCampaign": {"CounterIds": {"Items": [999]}}}])
    with pytest.raises(ValueError, match="goal_catalog_campaign_id"):
        await executor.preflight(api, plan)
    assert not any(call[0] == "v4" for call in api.calls)


def test_direct_lowercase_normalization_of_excluded_sites_is_accepted():
    plan = compiled()
    network = next(row for row in plan["campaigns"] if row["channel"] == "network")
    assert "com.Balls" in network["campaign"]["ExcludedSites"]["Items"]
    execution = asyncio.run(executor.apply(FakeApi(), plan))
    settings, groups, ad_rows, keyword_ids = readback_payloads(plan, execution)
    actual = next(row for row in settings["campaigns"] if row["excluded_sites"])
    actual["excluded_sites"] = [s.lower() for s in actual["excluded_sites"]]
    result = executor.compare_readback(plan, execution, settings, groups, ad_rows, keyword_ids)
    assert result["verified"]

    # Case normalization must not hide an omitted or a substituted placement.
    actual["excluded_sites"].remove("com.balls")
    actual["excluded_sites"].append("com.unexpected.app")
    result = executor.compare_readback(plan, execution, settings, groups, ad_rows, keyword_ids)
    assert not result["verified"]
    assert any(row["rule"] == "readback.excluded_sites" and row["status"] == "BLOCK"
               for row in result["findings"])


@pytest.mark.parametrize("campaign_type", ["SMART_CAMPAIGN", "MOBILE_APP_CAMPAIGN",
                                           "CPM_BANNER_CAMPAIGN"])
def test_unsupported_campaign_is_manual_without_false_budget_block(campaign_type):
    result = audit.audit_payload({"campaigns": [{"id": 1, "type": campaign_type}]})
    assert result["summary"]["BLOCK"] == 0
    assert result["summary"]["MANUAL"] == 1
    assert result["campaigns"][0]["supported"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["ads", "adgroups", "keywords", "bid_modifiers"])
async def test_audit_propagates_each_incomplete_source(monkeypatch, source):
    monkeypatch.setattr(audit.campaigns, "read_settings", AsyncMock(return_value={
        "campaigns": [{"id": 1, "type": "UNIFIED_CAMPAIGN"}],
    }))
    payloads = {"ads": {"ads": []}, "adgroups": {"groups": []}, "keywords": {"keywords": []},
                "bid_modifiers": {"items": []}}
    payloads[source].update(truncated=True, limited_by=10000)
    monkeypatch.setattr(audit.ads, "read", AsyncMock(return_value=payloads["ads"]))
    monkeypatch.setattr(audit.adgroups, "read", AsyncMock(return_value=payloads["adgroups"]))
    monkeypatch.setattr(audit.keywords, "read", AsyncMock(return_value=payloads["keywords"]))
    monkeypatch.setattr(audit.account, "read", AsyncMock(return_value={
        "bid_modifiers": payloads["bid_modifiers"],
    }))
    result = await audit.read_and_audit(FakeApi(), "client", include_landing_pages=False)
    assert not result["ready"] and result["source_truncated"]
    assert result["incomplete_sources"][0]["source"] == source
    assert artifacts.compact(result)["incomplete_sources"]


@pytest.mark.asyncio
async def test_readback_reads_keyword_ids_in_bounded_batches(monkeypatch):
    plan = compiled()
    execution = await executor.apply(FakeApi(), plan)
    execution["campaigns"][0]["groups"][0]["keywords"] = [{"Id": i} for i in range(1, 20002)]
    for row in execution["campaigns"][1:]:
        for group in row["groups"]:
            group["keywords"] = []
    monkeypatch.setattr(executor.campaigns, "read_settings",
                        AsyncMock(return_value={"campaigns": []}))
    monkeypatch.setattr(executor.adgroups, "read", AsyncMock(return_value={"groups": []}))
    monkeypatch.setattr(executor.ads, "read", AsyncMock(return_value={"ads": []}))
    monkeypatch.setattr(executor, "_read_created_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(executor.account, "read", AsyncMock(return_value={}))
    batches = []

    async def read(api, login, *, keyword_ids, limit):
        batches.append(keyword_ids)
        return {"keywords": [{"id": i, "keyword": "x"} for i in keyword_ids]}

    monkeypatch.setattr(executor.keywords, "read", read)
    monkeypatch.setattr(executor, "compare_readback", lambda *args: {"verified": True})
    result = await executor.readback(FakeApi(), plan, execution)
    assert [len(ids) for ids in batches] == [10000, 10000, 1]
    assert result["counts"]["criteria"] == 20001


@pytest.mark.asyncio
async def test_truncated_keywords_are_incomplete_not_missing_ids(monkeypatch):
    plan = compiled()
    execution = await executor.apply(FakeApi(), plan)
    monkeypatch.setattr(executor.campaigns, "read_settings",
                        AsyncMock(return_value={"campaigns": []}))
    monkeypatch.setattr(executor.adgroups, "read", AsyncMock(return_value={"groups": []}))
    monkeypatch.setattr(executor.ads, "read", AsyncMock(return_value={"ads": []}))
    monkeypatch.setattr(executor, "_read_created_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(executor.keywords, "read", AsyncMock(return_value={
        "keywords": [], "truncated": True, "limited_by": 10000,
    }))
    result = await executor.readback(FakeApi(), plan, execution)
    assert not result["verified"]
    assert result["comparison"]["findings"][0]["rule"] == "readback.incomplete_data"


def test_timezone_settings_attribution_bid_ceiling_and_age_label_compile():
    source = source_bundle()
    source.update(time_zone="Asia/Vladivostok", attribution_model="LC",
                  settings={"ALTERNATIVE_TEXTS_ENABLED": False,
                            "CAMPAIGN_EXACT_PHRASE_MATCHING_ENABLED": True})
    source["channels"]["search"]["strategy"] = {"type": "maximum_clicks", "bid_ceiling": 99.25}
    source["channels"]["search"]["groups"][0]["ads"][0]["age_label"] = "AGE_18"
    plan = bundle.compile_bundle(source, "client")
    campaign = plan["campaigns"][0]["campaign"]
    assert campaign["TimeZone"] == "Asia/Vladivostok"
    unified = campaign["UnifiedCampaign"]
    assert unified["AttributionModel"] == "LC"
    assert unified["BiddingStrategy"]["Search"]["WbMaximumClicks"]["BidCeiling"] == 99_250_000
    assert {row["Option"]: row["Value"] for row in unified["Settings"]}[
        "CAMPAIGN_EXACT_PHRASE_MATCHING_ENABLED"] == "YES"
    assert plan["campaigns"][0]["groups"][0]["ads"][0]["ResponsiveAd"]["AgeLabel"] == "AGE_18"
    assert ads._shape({"AgeLabel": "AGE_18"})["age_label"] == "AGE_18"
    assert "AgeLabel" in ads._AD_FIELDS and "AgeLabel" not in ads._RESPONSIVE_AD_FIELDS


def test_holidays_can_suspend_24_7_and_do_not_fill_split_hours():
    targeting = bundle._time_targeting({"holidays": {"suspend": True}})
    assert targeting["HolidaysSchedule"] == {"SuspendOnHolidays": "YES"}
    targeting = bundle._time_targeting({"hours": [9, 10, 22, 23]})
    assert "HolidaysSchedule" not in targeting
    assert targeting["Schedule"]["Items"][0].split(",")[12] == "0"
    targeting = bundle._time_targeting({"holidays": {
        "suspend": False, "start_hour": 9, "end_hour": 11, "bid_percent": 80,
    }})
    assert targeting["HolidaysSchedule"] == {
        "SuspendOnHolidays": "NO", "StartHour": 9, "EndHour": 11, "BidPercent": 80,
    }


def test_variants_require_explicit_budgets_and_expose_channel_total():
    source = source_bundle()
    variant = source["channels"]["search"]
    source["channels"]["search"] = [deepcopy(variant) for _ in range(3)]
    for i, row in enumerate(source["channels"]["search"]):
        row["name_suffix"] = str(i)
    with pytest.raises(ValueError, match="weekly_budget"):
        bundle.compile_bundle(source, "client")
    for row in source["channels"]["search"]:
        row["weekly_budget"] = 5000
    plan = bundle.compile_bundle(source, "client")
    assert plan["summary"]["weekly_budget_by_channel"]["search"] == 15000
    assert plan["summary"]["weekly_budget_total"] == 19000
    assert plan["ready"]
    assert any(row["rule"] == "budget.channel_total" and row["status"] == "WARNING"
               for row in plan["findings"])


def test_explicit_negatives_are_applied_to_network():
    source = source_bundle()
    source["additional_negative_keywords"] = ["вакансия"]
    plan = bundle.compile_bundle(source, "client")
    network = next(row for row in plan["campaigns"] if row["channel"] == "network")
    assert "вакансия" in network["campaign"]["NegativeKeywords"]["Items"]


_png_stream = BytesIO()
Image.new("RGB", (450, 450), "white").save(_png_stream, format="PNG")
PNG = _png_stream.getvalue()


def test_image_approval_binds_file_bytes_and_preview_is_bounded(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(PNG)
    source = {"images": [{"path": str(path), "name": "Test"}]}
    plan = assets.normalize(source, "client")
    registry = approval.ApprovalRegistry()
    grant = registry.issue("client", plan["plan_hash"])
    assert "images" not in assets.preview(plan)
    assert plan["image_manifest"][0]["bytes"] == len(PNG)
    path.write_bytes(PNG + b"changed")
    changed = assets.normalize(source, "client")
    with pytest.raises(ValueError):
        registry.consume(grant.phrase, "client", changed["plan_hash"])


@pytest.mark.asyncio
async def test_image_upload_batches_and_reads_hashes():
    calls = []

    class Api:
        async def call_v501(self, service, method, params, **kwargs):
            calls.append((service, method, params))
            if method == "get":
                return {"AdImages": [{"AdImageHash": value}
                                     for value in params["SelectionCriteria"]["AdImageHashes"]]}
            return {"AddResults": [{"AdImageHash": row["Name"]} for row in params["AdImages"]]}

    plan = assets.normalize({"images": [
        {"name": str(i), "image_data": base64.b64encode(PNG).decode()} for i in range(7)
    ]}, "client")
    result = await assets.apply(Api(), plan)
    assert result["status"] == "complete" and result["images_verified"]
    assert [len(params["AdImages"]) for _, method, params in calls if method == "add"] == [7]


@pytest.mark.asyncio
async def test_report_settings_cache_is_per_client_and_scope(monkeypatch):
    api = ReportApi()
    now = [100.0]
    monkeypatch.setattr(report_context.time, "monotonic", lambda: now[0])
    first = await report_context.resolve(api, "client", contract())
    again = await report_context.resolve(api, "client", contract())
    assert len(api.calls) == 1 and first == again
    assert "Settings" not in api.calls[0]["UnifiedCampaignFieldNames"]
    await report_context.resolve(api, "other", contract())
    assert len(api.calls) == 2
    now[0] += report_context.CACHE_TTL_SECONDS + 1
    await report_context.resolve(api, "client", contract())
    assert len(api.calls) == 3


def client_settings(**kwargs):
    return SimpleNamespace(sandbox=True, max_inflight=5, token="test", lang="ru",
                           report_deadline=10, **kwargs)


@pytest.mark.asyncio
async def test_units_are_per_login_and_task_identity_is_not_reused():
    async with DirectClient(client_settings()) as api:
        async def record(login, rest):
            mark = api.units_mark()
            api._note_units(httpx.Response(200, headers={"Units": f"1/{rest}/1000"}),
                            api_version="v501", service="campaigns", method="get",
                            client_login=login)
            return mark

        first = await asyncio.create_task(record("first", 10))
        second = await asyncio.create_task(record("second", 900))
        assert first.task_id != second.task_id
        assert api.units_for("FIRST").rest == 10 and api.units_for("second").rest == 900
        assert api.units_for("missing") is None
        assert api.units_since(first)["spent"] == 1


@pytest.mark.asyncio
async def test_reports_share_a_user_semaphore_across_logins():
    async with DirectClient(client_settings()) as api:
        active, maximum = 0, 0

        async def handler(request):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, text="Clicks\n1\n")

        await api._http.aclose()
        api._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await asyncio.gather(*(api.report({"FieldNames": ["Clicks"]}, client_login=f"c{i}")
                               for i in range(12)))
        assert maximum == 5


@pytest.mark.asyncio
async def test_repair_null_goals_are_verified_not_assumed(monkeypatch):
    plan = repair.normalize({"campaigns": [{"id": 1, "priority_goals": []}]}, "client")
    assert plan["campaigns"][0]["UnifiedCampaign"]["PriorityGoals"] is None
    monkeypatch.setattr(repair.campaigns, "read_settings", AsyncMock(return_value={
        "campaigns": [{"id": 1, "priority_goals": [{"GoalId": 77, "Value": 1}]}],
    }))
    result = await repair.readback(FakeApi(), plan)
    assert not result["verified"] and result["mismatches"][0]["reason"] == "priority_goals"


@pytest.mark.asyncio
async def test_expired_unit_rates_do_not_hard_block_preflight(monkeypatch):
    regions.reset_cache()
    api = FakeApi()
    api.last_units = SimpleNamespace(rest=1)
    monkeypatch.setattr(api_units, "rates_current", lambda: False)
    result = await executor.preflight(api, compiled())
    assert result["api_units"]["enough_for_estimated_write"] is False
    assert result["api_units"]["warning"]


def test_official_narrow_characters_and_agency_text_count_remain_explicit():
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["ads"][0]["texts"] = ["a", "b"]
    with pytest.raises(ValueError, match="3"):
        bundle.compile_bundle(source, "client")
    assert policy.AGENCY_POLICY_V1["creative"]


def test_report_cache_detects_changed_bytes_with_identical_stat(tmp_path, monkeypatch):
    store.persist("Clicks\n11\n", out_dir=tmp_path, stem="same", inline_rows=0,
                  metadata={"goals": ["77"]})
    path = tmp_path / "same.tsv"
    original_stat = path.stat()
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda p, *args, **kwargs:
                        original_stat if p == path else real_stat(p, *args, **kwargs))
    assert store.read_back(path, 0, 1)["data"] == [{"Clicks": "11"}]
    path.write_text("Clicks\n22\n", encoding="utf-8")
    result = store.read_back(path, 0, 1)
    assert result["data"] == [{"Clicks": "22"}] and "metadata_warning" in result


@pytest.mark.asyncio
async def test_campaign_write_invalidates_report_context_cache():
    async with DirectClient(client_settings()) as api:
        api._report_settings_cache[("client", ())] = (0, {})
        await api._http.aclose()
        api._http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1}]}})
        ))
        await api.call_v501("campaigns", "update", {"Campaigns": [{"Id": 1}]},
                           client_login="client")
        assert not api._report_settings_cache


def test_discount_flag_reaches_report_spec_and_metadata(tmp_path):
    result = _in_server('''import asyncio, json
import yadirect_mcp.server as s
class Api:
    last_units = None
    def report_name(self, spec): return "discount"
    async def report(self, spec, **kwargs):
        self.spec = spec
        return "Clicks\\tCost\\n1\\t10\\n"
api = s._client = Api()
result = asyncio.run(s.direct_report("client", "2026-01-01", "2026-01-02",
                                    ["Clicks", "Cost"], include_discount=True))
print(json.dumps({"spec": api.spec, "result": result.structuredContent}))''', str(tmp_path))
    assert "IncludeDiscount" not in result["spec"]
    assert result["result"]["metadata"]["include_discount"] is True


def test_assets_preview_never_returns_binary_data_from_tool(tmp_path):
    raw = {"images": [{"name": "Test", "image_data": base64.b64encode(PNG).decode()}]}
    result = _in_server(
        "import asyncio, json\nimport yadirect_mcp.server as s\n"
        f"raw = json.loads({json.dumps(json.dumps(raw))})\n"
        "result = asyncio.run(s.direct_ad_assets_create('client', raw))\n"
        "print(result.content[0].text)", str(tmp_path), YD_MODE="campaign_setup",
    )
    assert result["status"] == "preview" and result["image_manifest"][0]["sha256"]
    assert "images" not in result and "ImageData" not in json.dumps(result)


@pytest.mark.asyncio
async def test_image_partial_failure_retains_previous_chunk_hashes():
    class Api:
        calls = 0

        async def call_v501(self, service, method, params, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("connection lost")
            return {"AddResults": [{"AdImageHash": row["Name"]} for row in params["AdImages"]]}

    plan = assets.normalize({"images": [
        {"name": str(i), "image_data": base64.b64encode(PNG).decode()} for i in range(100)
    ]}, "client")
    # Exercise the executor boundary independently of the public 100-image limit.
    plan["images"].append({**plan["images"][0], "Name": "100"})
    result = await assets.apply(Api(), plan)
    assert result["status"] == "partial"
    assert [row["AdImageHash"] for row in result["results"]["AdImages"]] == [
        str(i) for i in range(100)]


@pytest.fixture(autouse=True)
def mock_effective_url_http(monkeypatch):
    from yadirect_mcp import landing

    async def checked(pages, **kwargs):
        return [{"url": p["url"], "ok": True, "status_code": 200} for p in pages]
    monkeypatch.setattr(landing, "inspect_pages", checked)
