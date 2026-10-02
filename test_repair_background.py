"""Offline regressions for expensive repair previews and transport URL deduplication."""
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from test_approval_compatibility import host
from test_group_append import source
from test_repair_preflight import server as repair_server_fixture
from yadirect_mcp import approval, jobs, landing, repair

server = repair_server_fixture


@pytest.mark.asyncio
async def test_fragment_transport_dedup_preserves_queries_devices_and_evidence(monkeypatch,
                                                                              respx_mock):
    async def target(url):
        return httpx.URL(url), "93.184.216.34"
    monkeypatch.setattr(landing, "_public_target", target)
    monkeypatch.setattr(landing, "HOST_INTERVAL", 0)
    route = respx_mock.route().respond(200, text="<main><h1>Квартиры</h1></main>")
    pages = [{"url": "https://example.test/?ad=1#" + x} for x in ["quiz", "about", "contacts"]]
    pages.append({"url": "https://example.test/?ad=2#quiz"})
    token = landing.CACHE_SCOPE.set("test-fragments")
    try:
        rows = await landing.inspect_pages(pages)
        assert route.call_count == 2
        assert [r["url"] for r in rows] == [p["url"] for p in pages]
        assert all(r["ok"] and r["content"]["review_required"] for r in rows)
        assert all("#" not in r["http_request_url"] for r in rows)
        rows[0]["content"]["signals"].append("changed")
        assert "changed" not in rows[1]["content"]["signals"]
        await landing.inspect_pages(pages)
        assert route.call_count == 2
        await landing.inspect_pages(pages, user_agent=landing.MOBILE_USER_AGENT)
        assert route.call_count == 4
    finally:
        landing.CACHE_SCOPE.reset(token)
        landing._CACHE.clear()


@pytest.mark.asyncio
async def test_append_preflight_is_background_and_binds_evidence(server, monkeypatch):
    gate = asyncio.Event()
    evidence = {"status": "PASS", "source_hash": "source", "before": {}}

    async def slow(*args):
        await gate.wait()
        return evidence
    monkeypatch.setattr(repair, "preflight", slow)
    initial = (await server.direct_campaign_repair("client", source())).structuredContent
    assert initial["status"] == "running"
    assert initial["job_id"] and initial["poll_tool"] == "direct_write_job"
    assert not approval.REGISTRY._grants
    assert not server._client.calls
    gate.set()
    await jobs.wait_briefly(initial["job_id"], seconds=2)
    # Completed preview must survive token-registry recreation, using journal evidence.
    monkeypatch.setattr(approval, "REGISTRY", approval.ApprovalRegistry())
    preview = (await server.direct_write_job("client", initial["job_id"])).structuredContent
    assert preview["status"] == "preview" and preview["preview_kind"] == "repair"
    token = preview["confirmation_required"]
    grant = approval.REGISTRY.validate(token, "client", preview["plan_hash"])
    assert grant.evidence == evidence
    apply = AsyncMock(return_value={"status": "complete", "preflight": evidence})
    monkeypatch.setattr(repair, "apply", apply)
    monkeypatch.setattr(repair, "readback", AsyncMock(return_value={"verified": True}))
    refused = await server.direct_campaign_repair("client", source(), token, ctx=host("decline"))
    assert refused.isError and not apply.called
    accepted = await server.direct_campaign_repair("client", source(), token, ctx=host())
    assert not accepted.isError
    assert apply.call_args.kwargs["expected_preflight"] == evidence
    repeated = await server.direct_campaign_repair("client", source(), token, ctx=host())
    assert repeated.isError and apply.call_count == 1


@pytest.mark.asyncio
async def test_background_failure_never_issues_confirmation(server, monkeypatch):
    monkeypatch.setattr(repair, "preflight", AsyncMock(side_effect=ValueError("URL BLOCK")))
    response = (await server.direct_campaign_repair("client", source())).structuredContent
    await jobs.wait_briefly(response["job_id"], seconds=2)
    result = (await server.direct_write_job("client", response["job_id"])).structuredContent
    assert result["status"] == "blocked"
    assert "confirmation_required" not in result
    assert not approval.REGISTRY._grants and not server._client.calls


@pytest.mark.asyncio
async def test_repair_http_scope_restored_after_failure(monkeypatch):
    async def fail(api, plan):
        assert landing.CACHE_SCOPE.get() == "client:repair:hash"
        raise ValueError("broken")
    monkeypatch.setattr(repair, "_preflight", fail)
    previous = landing.CACHE_SCOPE.get()
    with pytest.raises(ValueError, match="broken"):
        await repair.preflight(None, {"client_login": "client", "plan_hash": "hash"})
    assert landing.CACHE_SCOPE.get() == previous
