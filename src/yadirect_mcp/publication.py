"""Recover HTML publication without repeating any advertising writes."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

from . import creation_report, jobs


def snapshot(path: Path, out_dir: Path, job_id: str) -> dict:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    target = out_dir / "jobs" / "reports" / job_id / (sha + ".html")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError("Снимок HTML изменён")
    else:
        with target.open("xb") as stream:
            stream.write(data)
    return {"artifact_path": str(target), "sha256": sha}


def prepare(settings, client_login: str, source_job_id: str, *, attempt: str | None = None) -> dict:
    job = jobs.read(settings.out_dir, source_job_id, client_login)
    result = job.get("result") or {}
    if (job.get("kind") != "apply" or job.get("status") != "done" or job.get("uncertain")
            or not result.get("executed")
            or not any(row.get("id") for row in result.get("campaigns", []))):
        raise ValueError("Нужен завершённый apply job с созданными кампаниями без uncertain")
    report = result.get("creation_report") or {}
    path = Path(report.get("artifact_path", "")).resolve()
    if (not path.is_relative_to(settings.out_dir.resolve()) or not path.is_file()
            or path.suffix.lower() != ".html"):
        raise ValueError("В исходном job нет доступного HTML внутри YD_OUT_DIR")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if report.get("sha256") and report["sha256"] != sha:
        raise ValueError("HTML изменился относительно исходного job")
    if not settings.create_report_ssh_host:
        raise ValueError("Публикация не настроена")
    job_path = settings.out_dir / "jobs" / (source_job_id + ".json")
    plan = {"schema": "direct_publication_recovery_v1", "client_login": client_login,
            "source_job_id": source_job_id, "source_plan_hash": job["plan_hash"],
            "source_journal_sha256": hashlib.sha256(job_path.read_bytes()).hexdigest(),
            "artifact_path": str(path), "html_sha256": sha,
            "ssh_host": settings.create_report_ssh_host,
            "remote_root": settings.create_report_remote_root,
            "public_base_url": settings.create_report_public_base_url,
            "public_url": creation_report.public_url(client_login,
                                                       settings.create_report_public_base_url),
            "attempt": attempt or uuid.uuid4().hex,
            "summary": {"operation": "publish_existing_html", "advertising_writes": 0}}
    plan["plan_hash"] = hashlib.sha256(json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return plan


async def apply(settings, plan: dict, journal) -> dict:
    journal.payload.update(source_job_id=plan["source_job_id"],
                           source_journal_sha256=plan["source_journal_sha256"])
    journal.save()
    # Recheck under jobs.start's cross-process advertiser lock.
    current = prepare(settings, plan["client_login"], plan["source_job_id"],
                      attempt=plan["attempt"])
    if current != plan:
        raise ValueError("План публикации изменился; требуется новый preview")
    bound = snapshot(Path(plan["artifact_path"]), settings.out_dir, journal.payload["job_id"])
    if bound["sha256"] != plan["html_sha256"]:
        raise ValueError("HTML изменился перед публикацией")
    result = {"client_login": plan["client_login"], "plan_hash": plan["plan_hash"],
              "source_job_id": plan["source_job_id"], "advertising_writes": False,
              "artifact_path": bound["artifact_path"], "executed": True}
    journal.event(stage="request", service="publication", html_sha256=plan["html_sha256"],
                  public_url=plan["public_url"])
    try:
        published = await asyncio.to_thread(
            creation_report.publish, Path(bound["artifact_path"]),
            client_login=plan["client_login"], ssh_host=plan["ssh_host"],
            remote_root=plan["remote_root"], public_base_url=plan["public_base_url"])
        if published.get("verified") is not True or published.get("sha256") != plan["html_sha256"]:
            raise ValueError("Публичный readback не подтвердил согласованный HTML")
        result.update(status="published", publication=published)
    except Exception as exc:  # noqa: BLE001 - publishing may have succeeded before readback failed
        result.update(status="publish_failed", error=str(exc),
                      publication={"status": "publish_failed", "verified": False},
                      retry_requires_new_preview=True)
    journal.event(stage="response", service="publication", result=result)
    return result


def latest(out_dir: Path, source: dict) -> dict | None:
    matches = []
    for path in (out_dir / "jobs").glob("*.json"):
        if len(path.stem) != 32:
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # The registry reports unreadable records separately.
        if (row.get("kind") == "publish" and row.get("source_job_id") == source["job_id"]
                and row.get("client_login", "").casefold() == source["client_login"].casefold()):
            matches.append(row)
    if not matches:
        return None
    row = max(matches, key=lambda item: (item.get("created_at", 0), item["job_id"]))
    source_path = out_dir / "jobs" / (source["job_id"] + ".json")
    if row.get("source_journal_sha256") != hashlib.sha256(source_path.read_bytes()).hexdigest():
        raise ValueError("Публикация не соответствует исходному журналу")
    return {"job_id": row["job_id"], "job_status": row["status"],
            "uncertain": row.get("uncertain", False), "checked_at": row.get("updated_at"),
            "result": row.get("result")}
