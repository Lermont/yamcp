"""Read-only pending-action registry derived from durable job evidence."""
from __future__ import annotations

import hashlib
import json
import time

from . import jobs, manual_review, publication, workflow


def pending(settings, client_login: str, *, offset: int = 0, limit: int = 100) -> dict:
    settings.check_login(client_login)
    if isinstance(offset, bool) or isinstance(limit, bool) or offset < 0 or not 1 <= limit <= 500:
        raise ValueError("offset >= 0; limit от 1 до 500")
    rows, unreadable, review_errors = [], 0, 0
    paths = sorted((settings.out_dir / "jobs").glob("*.json"))
    applied = set()
    for path in paths:
        if len(path.stem) != 32:
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            if item.get("kind") in {"apply", "repair", "assets", "feeds"} and (
                (item.get("result") or {}).get("executed") or item.get("status") == "running"
            ):
                applied.add((item["client_login"].casefold(), item["plan_hash"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Count malformed journals in the main pass.
    for path in paths:
        if len(path.stem) != 32:
            continue
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["client_login"].casefold() != client_login.casefold():
                continue
            if job["job_id"] != path.stem:
                raise ValueError("job_id не соответствует файлу")
            result = job.get("result") or {}
            actions = []
            def add(rule, detail=None, *, owner=job["job_id"], target=actions):
                action = {"rule": rule, **(detail or {})}
                action["action_id"] = workflow.action_id(owner, action)
                target.append(action)
            if job.get("uncertain") or job["status"] in {"interrupted", "needs_reconciliation"}:
                add("write.reconciliation", {"status": "attention_required"})
            elif job["status"] == "running":
                add("write.running", {"status": "running" if job.get("owner_process") ==
                    jobs.PROCESS_ID else "owner_changed"})
            elif job["status"] == "failed" or result.get("status") in {"partial", "blocked"}:
                add("write.incomplete", {"status": result.get("status", job["status"])})
            if job["kind"] == "preview" and result.get("status") == "preview" and (
                client_login.casefold(), job["plan_hash"]
            ) not in applied:
                add("preview.review", {"status": "requires_review", "tool": "direct_write_job"})
            if job["kind"] == "feeds" and (result.get("status") == "readback_failed"
                                          or not result.get("ready_for_campaign")):
                add("verification.feed", {"status": "check_source",
                                          "tool": "direct_product_source"})
            if job["kind"] == "assets" and result.get("status") == "readback_failed":
                add("verification.assets", {"status": "failed"})
            if job["kind"] in {"apply", "repair"} and result.get("executed"):
                current = manual_review.current(settings.out_dir, job)
                state = current["workflow"]
                reviewed = {a["action_id"]: a for a in current["manual_review"]["actions"]}
                if not current["manual_review"]["complete"]:
                    add("verification.ui_evidence", {"status": "attention_required",
                                                     "error": current["manual_review"]["error"]})
                    review_errors += 1
                if state["api_verification"] != "verified":
                    add("verification.api", {"status": state["api_verification"],
                                             "tool": "direct_verify_job"})
                for action in current["required_manual_actions"]:
                    if action.get("required"):
                        receipt = reviewed.get(workflow.action_id(job["job_id"], action), {})
                        if receipt.get("status") == "verified_by_reviewer":
                            continue
                        add(action["rule"], {"status": "requires_explicit_instruction"
                            if action["rule"].startswith("launch.") else
                            receipt.get("status", "pending_ui"),
                            **{key: action[key] for key in ("ad_id", "group_id", "campaign_id")
                               if key in action}})
                report = result.get("creation_report")
                recovered = publication.latest(settings.out_dir, job)
                if recovered:
                    report = (recovered.get("result") or {}).get("publication") or {
                        "status": recovered["job_status"], "verified": False}
                if report and (report.get("status") != "published"
                               or report.get("verified") is not True):
                    add("publication.report", {"status": report.get("status"),
                                               "tool": "direct_publish_job"})
            if actions:
                rows.append({"job_id": job["job_id"], "kind": job["kind"],
                             "job_status": job["status"], "plan_hash": job["plan_hash"],
                             "actions": list({a["action_id"]: a for a in actions}.values())})
        except (OSError, ValueError, KeyError, TypeError):
            unreadable += 1
    lock = settings.out_dir / "jobs" / (
        "login-" + hashlib.sha256(client_login.casefold().encode()).hexdigest() + ".lock")
    return {"client_login": client_login, "checked_at": time.time(),
            "jobs": rows[offset:offset+limit],
            "total_pending_jobs": len(rows), "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "complete": unreadable == 0 and review_errors == 0,
            "unreadable_records": unreadable, "review_errors": review_errors,
            "login_locked": lock.exists(), "source": "local_job_journals",
            "live_account_checked": False}
