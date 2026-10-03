"""Local UI receipts: no Direct, browser, SSH, or real account writes."""
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from test_operations_stage3 import fixture as base_fixture
from test_server import _in_server
from yadirect_mcp import executor, jobs, operations, verification, workflow
from yadirect_mcp import manual_review as mr


def recheck(settings, job_id, *, verified=True, age=1):
    job = jobs.read(settings.out_dir, job_id, "client")
    directory = settings.out_dir / "jobs" / "verifications" / job_id
    directory.mkdir(parents=True, exist_ok=True)
    record = {"verification_id": str(time.time_ns()), "job_id": job_id,
              "client_login": "client", "plan_hash": job["plan_hash"],
              "source_journal_sha256": mr._journal_hash(settings.out_dir, job),
              "checked_at": time.time() - age, "readback": {"verified": verified}}
    jobs._atomic(directory / (str(time.time_ns()) + ".json"), record)


def fixture(tmp_path, rule="ads.action_button"):
    settings, job_id, path = base_fixture(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    action = {"rule": rule, "required": True, "campaign_id": "123", "group_id": "456",
              "ad_id": "1921019359743476961",
              "requested": {"text": "Подробнее", "href": "https://example.test/"}}
    data["updated_at"] = time.time() - 20
    data["result"]["required_manual_actions"] = [action, {"rule": "launch.moderation",
                                                         "required": True}]
    data["result"].pop("creation_report")
    jobs._atomic(path, data)
    evidence = tmp_path / "ui-evidence" / "observation.txt"
    evidence.parent.mkdir()
    evidence.write_text("Saved UI observation with IDs and actual values.", encoding="utf-8")
    fields = {"ads.action_button": action["requested"],
              "network.carousel": {"slides": ["image-a", "image-b"],
                                   "order_checked": True, "images_loaded": True},
              "ads.neuro_ad": {"landing_url": action["requested"]["href"],
                               "generation_status": "ready", "texts_checked": True,
                               "images_checked": True}}[rule]
    review = {"outcome": "verified", "reviewer": "Specialist", "saved_and_reopened": True,
              "checked_at": datetime.now(UTC).isoformat(), "checked_fields": fields,
              "evidence_paths": [str(evidence)]}
    recheck(settings, job_id)
    return settings, job_id, path, workflow.action_id(job_id, action), review


async def commit(settings, job_id, action_id, review):
    plan = mr.prepare(settings, "client", job_id, action_id, review)
    async def operation(journal):
        return await mr.apply(settings, plan, journal)
    receipt = jobs.start(settings.out_dir, "client", plan["plan_hash"], "manual_review", operation)
    await jobs.wait_briefly(receipt)
    row = jobs.read(settings.out_dir, receipt, "client")
    assert row["status"] == "done", row
    return row


def current(settings, job_id):
    return mr.current(settings.out_dir, jobs.read(settings.out_dir, job_id, "client"))


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", sorted(mr.RULES))
async def test_receipt_completes_only_ui_and_preserves_original(tmp_path, rule):
    settings, job_id, path, action, review = fixture(tmp_path, rule)
    before = path.read_bytes()
    assert not current(settings, job_id)["setup_complete"]
    receipt = await commit(settings, job_id, action, review)
    assert path.read_bytes() == before
    assert not receipt["result"]["advertising_writes"]
    assert receipt["result"]["verification_method"] == "reviewer_attested_saved_ui"
    Path(review["evidence_paths"][0]).unlink()
    state = current(settings, job_id)
    assert state["setup_complete"]
    assert state["workflow"]["ui_verification"] == "verified_by_reviewer"
    assert state["workflow"]["launch"] == "requires_explicit_instruction"
    pending = operations.pending(settings, "client")
    assert pending["complete"]
    assert [a["rule"] for a in pending["jobs"][0]["actions"]] == ["launch.moderation"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "tamper", "revoke", "failed_api", "related_write"])
async def test_receipt_invalidates_and_returns_to_pending(tmp_path, monkeypatch, change):
    settings, job_id, _, action, review = fixture(tmp_path)
    receipt = await commit(settings, job_id, action, review)
    expected = {"expired": "expired", "tamper": "pending_ui", "revoke": "invalidated",
                "failed_api": "stale", "related_write": "stale"}[change]
    if change == "expired":
        now = time.time()
        monkeypatch.setattr(mr.time, "time", lambda: now + mr.MAX_AGE + 1)
    elif change == "tamper":
        Path(receipt["result"]["evidence"][0]["artifact_path"]).write_text("changed")
    elif change == "revoke":
        await commit(settings, job_id, action, {"outcome": "invalidated", "reviewer": "S",
                                              "reason": "Button changed in UI"})
    elif change == "failed_api":
        recheck(settings, job_id, verified=False, age=0)
        recheck(settings, job_id, verified=True, age=0)
    else:
        jobs._atomic(settings.out_dir / "jobs" / ("c" * 32 + ".json"), {
            "job_id": "c" * 32, "kind": "repair", "status": "done", "client_login": "client",
            "plan_hash": "c" * 64, "events": [{"stage": "request", "at": time.time(),
                "params": {"Ads": [{"Id": "1921019359743476961"}]}}]})
    state = current(settings, job_id)
    assert not state["setup_complete"]
    assert state["manual_review"]["actions"][0]["status"] == expected
    pending = operations.pending(settings, "client")
    assert any(a["rule"] == "ads.action_button" for j in pending["jobs"] for a in j["actions"])


@pytest.mark.asyncio
async def test_unrelated_write_does_not_invalidate(tmp_path):
    settings, job_id, _, action, review = fixture(tmp_path)
    await commit(settings, job_id, action, review)
    jobs._atomic(settings.out_dir / "jobs" / ("d" * 32 + ".json"), {
        "job_id": "d" * 32, "kind": "repair", "client_login": "client",
        "events": [{"stage": "request", "params": {"Ads": [{"Id": "999"}]}}]})
    assert current(settings, job_id)["setup_complete"]


@pytest.mark.parametrize("bad", ["old_api", "failed_api", "outside", "reopened", "fields",
                                 "old_date", "future", "login", "action", "partial", "uncertain"])
def test_rejects_untrustworthy_inputs(tmp_path, bad):
    settings, job_id, path, action, review = fixture(tmp_path)
    login = "client"
    if bad == "old_api":
        recheck(settings, job_id, age=1000)
    elif bad == "failed_api":
        recheck(settings, job_id, verified=False)
    elif bad == "outside":
        review["evidence_paths"] = [str(path)]
    elif bad == "reopened":
        review["saved_and_reopened"] = False
    elif bad == "fields":
        review["checked_fields"]["href"] = "https://wrong.test/"
    elif bad in {"old_date", "future"}:
        review["checked_at"] = datetime.fromtimestamp(
            time.time() + (1000 if bad == "future" else -1000), UTC).isoformat()
    elif bad == "login":
        login = "foreign"
    elif bad == "action":
        action = "e" * 32
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if bad == "partial":
            data["result"]["api_creation_complete"] = False
        else:
            data["uncertain"] = True
        jobs._atomic(path, data)
    with pytest.raises((ValueError, PermissionError)):
        mr.prepare(settings, login, job_id, action, review)


@pytest.mark.asyncio
async def test_changed_evidence_after_preview_never_completes(tmp_path):
    settings, job_id, _, action, review = fixture(tmp_path)
    plan = mr.prepare(settings, "client", job_id, action, review)
    Path(review["evidence_paths"][0]).write_text("different")
    async def operation(journal):
        return await mr.apply(settings, plan, journal)
    receipt = jobs.start(settings.out_dir, "client", plan["plan_hash"], "manual_review", operation)
    await jobs.wait_briefly(receipt)
    assert jobs.read(settings.out_dir, receipt, "client")["status"] == "failed"
    assert not current(settings, job_id)["setup_complete"]


@pytest.mark.asyncio
async def test_recheck_returns_current_ui_state_without_mutating_source(tmp_path, monkeypatch):
    settings, job_id, path, action, review = fixture(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    encoded = json.dumps({"plan_hash": data["plan_hash"], "client_login": "client"})
    data.update(verification_plan_json=encoded,
                verification_plan_sha256=hashlib.sha256(encoded.encode()).hexdigest())
    jobs._atomic(path, data)
    recheck(settings, job_id)
    await commit(settings, job_id, action, review)
    before = path.read_bytes()
    monkeypatch.setattr(executor, "readback", AsyncMock(return_value={"verified": True}))
    monkeypatch.setattr(verification.creative, "executed_actions", lambda *args: [])
    record = await verification.run(object(), settings.out_dir, "client", job_id)
    assert record["setup_complete"]
    assert path.read_bytes() == before
    monkeypatch.setattr(executor, "readback", AsyncMock(return_value={"verified": False}))
    failed = await verification.run(object(), settings.out_dir, "client", job_id)
    assert not failed["setup_complete"]


def test_tool_guards_token_readback_and_no_api(tmp_path):
    _, job_id, _, action, review = fixture(tmp_path)
    value = _in_server(f'''import asyncio,json
from test_approval_compatibility import host
import yadirect_mcp.server as s
review=json.loads({json.dumps(review)!r})
async def run():
    args=('client','{job_id}','{action}')
    p=await s.direct_manual_review(*args,review)
    token=p.structured_content['confirmation_required']
    changed=await s.direct_manual_review(*args,dict(review,reviewer='Other'),token)
    denied=await s.direct_manual_review(*args,review,token,ctx=host('decline'))
    ok=await s.direct_manual_review(*args,review,token,ctx=host('accept',True))
    reused=await s.direct_manual_review(*args,review,token,ctx=host('accept',True))
    read=await s.direct_write_job('client','{job_id}')
    return dict(changed=changed.is_error,denied=denied.is_error,reused=reused.is_error,
                ok=ok.structured_content,read=read.structured_content,api=s._client is not None)
print(json.dumps(asyncio.run(run())))''', str(tmp_path), YD_MODE="campaign_setup",
        YD_APPROVAL_MODE="elicitation", YD_ALLOWED_CLIENTS="client")
    assert value["changed"] and value["denied"] and value["reused"]
    assert value["ok"]["status"] == "recorded"
    assert value["read"]["setup_complete"] and not value["api"]


@pytest.mark.asyncio
async def test_other_required_actions_stay_pending(tmp_path):
    settings, job_id, path, action, review = fixture(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["result"]["required_manual_actions"].append({
        "rule": "product.generated_offers", "campaign_id": "123", "required": True})
    jobs._atomic(path, data)
    recheck(settings, job_id)
    await commit(settings, job_id, action, review)
    assert not current(settings, job_id)["setup_complete"]
    pending = operations.pending(settings, "client")["jobs"][0]["actions"]
    assert {a["rule"] for a in pending} == {"launch.moderation", "product.generated_offers"}
    for item in pending:
        with pytest.raises(ValueError, match="action_id"):
            mr.prepare(settings, "client", job_id, item["action_id"], review)


@pytest.mark.asyncio
async def test_corrupt_receipt_cannot_fall_back_to_earlier_success(tmp_path):
    settings, job_id, _, action, review = fixture(tmp_path)
    await commit(settings, job_id, action, review)
    newer = await commit(settings, job_id, action, review)
    path = settings.out_dir / "jobs" / (newer["job_id"] + ".json")
    newer["verification_plan_sha256"] = "wrong"
    jobs._atomic(path, newer)
    state = current(settings, job_id)
    assert not state["setup_complete"] and not state["manual_review"]["complete"]


@pytest.mark.asyncio
async def test_client_lock_prevents_receipt_write(tmp_path):
    settings, job_id, _, action, review = fixture(tmp_path)
    plan = mr.prepare(settings, "client", job_id, action, review)
    lock = settings.out_dir / "jobs" / (
        "login-" + hashlib.sha256(b"client").hexdigest() + ".lock")
    lock.write_text("other job")
    operation = AsyncMock()
    with pytest.raises(ValueError, match="логин"):
        jobs.start(settings.out_dir, "client", plan["plan_hash"], "manual_review", operation)
    operation.assert_not_awaited()
    assert not current(settings, job_id)["setup_complete"]


@pytest.mark.asyncio
async def test_newly_discovered_manual_action_cannot_disappear(tmp_path, monkeypatch):
    settings, job_id, path, action_id, review = fixture(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    action = data["result"]["required_manual_actions"].pop(0)
    plan = {"plan_hash": data["plan_hash"], "client_login": "client"}
    encoded = json.dumps(plan)
    data.update(verification_plan_json=encoded,
                verification_plan_sha256=hashlib.sha256(encoded.encode()).hexdigest())
    jobs._atomic(path, data)
    monkeypatch.setattr(executor, "readback", AsyncMock(return_value={"verified": True}))
    monkeypatch.setattr(verification.creative, "executed_actions", lambda *args: [action])
    record = await verification.run(object(), settings.out_dir, "client", job_id)
    assert not record["setup_complete"]
    assert current(settings, job_id)["workflow"]["ui_verification"] == "pending"
    pending = operations.pending(settings, "client")["jobs"][0]["actions"]
    assert any(a["action_id"] == action_id for a in pending)
    await commit(settings, job_id, action_id, review)
    assert current(settings, job_id)["setup_complete"]
