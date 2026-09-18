"""Repair P1 regressions. All API and HTTP reads are local fakes."""

import asyncio
import json
import os
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from test_approval_compatibility import host
from test_repair import FakeApi, source
from yadirect_mcp import ads, approval, assets, repair
from yadirect_mcp.identifiers import wire


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def check(pages):
        for page in pages:
            page.setdefault("site_check", {"ok": True, "checked": True})
        return pages
    monkeypatch.setattr(repair.link_checks, "check_pages", check)


class Api(FakeApi):
    last_units = None

    def __init__(self):
        super().__init__()
        self.current = None
        self.fail_url = False

    async def aclose(self):
        pass

    async def call_v501(self, service, method, params, **kwargs):
        if service == "ads" and method == "get":
            if self.current is None:
                result = await super().call_v501(service, method, params, **kwargs)
                self.current = result["Ads"][0]
                self.current["ResponsiveAd"]["DisplayUrlPath"] = "preserved"
            return {"Ads": [deepcopy(self.current)]}
        if service == "ads" and method == "update":
            self.calls.append((service, method, params, kwargs.get("client_login")))
            for row in params["Ads"]:
                changes = deepcopy(row["ResponsiveAd"])
                if "CalloutSetting" in changes:
                    setting = changes.pop("CalloutSetting") or {}
                    changes["AdExtensions"] = [{"AdExtensionId": v["AdExtensionId"]}
                                               for v in setting.get("AdExtensions", [])]
                self.current["ResponsiveAd"].update(changes)
            return {"UpdateResults": [{"Id": row["Id"]} for row in params["Ads"]]}
        return await super().call_v501(service, method, params, **kwargs)


def ad_source():
    return {"ads": [source()["ads"][0]]}


@pytest.mark.asyncio
async def test_string_ids_roundtrip_and_realistic_numeric_response():
    raw = wire(ad_source())
    plan = repair.normalize(raw, "client")
    ad = plan["ads"][0]
    assert isinstance(ad["Id"], int)
    assert isinstance(ad["ResponsiveAd"]["SitelinkSetId"], int)
    assert repair.normalize(ad_source(), "client")["plan_hash"] == plan["plan_hash"]
    result = await assets.read_sets(FakeApi(), "client", ["41"])
    assert list(result) == [41]
    result = await repair.apply(Api(), plan)
    assert result["status"] == "complete"


@pytest.mark.parametrize("bad", [True, 1.5, "1.5", "0", "-2", "9223372036854775808"])
@pytest.mark.parametrize("field", ["sitelink_set_id", "ad_extension_ids"])
def test_invalid_nested_ids_rejected(bad, field):
    raw = ad_source()
    raw["ads"][0][field] = [bad] if field.endswith("ids") else bad
    with pytest.raises(ValueError):
        repair.normalize(raw, "client")


@pytest.mark.parametrize("section", ["campaigns", "ads", "autotargetings"])
def test_duplicate_objects_rejected(section):
    raw = source()
    raw[section].append(deepcopy(raw[section][0]))
    with pytest.raises(ValueError, match="дубли"):
        repair.normalize(raw, "client")


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[999], [51], [51, 52, 53], []])
async def test_exact_callout_readback_rejects_wrong_missing_and_extra(ids):
    api = Api()
    plan = repair.normalize(ad_source(), "client")
    checked = await repair.preflight(api, plan)
    api.current["ResponsiveAd"]["AdExtensions"] = [{"AdExtensionId": i} for i in ids]
    result = await repair.readback(api, plan, before=checked["before"])
    assert not result["verified"]
    assert any(row["reason"] == "ad_extension_ids" for row in result["mismatches"])


@pytest.mark.asyncio
async def test_omitted_fields_preserved_and_unexpected_change_detected():
    api = Api()
    plan = repair.normalize({"ads": [{"id": "20", "href": "https://example.test/new"}]}, "client")
    assert plan["ads"][0]["ResponsiveAd"] == {"Href": "https://example.test/new"}
    result = await repair.apply(api, plan)
    assert (await repair.readback(api, plan, before=result["preflight"]["before"]))["verified"]
    api.current["ResponsiveAd"]["DisplayUrlPath"] = "changed"
    result = await repair.readback(api, plan, before=result["preflight"]["before"])
    assert not result["verified"]
    assert result["mismatches"][0]["reason"] == "display_url_path"


@pytest.mark.asyncio
async def test_clear_optional_fields_and_callouts_explicitly():
    api = Api()
    raw = {"ads": [{"id": 20, "display_url_path": None, "sitelink_set_id": None,
                    "ad_extension_ids": []}]}
    plan = repair.normalize(raw, "client")
    patch = plan["ads"][0]["ResponsiveAd"]
    assert patch == {"DisplayUrlPath": None, "SitelinkSetId": None, "CalloutSetting": None}
    result = await repair.apply(api, plan)
    assert (await repair.readback(api, plan, before=result["preflight"]["before"]))["verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("group_override", [False, True])
async def test_effective_links_include_campaign_group_and_sitelinks(group_override):
    class TrackingApi(Api):
        async def call_v501(self, service, method, params, **kwargs):
            result = await super().call_v501(service, method, params, **kwargs)
            if service == "adgroups" and not group_override:
                result["AdGroups"][0]["TrackingParams"] = None
            return result
    api = TrackingApi()
    plan = repair.normalize(ad_source(), "client")
    checked = await repair.preflight(api, plan)
    assert api.calls == []
    pages = checked["effective_link_checks"]["pages"]
    assert {p["device"] for p in pages} == {"desktop", "mobile"}
    assert {kind for p in pages for kind in p["link_kinds"]} == {"main", "sitelink"}
    expected = "utm_content=20" if group_override else "utm_campaign=10"
    absent = "utm_campaign=" if group_override else "utm_content="
    assert all(expected in p["url"] and absent not in p["url"] for p in pages)
    assert checked["plan_hash"] == plan["plan_hash"]


@pytest.mark.asyncio
async def test_changed_source_blocks_all_writes_after_preview():
    api = Api()
    plan = repair.normalize(ad_source(), "client")
    checked = await repair.preflight(api, plan)
    api.current["ResponsiveAd"]["DisplayUrlPath"] = "changed"
    with pytest.raises(ValueError, match="после preview"):
        await repair.apply(api, plan, expected_preflight=checked)
    assert api.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["missing", "truncated", "duplicates", "archived", "type"])
async def test_bad_inventory_blocks_before_writes(monkeypatch, problem):
    api = Api()
    snapshot = await ads.read(api, "client", ad_ids=[20], preview_limit=None)
    if problem == "missing":
        snapshot["ads"] = []
    elif problem == "truncated":
        snapshot["truncated"] = True
    elif problem == "duplicates":
        snapshot["ads"] *= 2
    elif problem == "archived":
        snapshot["ads"][0]["state"] = "ARCHIVED"
    else:
        snapshot["ads"][0]["type"] = "TEXT_AD"
    monkeypatch.setattr(repair.ads, "read", AsyncMock(return_value=snapshot))
    with pytest.raises(ValueError):
        await repair.apply(api, repair.normalize(ad_source(), "client"))
    assert api.calls == []


@pytest.mark.asyncio
async def test_url_failure_blocks_before_writes(monkeypatch):
    async def fail(pages):
        for page in pages:
            page["site_check"] = {"ok": False, "checked": True, "status": 404}
        return pages
    monkeypatch.setattr(repair.link_checks, "check_pages", fail)
    api = Api()
    with pytest.raises(ValueError, match="URL"):
        await repair.apply(api, repair.normalize(ad_source(), "client"))
    assert api.calls == []


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("YD_TOKEN", "dummy")
    monkeypatch.setenv("YD_OUT_DIR", str(tmp_path))
    from yadirect_mcp import server as s
    monkeypatch.setattr(s, "SETTINGS", SimpleNamespace(
        mode="campaign_setup", out_dir=tmp_path, check_login=lambda login: None,
    ))
    monkeypatch.setattr(s, "_client", Api())
    monkeypatch.setattr(approval, "REGISTRY", approval.ApprovalRegistry())
    return s


@pytest.mark.asyncio
async def test_failed_preview_issues_no_token(server, monkeypatch):
    monkeypatch.setattr(repair, "preflight", AsyncMock(side_effect=ValueError("URL BLOCK")))
    result = await server.direct_campaign_repair("client", ad_source())
    assert result.isError
    assert not approval.REGISTRY._grants
    assert server._client.calls == []


@pytest.mark.asyncio
async def test_repair_confirmation_integrity_decline_and_single_use(server):
    raw = ad_source()
    preview = await server.direct_campaign_repair("client", raw)
    assert not preview.isError
    assert server._client.calls == []
    token = preview.structuredContent["confirmation_required"]
    changed = deepcopy(raw)
    changed["ads"][0]["href"] = "https://example.test/other"
    for login, value in [("other-client", raw), ("client", changed)]:
        result = await server.direct_campaign_repair(login, value, token, ctx=host())
        assert result.isError
    result = await server.direct_campaign_repair("client", raw, token, ctx=host("decline"))
    assert result.isError
    assert server._client.calls == []
    result = await server.direct_campaign_repair("client", raw, token, ctx=host())
    assert not result.isError, result
    assert result.structuredContent["readback"]["verified"]
    assert len(server._client.calls) == 1
    result = await server.direct_campaign_repair("client", raw, token, ctx=host())
    assert result.isError
    assert len(server._client.calls) == 1


@pytest.mark.asyncio
async def test_expired_repair_token_does_not_write(server, monkeypatch):
    preview = await server.direct_campaign_repair("client", ad_source())
    token = preview.structuredContent["confirmation_required"]
    grant = approval.REGISTRY.validate(token, "client", preview.structuredContent["plan_hash"])
    monkeypatch.setattr(approval.time, "monotonic", lambda: grant.expires_at + 1)
    result = await server.direct_campaign_repair("client", ad_source(), token, ctx=host())
    assert result.isError
    assert not server._client.calls


@pytest.mark.asyncio
async def test_campaign_readback_checks_preserved_settings(monkeypatch):
    plan = repair.normalize({"campaigns": [{"id": 10, "negative_keywords": []}]}, "client")
    before = {"campaigns": [{"id": 10, "negative_keywords": ["old"],
                             "time_zone": "Europe/Moscow", "state": "OFF"}]}
    actual = {"campaigns": [{"id": 10, "negative_keywords": [],
                             "time_zone": "Asia/Yekaterinburg", "state": "OFF"}]}
    monkeypatch.setattr(repair.campaigns, "read_settings", AsyncMock(return_value=actual))
    result = await repair.readback(FakeApi(), plan, before=before)
    assert not result["verified"]
    assert result["mismatches"][0]["reason"] == "preserved_time_zone"


@pytest.mark.asyncio
async def test_readback_rejects_duplicate_ads(monkeypatch):
    api = Api()
    rows = await ads.read(api, "client", ad_ids=[20], preview_limit=None)
    rows["ads"] *= 2
    monkeypatch.setattr(repair.ads, "read", AsyncMock(return_value=rows))
    result = await repair.readback(api, repair.normalize(ad_source(), "client"))
    assert not result["verified"]
    assert any(r["reason"] == "duplicate_readback_ids" for r in result["mismatches"])


@pytest.mark.asyncio
async def test_partial_repair_over_real_stdio(tmp_path):
    code = """
from test_repair_preflight import Api
from yadirect_mcp import server as s
async def check(pages):
    for page in pages:
        page.setdefault("site_check", {"ok": True, "checked": True})
    return pages
s.repair.link_checks.check_pages = check
s._client = Api()
s.mcp.run()
"""
    async def consent(context, params):
        return types.ElicitResult(action="accept", content={"approve": True})

    env = {**os.environ, "YD_TOKEN": "dummy", "YD_OUT_DIR": str(tmp_path),
           "YD_MODE": "campaign_setup", "YD_ALLOWED_LOGINS": "client"}
    parameters = StdioServerParameters(command=sys.executable, args=["-c", code], env=env)
    async with stdio_client(parameters) as (reader, writer), ClientSession(
        reader, writer, elicitation_callback=consent,
    ) as session:
        await session.initialize()
        payload = {"client_login": "client", "repair_bundle": {"ads": [{
            "id": "1921019359743476961", "sitelink_set_id": "41",
            "ad_extension_ids": ["51", "52"],
        }]}}
        result = await session.call_tool("direct_campaign_repair", payload)
        assert not result.isError, result
        assert result.structuredContent["preflight"]["status"] == "PASS"
        payload["confirmation"] = result.structuredContent["confirmation_required"]
        result = await session.call_tool("direct_campaign_repair", payload)
        for _ in range(100):
            if result.structuredContent.get("status") != "running":
                break
            await asyncio.sleep(0.02)
            result = await session.call_tool("direct_write_job", {
                "client_login": "client", "job_id": result.structuredContent["job_id"],
            })
        assert not result.isError, result
        assert result.structuredContent["readback"]["verified"]
        assert result.structuredContent["updates"]["ads"][0]["Id"] == "1921019359743476961"
        assert (await session.call_tool("direct_campaign_repair", payload)).isError
    journal = json.loads(next((tmp_path / "jobs").glob("*.json")).read_text(encoding="utf-8"))
    requests = [e for e in journal["events"] if e["stage"] == "request"]
    assert len(requests) == 1 and requests[0]["method"] == "update"
    assert requests[0]["params"]["Ads"][0]["ResponsiveAd"] == {
        "SitelinkSetId": "41", "CalloutSetting": {"AdExtensions": [
            {"AdExtensionId": "51", "Operation": "SET"},
            {"AdExtensionId": "52", "Operation": "SET"},
        ]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_selected_goals_checked_before_writes(monkeypatch, available):
    monkeypatch.setattr(repair.goals, "read", AsyncMock(return_value={
        "goals": [{"id": 77}] if available else [],
    }))
    api = Api()
    raw = {"campaigns": [{"id": 10, "priority_goals": [{"goal_id": "77", "value": 5}]}]}
    plan = repair.normalize(raw, "client")
    if available:
        assert (await repair.preflight(api, plan))["status"] == "PASS"
    else:
        with pytest.raises(ValueError, match="цели"):
            await repair.preflight(api, plan)
    assert not api.calls
