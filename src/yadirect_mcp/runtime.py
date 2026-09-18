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
            "dependencies": {name: version(name) for name in ("mcp", "httpx", "Pillow")},
            "out_dir": str(settings.out_dir), "allowlist_enabled": bool(settings.allowed_logins),
            "publication_configured": bool(settings.create_report_ssh_host),
            "loaded_source_sha256": LOADED_SOURCE_SHA256, "disk_source_sha256": disk,
            "restart_required": disk != LOADED_SOURCE_SHA256,
            "protocol_revision": "2026-09-17-ops-p3",
            "features": ["image_decode_preflight", "independent_workflow", "job_reverification",
                         "pending_actions", "publication_recovery", "runtime_diagnostics"]}
