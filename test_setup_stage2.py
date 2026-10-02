"""Stage 2 regressions. All Direct calls are fake; no account writes or HTTP."""
import asyncio
import base64
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from test_executor import FakeApi, compiled
from test_server import _in_server
from yadirect_mcp import assets, executor, image_validation, jobs, verification, workflow

BIG = 1921019359743476961


def picture(size=(450, 450), fmt="PNG"):
    stream = BytesIO()
    Image.new("RGB", size, "white").save(stream, format=fmt)
    return stream.getvalue()


def image_bundle(data, kind="AUTO"):
    return {"images": [{"name": "test", "type": kind,
                        "image_data": base64.b64encode(data).decode()}]}


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF"])
def test_valid_decoded_image_manifest(fmt):
    data = picture(fmt=fmt)
    plan = assets.normalize(image_bundle(data), "client")
    meta = plan["image_manifest"][0]
    assert (meta["format"], meta["width"], meta["height"]) == (fmt, 450, 450)
    assert meta["decoded"] and meta["frames"] == 1
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()
    assert base64.b64decode(plan["images"][0]["ImageData"]) == data


@pytest.mark.parametrize("size,kind,matched", [
    ((450, 600), "AUTO", "REGULAR"), ((600, 450), "REGULAR", "REGULAR"),
    ((5000, 5000), "REGULAR", "REGULAR"), ((1080, 607), "WIDE", "WIDE"),
    ((1080, 608), "AUTO", "WIDE"), ((5000, 2812), "WIDE", "WIDE"),
    ((320, 50), "AUTO", "FIXED_IMAGE"), ((600, 500), "AUTO", "REGULAR"),
    ((600, 500), "FIXED_IMAGE", "FIXED_IMAGE"),
])
def test_geometry_boundaries(size, kind, matched):
    assert image_validation.geometry(*size, kind, 1024) == matched


@pytest.mark.parametrize("size,kind,size_bytes", [
    ((449, 450), "REGULAR", 100), ((5001, 5000), "AUTO", 100),
    ((450, 601), "REGULAR", 100), ((601, 450), "REGULAR", 100),
    ((1079, 607), "WIDE", 100), ((1920, 1079), "WIDE", 100),
    ((5000, 2813), "WIDE", 100), ((450, 450), "FIXED_IMAGE", 100),
    ((320, 50), "AUTO", 512 * 1024 + 1), ((450, 450), "WIDE", 100),
])
def test_geometry_rejects_wrong_dimensions_and_types(size, kind, size_bytes):
    with pytest.raises(ValueError):
        image_validation.geometry(*size, kind, size_bytes)


@pytest.mark.parametrize("data", [b"\x89PNG\r\n\x1a\n", b"GIF89a", b"\xff\xd8\xff",
                                  picture()[:-30], picture(fmt="BMP"), picture((1, 1))],
                         ids=["png-header", "gif-header", "jpeg-header",
                              "truncated", "wrong-format", "tiny"])
def test_corrupt_or_invalid_files_fail_before_preview(data):
    with pytest.raises(ValueError):
        assets.normalize(image_bundle(data), "client")


def test_animated_gif_decodes_every_frame_and_bounds_work(monkeypatch):
    stream = BytesIO()
    Image.new("RGB", (450, 450), "white").save(
        stream, format="GIF", save_all=True,
        append_images=[Image.new("RGB", (450, 450), "black")])
    assert image_validation.inspect(stream.getvalue(), "AUTO")["frames"] == 2
    monkeypatch.setattr(image_validation, "MAX_FRAMES", 1)
    with pytest.raises(ValueError, match="кадров"):
        image_validation.inspect(stream.getvalue(), "AUTO")
    monkeypatch.setattr(image_validation, "MAX_FRAMES", 200)
    monkeypatch.setattr(image_validation, "MAX_DECODED_PIXELS", 202500)
    with pytest.raises(ValueError, match="пикселей"):
        image_validation.inspect(stream.getvalue(), "AUTO")


@pytest.mark.asyncio
async def test_bad_last_image_prevents_all_asset_writes():
    plan = assets.normalize(image_bundle(picture()), "client")
    plan["sitelink_sets"] = [{"Sitelinks": [{"Title": "example"}]}]
    plan["images"] *= 100
    plan["images"].append({"Name": "bad", "ImageData": "R0lGODlh"})
    api = SimpleNamespace(call_v501=AsyncMock())
    with pytest.raises(ValueError):
        await assets.apply(api, plan)
    api.call_v501.assert_not_called()


def test_setup_and_launch_are_independent():
    result = {"status": "complete", "api_creation_complete": True,
              "readback": {"verified": True},
              "required_manual_actions": [{"rule": "launch.moderation", "required": True}]}
    workflow.update(result)
    assert result["setup_complete"]
    assert result["workflow"]["launch"] == "requires_explicit_instruction"
    assert result["workflow"]["ui_verification"] == "not_required"


@pytest.mark.parametrize("readback,ui,complete,expected", [
    (None, False, True, "pending_verification"),
    ({"verified": False}, False, True, "incomplete"),
    ({"verified": True}, True, True, "pending_verification"),
    ({"verified": True}, False, False, "incomplete"),
])
def test_setup_requires_creation_api_and_ui(readback, ui, complete, expected):
    result = {"api_creation_complete": complete,
              "required_manual_actions": [{"rule": "ads.action_button", "required": True,
                                            "status": "verified"}] if ui else []}
    assert workflow.states(result, readback=readback)["setup"] == expected


@pytest.mark.asyncio
async def test_executor_does_not_tell_search_only_to_fix_carousels():
    plan = compiled()
    # Fixture contains image-bearing ads; remove just UI-only requirements for this test.
    for campaign in plan["campaigns"]:
        campaign["channel"] = "search"
        for group in campaign["groups"]:
            for ad in group["ads"]:
                ad["ResponsiveAd"].pop("AdImageHashes", None)
                ad["ResponsiveAd"].pop("VideoExtensionIds", None)
            group["action_buttons"] = [None] * len(group["ads"])
    result = await executor.apply(FakeApi(), plan)
    assert result["workflow"]["ui_verification"] == "not_required"
    assert "карусели" not in result["message"]
    assert not result["setup_complete"]  # API readback is still pending.


def saved_job(tmp_path, *, kind="apply", status="done", uncertain=False, legacy=False):
    plan = {"plan_hash": "a" * 64, "client_login": "client", "campaigns": [],
            "reference_id": BIG}
    job_id = "b" * 32
    payload = {"job_id": job_id, "plan_hash": plan["plan_hash"], "kind": kind,
               "client_login": "client", "status": status, "uncertain": uncertain,
               "events": [], "result": {"status": "complete_unverified", "executed": True,
                   "api_creation_complete": True, "campaigns": [], "reference_id": BIG,
                   "preflight": {"before": {"ads": [{"id": BIG}]}}}}
    path = tmp_path / "jobs" / (job_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    journal = jobs.Journal(path, payload)
    if not legacy:
        journal.verification_plan(plan)
    else:
        journal.save()
    return job_id, path, plan


@pytest.mark.asyncio
async def test_rechecks_keep_original_history_and_latest_failure(tmp_path, monkeypatch):
    job_id, path, plan = saved_job(tmp_path)
    initial = path.read_bytes()
    async def read(api, actual_plan, execution):
        assert actual_plan["reference_id"] == execution["reference_id"] == BIG
        return {"verified": True, "objects": {"ads": [BIG]}}
    monkeypatch.setattr(executor, "readback", read)
    first = await verification.run(object(), tmp_path, "client", job_id)
    assert first["setup_complete"] and first["writes_performed"] is False
    assert "objects" not in first["readback"]
    monkeypatch.setattr(executor, "readback", AsyncMock(side_effect=RuntimeError("incomplete")))
    second = await verification.run(object(), tmp_path, "client", job_id)
    assert second["status"] == "unverified" and not second["setup_complete"]
    assert first["verification_id"] != second["verification_id"]
    assert path.read_bytes() == initial
    assert len(list((path.parent / "verifications" / job_id).glob("*.json"))) == 2
    assert verification.latest(tmp_path, jobs.read(tmp_path, job_id, "client")) == second
    assert not list(path.parent.glob("*.lock"))


@pytest.mark.parametrize("status,uncertain,kind", [
    ("running", False, "apply"), ("done", True, "apply"),
    ("needs_reconciliation", True, "apply"), ("done", False, "preview"),
    ("done", False, "assets"),
])
@pytest.mark.asyncio
async def test_non_final_or_wrong_job_cannot_be_verified(tmp_path, status, uncertain, kind):
    job_id, _, _ = saved_job(tmp_path, status=status, uncertain=uncertain, kind=kind)
    with pytest.raises(ValueError):
        await verification.run(object(), tmp_path, "client", job_id)


@pytest.mark.asyncio
async def test_recheck_rejects_wrong_login_hash_and_tampered_plan(tmp_path):
    job_id, path, plan = saved_job(tmp_path)
    with pytest.raises(PermissionError):
        await verification.run(object(), tmp_path, "other", job_id)
    with pytest.raises(ValueError, match="отличается"):
        await verification.run(object(), tmp_path, "client", job_id,
                               legacy_plan={**plan, "plan_hash": "other"})
    payload = json.loads(path.read_text())
    payload["verification_plan_json"] += " "
    jobs._atomic(path, payload)
    with pytest.raises(ValueError, match="целостность"):
        await verification.run(object(), tmp_path, "client", job_id)


@pytest.mark.asyncio
async def test_legacy_plan_must_match_original_hash(tmp_path, monkeypatch):
    job_id, _, plan = saved_job(tmp_path, legacy=True)
    with pytest.raises(ValueError, match="исходный bundle"):
        await verification.run(object(), tmp_path, "client", job_id)
    with pytest.raises(ValueError, match="хешу"):
        await verification.run(object(), tmp_path, "client", job_id,
                               legacy_plan={**plan, "plan_hash": "other"})
    monkeypatch.setattr(executor, "readback", AsyncMock(return_value={"verified": True}))
    assert (await verification.run(object(), tmp_path, "client", job_id,
                                   legacy_plan=plan))["status"] == "verified"


@pytest.mark.asyncio
async def test_repair_recheck_retains_before_snapshot_and_scope(tmp_path, monkeypatch):
    job_id, _, _ = saved_job(tmp_path, kind="repair")
    async def read(api, plan, *, before):
        assert before["ads"][0]["id"] == BIG
        return {"verified": True}
    monkeypatch.setattr(verification.repair, "readback", read)
    result = await verification.run(object(), tmp_path, "client", job_id)
    assert result["workflow"]["scope"] == "repair"
    assert result["workflow"]["launch"] == "not_in_scope"


@pytest.mark.asyncio
async def test_recheck_lock_blocks_writes_and_other_rechecks(tmp_path, monkeypatch):
    job_id, path, _ = saved_job(tmp_path)
    async def read(api, plan, execution):
        with pytest.raises(ValueError, match="job_id"):
            jobs.start(tmp_path, "client", "other", "repair", AsyncMock())
        with pytest.raises(ValueError, match="выполняется"):
            await verification.run(object(), tmp_path, "client", job_id)
        return {"verified": True}
    monkeypatch.setattr(executor, "readback", read)
    assert (await verification.run(object(), tmp_path, "client", job_id))["status"] == "verified"
    assert not list(path.parent.glob("*.lock"))


@pytest.mark.asyncio
async def test_recheck_api_guard_prohibits_write_methods():
    raw = SimpleNamespace(call=AsyncMock(), call_v501=AsyncMock())
    api = verification.ReadOnlyAPI(raw)
    for method in ["add", "update", "resume", "moderate", "delete"]:
        for call in (api.call, api.call_v501):
            with pytest.raises(PermissionError):
                await call("ads", method, {})
    raw.call.assert_not_called()
    raw.call_v501.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_releases_only_verification_lock(tmp_path, monkeypatch):
    job_id, path, _ = saved_job(tmp_path)
    monkeypatch.setattr(executor, "readback", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await verification.run(object(), tmp_path, "client", job_id)
    assert not list(path.parent.glob("*.lock"))
    assert not list((path.parent / "verifications").glob("**/*.json"))


def test_mcp_job_exposes_latest_check_without_rewriting_original(tmp_path):
    job_id, path, _ = saved_job(tmp_path)
    initial = path.read_bytes()
    result = _in_server(f"""import asyncio, json
from unittest.mock import AsyncMock
import yadirect_mcp.server as s
s._client = object()
s.executor.readback = AsyncMock(return_value={{'verified': True}})
async def run():
    checked = await s.direct_verify_job('client', '{job_id}')
    polled = await s.direct_write_job('client', '{job_id}')
    tools = await s.mcp.list_tools()
    tool = next(t for t in tools if t.name == 'direct_verify_job')
    return {{'checked': checked.structuredContent, 'polled': polled.structuredContent,
             'read_only': tool.annotations.readOnlyHint,
             'destructive': tool.annotations.destructiveHint}}
print(json.dumps(asyncio.run(run())))""", str(tmp_path), YD_MODE="campaign_setup")
    assert result["polled"]["status"] == "complete_unverified"
    assert result["polled"]["current_verification"]["status"] == "verified"
    assert result["checked"]["job_id"] == job_id
    assert not result["read_only"] and not result["destructive"]  # local evidence is persisted
    assert path.read_bytes() == initial


@pytest.mark.asyncio
async def test_real_repair_readback_through_persisted_job(tmp_path, monkeypatch):
    from test_repair_preflight import Api
    from yadirect_mcp import repair

    async def offline(pages):
        for page in pages:
            page.setdefault("site_check", {"ok": True, "checked": True})
        return pages
    monkeypatch.setattr(repair.link_checks, "check_pages", offline)
    api = Api()
    plan = repair.normalize({"ads": [{"id": "20", "href": "https://example.test/new"}]},
                            "client")
    async def operation(journal):
        journal.verification_plan(plan)
        return await repair.apply(api, plan)
    job_id = jobs.start(tmp_path, "client", plan["plan_hash"], "repair", operation)
    await jobs.wait_briefly(job_id, seconds=2)
    api.calls.clear()
    result = await verification.run(api, tmp_path, "client", job_id)
    assert result["status"] == "verified"
    assert not api.calls  # This fake records every write.
    api.current["ResponsiveAd"]["DisplayUrlPath"] = "external-change"
    result = await verification.run(api, tmp_path, "client", job_id)
    assert result["status"] == "unverified"
    assert result["readback"]["mismatches"][0]["reason"] == "display_url_path"


@pytest.mark.asyncio
async def test_stale_verification_cannot_hide_changed_journal(tmp_path, monkeypatch):
    job_id, path, _ = saved_job(tmp_path)
    monkeypatch.setattr(executor, "readback", AsyncMock(return_value={"verified": True}))
    await verification.run(object(), tmp_path, "client", job_id)
    payload = json.loads(path.read_text())
    payload["result"]["status"] = "partial"
    jobs._atomic(path, payload)
    with pytest.raises(ValueError, match="исходному журналу"):
        verification.latest(tmp_path, payload)


@pytest.mark.asyncio
async def test_changed_journal_during_recheck_is_not_accepted(tmp_path, monkeypatch):
    job_id, path, _ = saved_job(tmp_path)
    async def read(api, plan, execution):
        payload = json.loads(path.read_text())
        payload["uncertain"] = True
        jobs._atomic(path, payload)
        return {"verified": True}
    monkeypatch.setattr(executor, "readback", read)
    with pytest.raises(ValueError, match="изменился"):
        await verification.run(object(), tmp_path, "client", job_id)
    assert not list((path.parent / "verifications").glob("**/*.json"))


@pytest.mark.asyncio
async def test_repeat_api_success_cannot_complete_ui_or_partial_creation(tmp_path, monkeypatch):
    plan = compiled()
    execution = await executor.apply(FakeApi(), plan)
    job_id, path, _ = saved_job(tmp_path)
    payload = jobs.read(tmp_path, job_id, "client")
    payload["plan_hash"] = plan["plan_hash"]
    payload["result"] = execution
    journal = jobs.Journal(path, payload)
    journal.verification_plan(plan)
    monkeypatch.setattr(executor, "readback", AsyncMock(return_value={"verified": True}))
    result = await verification.run(object(), tmp_path, "client", job_id)
    assert result["status"] == "verified" and not result["setup_complete"]
    assert result["workflow"]["ui_verification"] == "pending"
    execution.update(api_creation_complete=False, status="partial")
    journal.checkpoint(execution)
    result = await verification.run(object(), tmp_path, "client", job_id)
    assert result["workflow"]["setup"] == "incomplete"


def test_campaign_server_records_context_before_write_and_finishes_workflow(tmp_path):
    result = _in_server("""import asyncio, json
from unittest.mock import AsyncMock
from test_setup_stage2 import saved_job
import yadirect_mcp.server as s
job_id, path, plan = saved_job(s.SETTINGS.out_dir)
journal = s.jobs.Journal(path, s.jobs.read(s.SETTINGS.out_dir, job_id, 'client'))
async def apply(api, plan):
    assert json.loads(path.read_text())['verification_plan_json']
    return {'status': 'complete', 'executed': True, 'api_creation_complete': True,
            'client_login': 'client', 'plan_hash': plan['plan_hash'],
            'campaigns': [{'id': 123}], 'required_manual_actions': [
                {'rule': 'launch.moderation', 'required': True}]}
s.executor.apply = apply
s.executor.readback = AsyncMock(return_value={'verified': True})
s.creation_report.build_model = lambda *args: {}
s.creation_report.persist = lambda *args: path.parent / 'fake.html'
print(json.dumps(asyncio.run(s._execute_campaign(object(), plan, {}, journal))))
""", str(tmp_path), YD_MODE="campaign_setup", YD_CREATE_REPORT_SSH_HOST="")
    assert result["setup_complete"]
    assert result["workflow"]["setup"] == "complete"
    assert result["workflow"]["launch"] == "requires_explicit_instruction"


@pytest.mark.asyncio
async def test_verifier_never_removes_replaced_write_lock(tmp_path, monkeypatch):
    job_id, path, _ = saved_job(tmp_path)
    lock = path.parent / ("login-" + hashlib.sha256(b"client").hexdigest() + ".lock")
    async def read(api, plan, execution):
        lock.write_text("another-operation", encoding="utf-8")
        return {"verified": True}
    monkeypatch.setattr(executor, "readback", read)
    with pytest.raises(ValueError, match="Блокировка"):
        await verification.run(object(), tmp_path, "client", job_id)
    assert lock.read_text() == "another-operation"
