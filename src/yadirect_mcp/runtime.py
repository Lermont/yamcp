"""Non-secret process identity and source drift diagnostics."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
from importlib.metadata import version
from pathlib import Path

from . import jobs

STARTED_AT = time.time()
ROOT = Path(__file__).resolve().parent


def fingerprint() -> str:
    files = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in ROOT.rglob("*") if p.is_file()
             and p.suffix in {".py", ".md", ".json", ".html", ".css", ".js"}
             and "__pycache__" not in p.parts}
    templates = ROOT.parent.parent / "templates" / "client-report"
    if templates.is_dir():
        files.update({"templates/" + str(p.relative_to(templates)):
                      hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in templates.rglob("*") if p.is_file()
                      and p.suffix in {".py", ".html", ".css", ".js"}
                      and "__pycache__" not in p.parts and "dist" not in p.parts})
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


LOADED_SOURCE_SHA256 = fingerprint()


def describe(settings) -> dict:
    disk = fingerprint()
    return {"package_version": version("yadirect-mcp"), "process_id": jobs.PROCESS_ID,
            "pid": os.getpid(), "started_at": STARTED_AT, "python": platform.python_version(),
            "executable": sys.executable, "package_path": str(ROOT),
            "mode": settings.mode, "sandbox": settings.sandbox,
            "approval_mode": settings.approval_mode,
            "dependencies": {name: version(name) for name in ("mcp", "httpx", "Pillow")},
            "out_dir": str(settings.out_dir), "allowlist_enabled": bool(settings.allowed_logins),
            "publication_configured": bool(settings.create_report_ssh_host),
            "loaded_source_sha256": LOADED_SOURCE_SHA256, "disk_source_sha256": disk,
            "restart_required": disk != LOADED_SOURCE_SHA256,
            "protocol_revision": "2026-09-25-network-neuro-schedule-v4",
            "features": ["conversion_start_v3", "two_week_gross_budget",
                         "configured_task_authorization", "image_decode_preflight",
                         "independent_workflow", "job_reverification",
                         "pending_actions", "publication_recovery", "runtime_diagnostics",
                         "campaign_rename", "existing_search_groups_append",
                         "existing_network_groups_append", "active_campaign_draft_append",
                         "creative_defaults_v4",
                         "neuro_ads_ui_handoff", "campaign_alternative_texts_repair",
                         "native_always_on_schedule", "campaign_schedule_reset",
                         "landing_html_8mb", "repair_background_preflight",
                         "landing_fragment_dedup", "traffic_reports_campaign_identity",
                         "reports_moscow_statistics_timezone", "client_report_agency_voice",
                         "retired_geotargeting_guard", "manual_ui_review_receipts",
                         "weekly_hourly_bid_grid", "audience_interest_catalog",
                         "network_conversion_repair", "forecast_quality_requirements",
                         "draft_criteria_removal", "explicit_ad_resume",
                         "business_phone_check"]}
