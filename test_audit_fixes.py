"""Regression coverage for the verified repository audit findings."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from yadirect_mcp import approval, artifacts, cleanup, config, landing, regions, store


def test_approval_expiry_pruning_capacity_and_one_time_use(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(approval.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(approval, "MAX_GRANTS", 3)
    registry = approval.ApprovalRegistry(ttl_seconds=10)
    old = registry.issue("client", "a")
    clock[0] += 10
    with pytest.raises(ValueError, match="истекло"):
        registry.consume(old.phrase, "client", "a")
    for i in range(10):
        registry.issue("client", str(i))
    assert len(registry._grants) == 3
    clock[0] += 10
    current = registry.issue("client", "new")
    assert len(registry._grants) == 1
    registry.consume(current.phrase, "client", "new")
    with pytest.raises(ValueError):
        registry.consume(current.phrase, "client", "new")


@pytest.mark.asyncio
async def test_regions_single_flight_and_retry_after_failure():
    regions.reset_cache()
    calls = 0

    class Api:
        async def call(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            if calls == 1:
                raise RuntimeError("temporary")
            return {"GeoRegions": [{"GeoRegionId": 213}]}

    try:
        results = await asyncio.gather(
            *(regions._load(Api(), "client") for _ in range(10)), return_exceptions=True,
        )
        assert calls == 2
        assert isinstance(results[0], RuntimeError)
        assert all(row == [{"GeoRegionId": 213}] for row in results[1:])
    finally:
        regions.reset_cache()


def test_malformed_tsv_is_readable_without_fabricated_columns(tmp_path):
    saved = store.persist(
        "Clicks\tCost\n1\t2\n3\t4\textra\n5\n",
        out_dir=tmp_path, stem="bad", inline_rows=2,
    )
    assert saved["parse_error"]
    first = store.read_back(tmp_path / "bad.tsv", 0, 2)
    second = store.read_back(tmp_path / "bad.tsv", first["next_offset"], 2)
    assert first["raw_lines"] == ["Clicks\tCost", "1\t2"]
    assert second["raw_lines"] == ["3\t4\textra", "5"]
    assert second["next_offset"] is None
    assert "data" not in second


def test_report_pages_cache_parse_and_invalidate_with_content_and_metadata(tmp_path, monkeypatch):
    saved = store.persist(
        "Clicks\tCost\n1\t2\n3\t4\n", out_dir=tmp_path, stem="good",
        inline_rows=0, metadata={"goals": ["77"]},
    )
    path = tmp_path / "good.tsv"
    parse = store.parse_tsv
    calls = []

    def counted(tsv):
        calls.append(tsv)
        return parse(tsv)

    monkeypatch.setattr(store, "parse_tsv", counted)
    assert store.read_back(path, 0, 1)["data"] == [{"Clicks": "1", "Cost": "2"}]
    assert store.read_back(path, 1, 1)["data"] == [{"Clicks": "3", "Cost": "4"}]
    assert len(calls) == 1
    path.write_text("Clicks\tCost\n11\t22\n", encoding="utf-8")
    assert "metadata_warning" in store.read_back(path, 0, 1)
    assert len(calls) == 2
    metadata = json.loads((tmp_path / "good.metadata.json").read_text(encoding="utf-8"))
    assert metadata["tsv_sha256"] == saved["metadata"]["tsv_sha256"]
    (tmp_path / "good.metadata.json").unlink()
    assert "старого" in store.read_back(path, 0, 1)["metadata_warning"]


@pytest_asyncio.fixture
async def public_dns(monkeypatch):
    async def resolve(host, port, **kwargs):
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = "93.184.216.34"
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/", "http://user:password@example.com/",
    "http://[invalid-ipv6]/",
    "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/",
    "http://[::1]/", "http://[fe80::1]/", "http://[::ffff:127.0.0.1]/",
    "http://224.0.0.1/", "http://0.0.0.0/",
])
async def test_landing_rejects_unsafe_targets(url, public_dns, respx_mock):
    result = await landing.inspect_pages([{"url": url}])
    assert result[0]["ok"] is False
    assert not respx_mock.calls


@pytest.mark.asyncio
async def test_landing_rejects_dns_with_any_private_answer(monkeypatch):
    resolver = AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
    ])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    with pytest.raises(ValueError, match="непубличным"):
        await landing._public_target("https://example.test/")


@pytest.mark.asyncio
async def test_landing_pins_ip_and_revalidates_redirect(public_dns, respx_mock):
    route = respx_mock.get("https://93.184.216.34/").respond(
        302, headers={"Location": "http://169.254.169.254/latest/meta-data/"},
    )
    result = await landing.inspect_pages([{"url": "https://example.test/"}])
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["Host"] == "example.test"
    assert request.extensions["sni_hostname"] == "example.test"
    assert result[0]["ok"] is False
    assert "непубличным" in result[0]["error"]


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, blocks):
        self.blocks = blocks
        self.read = 0
        self.closed = False

    async def __aiter__(self):
        for block in self.blocks:
            self.read += len(block)
            yield block

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_landing_stream_is_bounded_and_closed(public_dns, respx_mock, monkeypatch):
    monkeypatch.setattr(landing, "MAX_HTML_BYTES", 100_000)
    body = CountingStream([b"<title>OK</title>"] + [b"x" * 65536] * 100)
    respx_mock.get("https://93.184.216.34/").respond(200, stream=body)
    result = await landing.inspect_pages([{"url": "https://example.test/"}])
    assert result[0]["ok"] is True
    assert result[0]["title"] == "OK"
    assert result[0]["html_truncated"] is True
    assert body.read < 200_000
    assert body.closed


@pytest.mark.asyncio
async def test_landing_rejects_invalid_compressed_body(public_dns, respx_mock):
    body = CountingStream([b"zip bomb"] * 100)
    respx_mock.get("https://93.184.216.34/").respond(
        200, stream=body, headers={"Content-Encoding": "gzip"},
    )
    result = await landing.inspect_pages([{"url": "https://example.test/"}])
    assert result[0]["ok"] is False
    assert body.read <= 65536
    assert body.closed


@pytest.mark.asyncio
async def test_landing_concurrency_order_and_total_timeout(monkeypatch):
    active = 0
    peak = 0

    async def inspect(_client, url):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(1)
            return {"url": url, "ok": True}
        finally:
            active -= 1

    monkeypatch.setattr(landing, "_inspect", inspect)
    monkeypatch.setattr(landing, "BATCH_TIMEOUT_SECONDS", 0.02)
    pages = [{"url": f"https://example.test/{i}"} for i in range(40)]
    results = await landing.inspect_pages(pages, timeout_seconds=0.01)
    assert len(results) == landing.MAX_PAGES
    assert [row["url"] for row in results] == [row["url"] for row in pages[:30]]
    assert all(row["ok"] is False for row in results)
    assert 1 < peak <= landing.MAX_CONCURRENT
    assert active == 0


def test_large_audit_summary_is_bounded_and_complete_json_remains_readable(tmp_path):
    payload = {
        "client_login": "client", "ready": False, "summary": {"BLOCK": 50},
        "campaigns": [{
            "campaign": {"id": i, "name": f"Campaign {i}", "raw": "x" * 100_000},
            "findings": [{"rule": "big", "status": "BLOCK", "message": "bad" * 1000,
                          "evidence": list(range(10_000))}],
        } for i in range(50)],
    }
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    summary = artifacts.compact({**payload, "artifact_path": str(path)})
    assert len(json.dumps(summary)) < 30_000
    assert summary["findings_total"] == 50
    assert summary["findings_truncated"]
    offset = 0
    chunks = []
    while True:
        page = artifacts.read(path, "/campaigns/49/campaign/raw", offset, 16000)
        chunks.append(page["json_fragment"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert json.loads("".join(chunks)) == "x" * 100_000


def test_publication_requires_explicit_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("YD_TOKEN", "dummy")
    monkeypatch.setenv("YD_OUT_DIR", str(tmp_path))
    for key in ("SSH_HOST", "REMOTE_ROOT", "PUBLIC_BASE_URL"):
        monkeypatch.delenv("YD_CREATE_REPORT_" + key, raising=False)
    settings = config.load()
    assert settings.create_report_remote_root == ""
    assert settings.create_report_public_base_url == ""
    monkeypatch.setenv("YD_CREATE_REPORT_SSH_HOST", "publisher")
    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL"):
        config.load()


def test_cleanup_dry_run_and_apply_preserve_paid_exports_and_journals(tmp_path):
    for name in ["plan_old.json", "audit_old.json", "apply_old.json", "paid.tsv"]:
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (1, 1))
    (tmp_path / "plan_new.json").write_text("{}", encoding="utf-8")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "plan_old.json").write_text("{}", encoding="utf-8")
    dry = cleanup.prune(tmp_path, keep_latest=1)
    assert len(dry["files"]) == 2
    assert len(list(tmp_path.iterdir())) == 6
    actual = cleanup.prune(tmp_path, keep_latest=1, apply=True)
    assert actual["removed"] == dry["files"]
    assert (tmp_path / "paid.tsv").exists()
    assert (tmp_path / "apply_old.json").exists()
    assert (backups / "plan_old.json").exists()
