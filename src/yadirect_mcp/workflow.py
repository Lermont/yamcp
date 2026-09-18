"""Independent setup and launch states. API hashes never prove saved UI."""
from __future__ import annotations


def states(result: dict, *, readback: dict | None = None, scope: str = "campaign") -> dict:
    complete = result.get("api_creation_complete")
    if complete is None:
        complete = result.get("status") in {"complete", "complete_unverified"}
    check = readback if readback is not None else result.get("readback")
    api_state = "pending" if check is None else (
        "verified" if check.get("verified") is True else "failed")
    ui_actions = [item for item in result.get("required_manual_actions", [])
                  if item.get("required") and not item.get("rule", "").startswith("launch.")]
    # A write result (including old UI status labels) is not UI evidence.
    ui_state = "pending" if ui_actions else "not_required"
    setup = "complete" if complete and api_state == "verified" and not ui_actions else (
        "incomplete" if not complete or api_state == "failed" else "pending_verification")
    return {"scope": scope, "api_creation": "complete" if complete else "incomplete",
            "api_verification": api_state, "ui_verification": ui_state,
            "setup": setup,
            "launch": "requires_explicit_instruction" if scope == "campaign" else "not_in_scope",
            "publication": (result.get("creation_report") or {}).get("status", "not_requested")}


def update(result: dict) -> None:
    result["workflow"] = states(result)
    result["setup_complete"] = result["workflow"]["setup"] == "complete"
