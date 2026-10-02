"""Regression: bare WordPress URL is 200, advertising name= causes 404."""
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from test_executor import FakeApi, compiled
from test_policy_audit import campaign
from yadirect_mcp import audit, landing, link_checks, policy, tracking


def inputs(params="name={campaign_name}"):
    return ([{"id": 12, "name": "Потолки | Москва", "tracking_params": params}],
            [{"id": 34, "campaign_id": 12}],
            [{"id": 56, "campaign_id": 12, "ad_group_id": 34,
              "href": "https://example.test/", "sitelink_set_id": 78}],
            {78: {"Id": 78, "Sitelinks": [{"Href": "https://example.test/prices/"}]}})


def test_tracking_priority_selects_entire_nearest_block_and_overrides_href():
    result = tracking.effective_params("https://example.test/?x=href&keep=1",
                                       "x=campaign&campaign_only=1", "x=group&group_only=1")
    assert result["effective_params"] == {"x": "group", "keep": "1", "group_only": "1"}
    result = tracking.effective_params("https://example.test/?x=href", "x=campaign",
                                       "x=group", "ad_only=1")
    assert result["effective_params"] == {"x": "href", "ad_only": "1"}


def test_render_preserves_path_query_fragment_and_encodes_values():
    result = tracking.render_url("https://example.test/{keyword}/?keep=1#section",
                                  "yd_campaign_name={campaign_name}",
                                  values={"keyword": "а/б", "campaign_name": "Москва & МО"})
    url = urlsplit(result["url"])
    assert url.path == "/%D0%B0%2F%D0%B1/"
    assert url.fragment == "section"
    assert parse_qs(url.query)["yd_campaign_name"] == ["Москва & МО"]
    assert parse_qs(url.query)["keep"] == ["1"]
    assert not result["unknown_macros"]


@pytest.mark.asyncio
async def test_real_http_parser_catches_name_404_and_safe_profile_works(monkeypatch, respx_mock):
    async def target(url):
        return httpx.URL(url), "93.184.216.34"
    monkeypatch.setattr(landing, "_public_target", target)
    requested = []
    def website(request):
        requested.append(request)
        if "name" in request.url.params:
            return httpx.Response(404, text="<title>Page Not Found</title><h1>404</h1>")
        return httpx.Response(200, text="<title>Цены потолков</title><h1>Цены</h1>")
    respx_mock.route().mock(side_effect=website)
    bad = await link_checks.check_pages(link_checks.inventory(*inputs()))
    assert len(bad) == 4
    assert all(p["site_check"]["status_code"] == 404 for p in bad)
    good = await link_checks.check_pages(
        link_checks.inventory(*inputs("yd_campaign_name={campaign_name}")),
    )
    assert all(p["site_check"]["ok"] for p in good)
    assert {p["device"] for p in good} == {"desktop", "mobile"}
    assert any("Mobile" in r.headers["User-Agent"] for r in requested)
    assert all("yclid" in r.url.params for r in requested)


@pytest.mark.asyncio
async def test_soft_404_is_not_http_success(monkeypatch, respx_mock):
    async def target(url):
        return httpx.URL(url), "93.184.216.34"
    monkeypatch.setattr(landing, "_public_target", target)
    respx_mock.route().respond(200, text="<title>Страница не найдена</title><h1>404</h1>")
    result = await landing.inspect_pages([{"url": "https://example.test/"}])
    assert result[0]["soft_404"] and not result[0]["ok"]


@pytest.mark.asyncio
async def test_unknown_macro_and_unread_sitelinks_fail_without_http(monkeypatch):
    data = list(inputs("x={unknown}"))
    data[-1] = {}
    pages = await link_checks.check_pages(link_checks.inventory(*data))
    assert not link_checks.summary(pages)["all_ok"]
    assert len(pages) == 3
    assert all(not p["site_check"]["checked"] for p in pages)


@pytest.mark.asyncio
async def test_limit_keeps_unchecked_urls_and_blocks_summary(monkeypatch):
    async def checked(pages, **kwargs):
        return [{"url": p["url"], "ok": True} for p in pages]
    monkeypatch.setattr(landing, "inspect_pages", checked)
    monkeypatch.setattr(link_checks, "MAX_URLS_PER_DEVICE", 1)
    pages = await link_checks.check_pages(link_checks.inventory(*inputs()))
    assert len(pages) == 4
    assert link_checks.summary(pages)["failed_or_unchecked"] == 2


def test_identical_urls_are_deduplicated_without_losing_ad_references():
    data = list(inputs("utm_source=yandex"))
    data[2].append(dict(data[2][0], id=57))
    pages = link_checks.inventory(*data)
    assert len(pages) == 4
    assert all(p["ad_ids"] == [56, 57] for p in pages)


def test_policy_compiler_uses_safe_name_and_requires_effective_checks():
    assert "name" not in policy.TRACKING_PARAMS_V1
    assert policy.TRACKING_PARAMS_V1["yd_campaign_name"] == "{campaign_name}"
    assert policy.AGENCY_POLICY_V1["landing_url_policy"]["required"]
    params = compiled()["campaigns"][0]["campaign"]["UnifiedCampaign"]["TrackingParams"]
    assert "yd_campaign_name=" in params


@pytest.mark.asyncio
async def test_preflight_blocks_broken_sitelink_despite_manual_verified_flag(monkeypatch):
    from yadirect_mcp import executor, regions
    async def checked(pages, **kwargs):
        return [{"url": p["url"], "ok": "/link-" not in p["url"],
                 "status_code": 404 if "/link-" in p["url"] else 200} for p in pages]
    monkeypatch.setattr(landing, "inspect_pages", checked)
    regions.reset_cache()
    api = FakeApi()
    result = await executor.preflight(api, compiled())
    assert result["status"] == "BLOCK"
    assert result["effective_link_checks"]["failed_or_unchecked"] > 0
    assert not any("add" in c[:3] for c in api.calls)


def test_audit_does_not_pass_bare_url_as_effective_check():
    ads = [{"id": 2, "campaign_id": 1, "href": "https://example.test/"}]
    result = audit.audit_campaign(campaign(id=1),
                                  selected_policy=policy.AGENCY_POLICY_V1, ad_rows=ads,
                                  landing_pages=[{"url": "https://example.test/", "campaigns": [1],
                                                  "site_check": {"ok": True}}])
    finding = next(f for f in result["findings"] if f["rule"] == "landing.effective_urls")
    assert finding["status"] == policy.BLOCK


@pytest.mark.asyncio
async def test_audit_reads_group_tracking_and_shared_urls_before_http(monkeypatch):
    campaigns, groups, ads, sets = inputs("utm_source=campaign")
    campaigns[0] = campaign(id=12, tracking_params="utm_source=campaign")
    groups[0]["tracking_params"] = "name={campaign_name}"
    monkeypatch.setattr(audit.campaigns, "read_settings", AsyncMock(
        return_value={"campaigns": campaigns, "count": 1}))
    monkeypatch.setattr(audit.ads, "read", AsyncMock(return_value={"ads": ads}))
    monkeypatch.setattr(audit.assets, "enrich_ads", AsyncMock(return_value=sets))
    monkeypatch.setattr(audit.adgroups, "read", AsyncMock(return_value={"groups": groups}))
    monkeypatch.setattr(audit.keywords, "read", AsyncMock(return_value={"keywords": []}))
    monkeypatch.setattr(audit.account, "read", AsyncMock(
        return_value={"bid_modifiers": {"items": []}}))
    async def checked(pages, **kwargs):
        assert all("name=" in p["url"] and "utm_source=" not in p["url"] for p in pages)
        return [{"url": p["url"], "ok": False, "status_code": 404} for p in pages]
    monkeypatch.setattr(landing, "inspect_pages", checked)
    result = await audit.read_and_audit(None, "client", campaign_ids=[12])
    finding = next(f for f in result["campaigns"][0]["findings"]
                   if f["rule"] == "landing.effective_urls")
    assert finding["status"] == policy.BLOCK
    assert finding["evidence"]["checks"]["effective_urls"] == 4


@pytest.mark.asyncio
async def test_large_plan_checks_all_device_urls_in_bounded_batches(monkeypatch):
    calls = []
    async def checked(pages, **kwargs):
        assert len(pages) <= 150
        calls.append((len(pages), kwargs['user_agent']))
        return [{"url": p["url"], "ok": True} for p in pages]
    monkeypatch.setattr(landing, "inspect_pages", checked)
    pages = [{"url": f"https://example.test/?ad={i}", "device": d}
             for d in ("desktop", "mobile") for i in range(305)]
    result = await link_checks.check_pages(pages)
    assert len(result) == 610
    assert [size for size, _ in calls] == [150, 150, 5, 150, 150, 5]
    assert link_checks.summary(result)["all_ok"]


@pytest.mark.asyncio
async def test_large_plan_missing_response_in_later_batch_still_blocks(monkeypatch):
    async def checked(pages, **kwargs):
        return [{"url": p["url"], "ok": True} for p in pages
                if p["url"] != "https://example.test/?ad=304"]
    monkeypatch.setattr(landing, "inspect_pages", checked)
    pages = [{"url": f"https://example.test/?ad={i}", "device": "desktop"}
             for i in range(305)]
    result = await link_checks.check_pages(pages)
    assert link_checks.summary(result)["failed_or_unchecked"] == 1
    assert result[-1]["site_check"]["checked"] is False
