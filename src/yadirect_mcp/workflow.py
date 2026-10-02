"""Independent setup and launch states. API hashes never prove saved UI."""
from __future__ import annotations

import hashlib
import json


def action_id(job_id: str, action: dict) -> str:
    identity = json.dumps({key: action[key] for key in (
        "rule", "ad_id", "group_id", "campaign_id", "source_job_id") if key in action},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((job_id + ":" + identity).encode()).hexdigest()[:32]


def states(result: dict, *, readback: dict | None = None, scope: str = "campaign",
           job_id: str | None = None, ui_review: dict | None = None) -> dict:
    complete = result.get("api_creation_complete")
    if complete is None:
        complete = result.get("status") in {"complete", "complete_unverified"}
    check = readback if readback is not None else result.get("readback")
    api_state = "pending" if check is None else (
        "verified" if check.get("verified") is True else "failed")
    ui_actions = [item for item in result.get("required_manual_actions", [])
                  if item.get("required") and not item.get("rule", "").startswith("launch.")]
    # Only the separately validated receipt registry may close an action.
    # Status labels in an advertising result or a caller's screenshot are not API proof.
    verified = {item["action_id"] for item in (ui_review or {}).get("actions", [])
                if item.get("status") == "verified_by_reviewer"} if (
                    job_id and (ui_review or {}).get("complete") is True) else set()
    unresolved = [item for item in ui_actions if action_id(job_id or "", item) not in verified]
    ui_state = ("not_required" if not ui_actions else
                "pending" if unresolved else "verified_by_reviewer")
    setup = "complete" if complete and api_state == "verified" and not unresolved else (
        "incomplete" if not complete or api_state == "failed" else "pending_verification")
    return {"scope": scope, "api_creation": "complete" if complete else "incomplete",
            "api_verification": api_state, "ui_verification": ui_state,
            "setup": setup,
            "launch": "requires_explicit_instruction" if scope == "campaign" else "not_in_scope",
            "publication": (result.get("creation_report") or {}).get("status", "not_requested")}


def update(result: dict) -> None:
    result["workflow"] = states(result)
    result["setup_complete"] = result["workflow"]["setup"] == "complete"
