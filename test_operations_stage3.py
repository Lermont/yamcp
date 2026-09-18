"""Operational regressions: no real Direct calls, SSH, SCP or HTTP."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from test_server import _in_server
from yadirect_mcp import config, jobs, operations, publication, runtime


def fixture(tmp_path):
    settings = config.Settings(token="SECRET", agency_login=None,
        allowed_logins=frozenset({"client"}), out_dir=tmp_path, sandbox=True,
        max_inflight=1, inline_rows=10, report_deadline=10, lang="ru", mode="campaign_setup",
        create_report_ssh_host="example-host", create_report_remote_root="/reports",
        create_report_public_base_url="https://example.test/reports")
    job_id = "a" * 32
    html = tmp_path / "report.html"
    html.write_text("<html>report</html>", encoding="utf-8")
    report = publication.snapshot(html, tmp_path, job_id)
    payload = {"job_id": job_id, "kind": "apply", "client_login": "client",
               "plan_hash": "b" * 64, "status": "done", "uncertain": False,
               "created_at": 1, "events": [], "result": {
                   "status": "complete_unverified", "executed": True,
                   "api_creation_complete": True, "campaigns": [{"id": 1921019359743476961}],
                   "creation_report": {**report, "status": "publish_failed", "verified": False},
                   "required_manual_actions": [{"rule": "ads.action_button", "required": True,
                                                "ad_id": "1921019359743476961"},
                                               {"rule": "launch.moderation", "required": True}]}}
    path = tmp_path / "jobs" / (job_id + ".json")
    jobs._atomic(path, payload)
    return settings, job_id, path


def test_runtime_has_identity_and_no_secrets(tmp_path, monkeypatch):
    settings, _, _ = fixture(tmp_path)
    info = runtime.describe(settings)
    assert "SECRET" not in json.dumps(info) and "token" not in info
    assert info["protocol_revision"] == "2026-09-17-ops-p3"
    assert not info["restart_required"]
    monkeypatch.setattr(runtime, "fingerprint", lambda: "changed")
    assert runtime.describe(settings)["restart_required"]
    assert runtime.describe(settings)["loaded_source_sha256"] == info["loaded_source_sha256"]


def test_registry_scopes_deduplicates_and_paginates(tmp_path):
    settings, job_id, path = fixture(tmp_path)
    payload = json.loads(path.read_text())
    payload["result"]["required_manual_actions"] *= 2
    jobs._atomic(path, payload)
    first = operations.pending(settings, "client", limit=1)
    assert first["complete"] and first["total_pending_jobs"] == 1
    actions = first["jobs"][0]["actions"]
    assert {a["rule"] for a in actions} == {
        "verification.api", "ads.action_button", "launch.moderation", "publication.report"}
    assert len(actions) == 4 and len({a["action_id"] for a in actions}) == 4
    assert actions == operations.pending(settings, "client")["jobs"][0]["actions"]
    assert operations.pending(settings, "client", offset=1)["jobs"] == []
    with pytest.raises(ValueError):
        operations.pending(settings, "another")
    assert not first["live_account_checked"]


@pytest.mark.parametrize("offset,limit", [(-1, 10), (0, 0), (0, 501), (True, 1), (0, True)])
def test_registry_rejects_invalid_page(tmp_path, offset, limit):
    settings, _, _ = fixture(tmp_path)
    with pytest.raises(ValueError):
        operations.pending(settings, "client", offset=offset, limit=limit)


@pytest.mark.parametrize("status,uncertain,rule", [
    ("running", False, "write.running"), ("done", True, "write.reconciliation"),
    ("failed", False, "write.incomplete"), ("interrupted", False, "write.reconciliation")])
def test_registry_retains_attention_required(tmp_path, status, uncertain, rule):
    settings, _, path = fixture(tmp_path)
    data = json.loads(path.read_text())
    data.update(status=status, uncertain=uncertain)
    jobs._atomic(path, data)
    assert rule in {a["rule"] for a in operations.pending(settings, "client")["jobs"][0]["actions"]}


def test_registry_does_not_claim_complete_on_corrupt_journal(tmp_path):
    settings, _, _ = fixture(tmp_path)
    (tmp_path / "jobs" / ("c" * 32 + ".json")).write_text("broken")
    value = operations.pending(settings, "client")
    assert not value["complete"] and value["unreadable_records"] == 1


def test_publication_preview_binds_html_job_and_destination(tmp_path):
    settings, job_id, path = fixture(tmp_path)
    before = path.read_bytes()
    plan = publication.prepare(settings, "client", job_id)
    assert plan["source_job_id"] == job_id and plan["summary"]["advertising_writes"] == 0
    altered = publication.prepare(replace(settings, create_report_remote_root="/elsewhere"),
                                  "client", job_id, attempt=plan["attempt"])
    assert altered["plan_hash"] != plan["plan_hash"]
    assert path.read_bytes() == before
    Path(plan["artifact_path"]).write_text("changed")
    with pytest.raises(ValueError, match="HTML изменился"):
        publication.prepare(settings, "client", job_id)


@pytest.mark.parametrize("field,value", [("status", "running"), ("uncertain", True),
                                         ("kind", "repair")])
def test_publication_rejects_unsafe_source_job(tmp_path, field, value):
    settings, job_id, path = fixture(tmp_path)
    data = json.loads(path.read_text())
    data[field] = value
    jobs._atomic(path, data)
    with pytest.raises(ValueError):
        publication.prepare(settings, "client", job_id)


def test_publication_blocks_foreign_login_and_outside_artifact(tmp_path):
    settings, job_id, path = fixture(tmp_path)
    with pytest.raises(PermissionError):
        publication.prepare(settings, "other", job_id)
    data = json.loads(path.read_text())
    data["result"]["creation_report"]["artifact_path"] = str(tmp_path.parent / "outside.html")
    jobs._atomic(path, data)
    with pytest.raises(ValueError, match="YD_OUT_DIR"):
        publication.prepare(settings, "client", job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True, "wrong_hash"])
async def test_recovery_is_independent_and_preserves_original(tmp_path, monkeypatch, fail):
    settings, job_id, path = fixture(tmp_path)
    original = path.read_bytes()
    plan = publication.prepare(settings, "client", job_id)
    calls = []
    def publish(local_path, **kwargs):
        calls.append(kwargs)
        if fail is True:
            raise RuntimeError("offline")
        return {"status": "published", "verified": True,
                "sha256": "bad" if fail else hashlib.sha256(local_path.read_bytes()).hexdigest()}
    monkeypatch.setattr(publication.creation_report, "publish", publish)
    async def operation(journal):
        return await publication.apply(settings, plan, journal)
    publish_id = jobs.start(tmp_path, "client", plan["plan_hash"], "publish", operation)
    await jobs.wait_briefly(publish_id, seconds=2)
    result = jobs.read(tmp_path, publish_id, "client")["result"]
    assert result["status"] == ("publish_failed" if fail else "published")
    assert not result["advertising_writes"] and result["source_job_id"] == job_id
    assert path.read_bytes() == original and len(calls) == 1
    current = publication.latest(tmp_path, jobs.read(tmp_path, job_id, "client"))
    assert current["job_id"] == publish_id
    source_actions = next(row["actions"] for row in operations.pending(settings, "client")["jobs"]
                          if row["job_id"] == job_id)
    assert ("publication.report" in {a["rule"] for a in source_actions}) == bool(fail)
    jobs.start(tmp_path, "client", plan["plan_hash"], "publish", operation)
    await jobs.wait_briefly(publish_id)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_html_changed_after_preview_never_publishes(tmp_path, monkeypatch):
    settings, job_id, _ = fixture(tmp_path)
    plan = publication.prepare(settings, "client", job_id)
    Path(plan["artifact_path"]).write_text("changed")
    calls = []
    monkeypatch.setattr(publication.creation_report, "publish", lambda *a, **kw: calls.append(1))
    async def operation(journal):
        return await publication.apply(settings, plan, journal)
    pid = jobs.start(tmp_path, "client", plan["plan_hash"], "publish", operation)
    await jobs.wait_briefly(pid, seconds=2)
    assert jobs.read(tmp_path, pid, "client")["status"] == "failed"
    assert calls == []


def test_snapshot_does_not_follow_later_report_overwrite(tmp_path):
    settings, job_id, _ = fixture(tmp_path)
    path = tmp_path / "mutable.html"
    path.write_text("first")
    one = publication.snapshot(path, tmp_path, job_id)
    path.write_text("second")
    two = publication.snapshot(path, tmp_path, job_id)
    assert one != two and Path(one["artifact_path"]).read_text() == "first"


def test_mcp_readonly_registry_and_runtime_do_not_create_api_client(tmp_path):
    fixture(tmp_path)
    result = _in_server("""import asyncio,json
import yadirect_mcp.server as s
async def run():
    info=await s.mcp.call_tool('direct_runtime',{})
    pending=await s.mcp.call_tool('direct_pending_actions',{'client_login':'client'})
    return {'runtime':info.structuredContent,'pending':pending.structuredContent,
            'client_created':s._client is not None}
print(json.dumps(asyncio.run(run())))""", str(tmp_path), YD_MODE="report")
    assert not result["client_created"]
    assert result["runtime"]["protocol_revision"] == "2026-09-17-ops-p3"
    assert result["pending"]["total_pending_jobs"] == 1


def test_publish_tool_requires_consent_and_single_use_token(tmp_path):
    _, job_id, _ = fixture(tmp_path)
    result = _in_server(f"""import asyncio,json
from types import SimpleNamespace
from test_approval_compatibility import host
import yadirect_mcp.server as s
calls=[]
def publish(path,**kwargs):
    calls.append(1)
    import hashlib
    return {{'status':'published','verified':True,
             'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
s.creation_report.publish=publish
async def run():
    p=await s.direct_publish_job('client','{job_id}')
    token=p.structuredContent['confirmation_required']
    refused=await s.direct_publish_job('client','{job_id}',token,ctx=host('decline'))
    assert not calls
    ok=await s.direct_publish_job('client','{job_id}',token,ctx=host('accept',True))
    reused=await s.direct_publish_job('client','{job_id}',token,ctx=host('accept',True))
    return {{'refused':refused.isError,'ok':ok.structuredContent,'reused':reused.isError,
             'calls':len(calls),'api':s._client is not None}}
print(json.dumps(asyncio.run(run())))""", str(tmp_path), YD_MODE="campaign_setup",
        YD_CREATE_REPORT_SSH_HOST="example-host", YD_CREATE_REPORT_REMOTE_ROOT="/reports",
        YD_CREATE_REPORT_PUBLIC_BASE_URL="https://example.test/reports")
    assert result["refused"] and result["reused"]
    assert result["ok"]["status"] == "published" and result["calls"] == 1
    assert not result["api"]


@pytest.mark.parametrize("kind,status,rule", [
    ("preview", "preview", "preview.review"),
    ("assets", "readback_failed", "verification.assets"),
])
def test_registry_includes_unapplied_previews_and_failed_image_checks(tmp_path, kind, status, rule):
    settings, _, path = fixture(tmp_path)
    payload = json.loads(path.read_text())
    payload["kind"] = kind
    payload["result"]["status"] = status
    payload["result"]["executed"] = kind != "preview"
    jobs._atomic(path, payload)
    assert rule in {a["rule"] for a in operations.pending(settings, "client")["jobs"][0]["actions"]}


def test_applied_preview_no_longer_requires_review(tmp_path):
    settings, _, path = fixture(tmp_path)
    payload = json.loads(path.read_text())
    payload.update(kind="preview", job_id="c" * 32)
    payload["result"] = {"status": "preview", "executed": False}
    jobs._atomic(path.parent / (payload["job_id"] + ".json"), payload)
    assert operations.pending(settings, "client")["total_pending_jobs"] == 1


def test_action_identity_survives_status_change(tmp_path):
    settings, _, path = fixture(tmp_path)
    def api_action():
        return next(a for a in operations.pending(settings, "client")["jobs"][0]["actions"]
                    if a["rule"] == "verification.api")
    first = api_action()
    payload = json.loads(path.read_text())
    payload["result"]["readback"] = {"verified": False}
    jobs._atomic(path, payload)
    second = api_action()
    assert first["status"] != second["status"] and first["action_id"] == second["action_id"]


def test_failed_initial_publication_keeps_recoverable_snapshot(tmp_path):
    _, job_id, _ = fixture(tmp_path)
    result = _in_server(f"""import asyncio,json
from pathlib import Path
from unittest.mock import AsyncMock
import yadirect_mcp.server as s
source=s.jobs.read(s.SETTINGS.out_dir,'{job_id}','client')
plan={{'client_login':'client','plan_hash':source['plan_hash']}}
source['result'].update(client_login='client',plan_hash=source['plan_hash'])
s.executor.apply=AsyncMock(return_value=source['result'])
s.executor.readback=AsyncMock(return_value={{'verified':True}})
s.creation_report.build_model=lambda *args: {{}}
def persist(*args):
    path=s.SETTINGS.out_dir/'mutable.html'
    path.write_text('<html>fixed report</html>')
    return path
s.creation_report.persist=persist
def publish(*args,**kwargs): raise RuntimeError('offline')
s.creation_report.publish=publish
journal=s.jobs.Journal(s.SETTINGS.out_dir/'jobs'/('{job_id}'+'.json'),source)
result=asyncio.run(s._execute_campaign(object(),plan,{{}},journal))
report=result['creation_report']
print(json.dumps({{'report':report,'exists':Path(report['artifact_path']).is_file()}}))
""", str(tmp_path), YD_MODE="campaign_setup", YD_CREATE_REPORT_SSH_HOST="example-host",
        YD_CREATE_REPORT_REMOTE_ROOT="/reports",
        YD_CREATE_REPORT_PUBLIC_BASE_URL="https://example.test/reports")
    assert result["report"]["status"] == "publish_failed" and result["exists"]
    assert len(result["report"]["sha256"]) == 64
