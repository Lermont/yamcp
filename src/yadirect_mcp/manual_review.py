"""Evidence-backed, client-attested UI reviews. Never writes to Direct.

Original advertising journals are immutable. Receipts are separate durable jobs;
API verification and a reviewer's UI observation remain different evidence types.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import jobs, workflow

RULES = {"ads.action_button", "network.carousel", "ads.neuro_ad"}
MAX_AGE = 24 * 3600
API_MAX_AGE = 15 * 60
MAX_FILE = 10 * 1024 * 1024
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".txt", ".json"}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _journal_hash(out_dir: Path, job: dict) -> str:
    return _file_hash(out_dir / "jobs" / (job["job_id"] + ".json"))


def _source(settings, login: str, job_id: str) -> dict:
    settings.check_login(login)
    job = jobs.read(settings.out_dir, job_id, login)
    result = job.get("result") or {}
    if (job.get("job_id") != job_id or job.get("kind") not in {"apply", "repair"}
            or job.get("status") != "done" or job.get("uncertain")
            or not result.get("executed")
            or workflow.states(result)["api_creation"] != "complete"):
        raise ValueError("Нужен полный завершённый apply/repair job без uncertain")
    return job


def _actions(job: dict) -> dict:
    return {workflow.action_id(job["job_id"], item): item
            for item in (job.get("result") or {}).get("required_manual_actions", [])
            if item.get("required") and not item.get("rule", "").startswith("launch.")}


def _effective(job: dict, checked: dict | None) -> dict:
    result = job.get("result") or {}
    actions = {workflow.action_id(job["job_id"], item): item for item in [
        *result.get("required_manual_actions", []),
        *(checked or {}).get("required_manual_actions", [])]}
    return {**job, "result": {**result, "required_manual_actions": list(actions.values())}}


def _ids(value: object, key: str = "") -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_ids(v, k) for k, v in value.items()), set())
    if isinstance(value, list):
        return set().union(*(_ids(v, key) for v in value), set())
    if key.lower() in {"id", "ids", "campaignid", "campaignids", "adgroupid", "adgroupids",
                       "ad_id", "group_id", "campaign_id"}:
        return {str(value)}
    return set()


def _revision(out_dir: Path, job: dict, action: dict) -> tuple[str, float]:
    """Known related writes and failed API rechecks permanently supersede old receipts."""
    targets = {str(action[k]) for k in ("ad_id", "group_id", "campaign_id") if action.get(k)}
    changes = {}
    changed_at = float(job.get("updated_at", job.get("created_at", 0)))
    for path in (out_dir / "jobs").glob("*.json"):
        if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["job_id"] != path.stem:
            raise ValueError("Повреждён реестр jobs")
        if row["client_login"].casefold() != job["client_login"].casefold():
            continue
        if row.get("kind") not in {"apply", "repair", "assets", "feeds"}:
            continue
        requests = [e for e in row.get("events", []) if e.get("stage") == "request"]
        relevant = [e for e in requests if not _ids(e.get("params"))
                    or targets & _ids(e.get("params"))]
        if row["job_id"] != job["job_id"] and relevant:
            changes[path.stem] = _file_hash(path)
            changed_at = max(changed_at, *(float(e.get("at", row.get("created_at", 0)))
                                           for e in relevant))
    failures = {}
    for path in (out_dir / "jobs" / "verifications" / job["job_id"]).glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if (row.get("readback") or {}).get("verified") is not True:
            failures[path.name] = digest(row.get("readback"))
            changed_at = max(changed_at, float(row.get("checked_at", 0)))
    return digest({"writes": changes, "failed_rechecks": failures}), changed_at


def _text(value: object, name: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name}: требуется непустая строка до {limit} знаков")
    return value.strip()


def _review(source: dict, action: dict, changed_at: float) -> dict:
    allowed = {"outcome", "reviewer", "checked_at", "saved_and_reopened", "checked_fields",
               "evidence_paths", "reason"}
    if not isinstance(source, dict) or set(source) - allowed:
        raise ValueError("Неизвестные поля review")
    outcome = source.get("outcome", "verified")
    reviewer = _text(source.get("reviewer"), "reviewer", 200)
    if outcome == "invalidated":
        if set(source) - {"outcome", "reviewer", "reason"}:
            raise ValueError("Для отзыва нужны только outcome, reviewer, reason")
        return {"outcome": outcome, "reviewer": reviewer,
                "reason": _text(source.get("reason"), "reason")}
    if outcome != "verified" or source.get("saved_and_reopened") is not True:
        raise ValueError("Подтвердите сохранение и повторное открытие: saved_and_reopened=true")
    stamp = datetime.fromisoformat(_text(source.get("checked_at"), "checked_at", 60))
    if stamp.tzinfo is None:
        raise ValueError("checked_at требует часовой пояс")
    at = stamp.timestamp()
    if at > time.time() + 60 or time.time() - at > MAX_AGE or at < changed_at:
        raise ValueError("Проверка устарела, предшествует изменениям или датирована будущим")
    fields = source.get("checked_fields")
    if not isinstance(fields, dict):
        raise ValueError("Нужны checked_fields с фактическими значениями")
    requested = action.get("requested") or {}
    rule = action["rule"]
    if rule == "ads.action_button":
        if set(fields) != {"text", "href"}:
            raise ValueError("Кнопка: нужны text и href")
        for key in ("text", "href"):
            _text(fields.get(key), key)
            if not requested.get(key) or fields[key] != requested[key]:
                raise ValueError("Кнопка не совпадает с согласованным текстом/URL")
    elif rule == "network.carousel":
        if (set(fields) != {"slides", "order_checked", "images_loaded"}
                or fields.get("order_checked") is not True
                or fields.get("images_loaded") is not True
                or not isinstance(fields.get("slides"), list)
                or not 2 <= len(fields["slides"]) <= 10):
            raise ValueError("Карусель: slides (2–10), order_checked=true, images_loaded=true")
        for slide in fields["slides"]:
            _text(slide, "slide")
    elif rule == "ads.neuro_ad":
        if (set(fields) != {"landing_url", "generation_status", "texts_checked", "images_checked"}
                or fields.get("landing_url") != requested.get("href")
                or not requested.get("href") or fields.get("generation_status") != "ready"
                or fields.get("texts_checked") is not True
                or fields.get("images_checked") is not True):
            raise ValueError("Нейрообъявление: проверьте landing_url, ready, тексты и изображения")
    paths = source.get("evidence_paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= 5:
        raise ValueError("Нужны 1–5 evidence_paths из YD_OUT_DIR/ui-evidence")
    return {"outcome": outcome, "reviewer": reviewer,
            "checked_at": stamp.astimezone(UTC).isoformat(),
            "saved_and_reopened": True, "checked_fields": fields, "valid_until": at + MAX_AGE}


def _evidence(out_dir: Path, paths: list) -> list[dict]:
    root = (out_dir / "ui-evidence").resolve()
    if not root.is_relative_to(out_dir.resolve()):
        raise ValueError("ui-evidence должен находиться внутри YD_OUT_DIR")
    result = []
    for name in paths:
        path = Path(_text(name, "evidence_path")).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or path.suffix.lower() not in SUFFIXES):
            raise ValueError("Свидетельство должно быть файлом внутри YD_OUT_DIR/ui-evidence")
        size = path.stat().st_size
        if not 0 < size <= MAX_FILE:
            raise ValueError("Размер свидетельства должен быть от 1 байта до 10 MiB")
        result.append({"path": str(path), "sha256": _file_hash(path),
                       "size": size, "suffix": path.suffix.lower()})
    if sum(item["size"] for item in result) > 20 * 1024 * 1024:
        raise ValueError("Общий размер свидетельств больше 20 MiB")
    return result


def prepare(settings, client_login: str, source_job_id: str, action_id: str,
            review: dict, *, attempt: str | None = None) -> dict:
    from . import verification

    job = _source(settings, client_login, source_job_id)
    checked = verification.latest(settings.out_dir, job)
    job = _effective(job, checked)
    action = _actions(job).get(action_id)
    if action is None or action.get("rule") not in RULES:
        raise ValueError("Нужен action_id кнопки, карусели или нейрообъявления из pending actions")
    if not action.get("campaign_id") or not (action.get("ad_id") or action.get("group_id")):
        raise ValueError("Ручное действие не содержит точных ID целевых объектов")
    revision, changed_at = _revision(settings.out_dir, job, action)
    normalized = _review(review, action, changed_at)
    if normalized["outcome"] == "verified" and (
            not checked or (checked.get("readback") or {}).get("verified") is not True
            or time.time() - checked["checked_at"] > API_MAX_AGE
            or checked["checked_at"] < changed_at):
        raise ValueError("Сначала выполните успешный свежий direct_verify_job (до 15 минут)")
    evidence = _evidence(settings.out_dir, review["evidence_paths"]) if (
        normalized["outcome"] == "verified") else []
    plan = {"schema": "direct_manual_review_v1", "client_login": client_login,
            "source_job_id": source_job_id, "source_plan_hash": job["plan_hash"],
            "source_journal_sha256": _journal_hash(settings.out_dir, job),
            "action_id": action_id, "action": action, "revision": revision,
            "review": normalized, "evidence": evidence,
            "api_verification_id": checked.get("verification_id") if checked else None,
            "attempt": attempt or uuid.uuid4().hex,
            "summary": {"operation": "record_ui_review", "rule": action["rule"],
                        "outcome": normalized["outcome"], "advertising_writes": 0}}
    plan["plan_hash"] = digest(plan)
    return plan


async def apply(settings, plan: dict, journal) -> dict:
    return await asyncio.to_thread(_apply, settings, plan, journal)


def _apply(settings, plan: dict, journal) -> dict:
    review = {k: v for k, v in plan["review"].items() if k != "valid_until"}
    if review["outcome"] == "verified":
        review["evidence_paths"] = [item["path"] for item in plan["evidence"]]
    current = prepare(settings, plan["client_login"], plan["source_job_id"],
                      plan["action_id"], review, attempt=plan["attempt"])
    if current != plan:
        raise ValueError("План проверки или свидетельства изменились; нужен новый preview")
    journal.verification_plan(plan)
    journal.payload["source_job_id"] = plan["source_job_id"]
    journal.save()
    evidence = []
    directory = settings.out_dir / "jobs" / "manual_review_evidence"
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.resolve().is_relative_to(settings.out_dir.resolve()):
        raise ValueError("Каталог свидетельств выходит за YD_OUT_DIR")
    for item in plan["evidence"]:
        data = Path(item["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("Свидетельство изменилось после preview")
        target = directory / (item["sha256"] + item["suffix"])
        if target.exists():
            if target.read_bytes() != data:
                raise ValueError("Сохранённое свидетельство повреждено")
        else:
            with target.open("xb") as stream:
                stream.write(data)
        evidence.append({"artifact_path": str(target), "sha256": item["sha256"]})
    return {"status": "recorded", "executed": True, "advertising_writes": False,
            "source_job_id": plan["source_job_id"], "action_id": plan["action_id"],
            "review": plan["review"], "evidence": evidence,
            "verification_method": "reviewer_attested_saved_ui",
            "identity_assurance": "client_attested", "live_account_checked": False}


def assess(out_dir: Path, job: dict) -> dict:
    actions = _actions(job)
    rows = {key: {"action_id": key, "rule": a["rule"], "status": "pending_ui"}
            for key, a in actions.items()}
    try:
        receipts = {}
        for path in (out_dir / "jobs").glob("*.json"):
            if not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                continue
            child = json.loads(path.read_text(encoding="utf-8"))
            if (child.get("kind") != "manual_review" or child.get("status") != "done"
                    or child.get("source_job_id") != job["job_id"]):
                continue
            result = child.get("result") or {}
            if result.get("status") != "recorded":
                continue
            encoded = child["verification_plan_json"]
            if hashlib.sha256(encoded.encode()).hexdigest() != child["verification_plan_sha256"]:
                raise ValueError("Повреждён план ручной проверки")
            plan = json.loads(encoded)
            fingerprint = plan.pop("plan_hash")
            if (digest(plan) != fingerprint or child["plan_hash"] != fingerprint
                    or child["job_id"] != path.stem or child.get("uncertain")
                    or child["client_login"].casefold() != job["client_login"].casefold()
                    or plan["client_login"].casefold() != job["client_login"].casefold()
                    or plan["source_job_id"] != job["job_id"]
                    or result.get("review") != plan["review"]
                    or result.get("action_id") != plan["action_id"]):
                raise ValueError("Ручная проверка не соответствует журналу")
            key = plan["action_id"]
            order = (child["created_at"], child["job_id"])
            if key not in receipts or order > receipts[key][0]:
                receipts[key] = (order, plan, result, child["job_id"])
        for key, (_, plan, result, receipt_id) in receipts.items():
            if key not in actions:
                continue
            row = rows[key]
            row.update(receipt_job_id=receipt_id, reviewer=plan["review"]["reviewer"],
                       verification_method="reviewer_attested_saved_ui")
            if plan["review"]["outcome"] == "invalidated":
                row.update(status="invalidated", reason=plan["review"]["reason"])
                continue
            revision, _ = _revision(out_dir, job, actions[key])
            if (plan["source_journal_sha256"] != _journal_hash(out_dir, job)
                    or plan["source_plan_hash"] != job["plan_hash"]
                    or plan["action"] != actions[key] or plan["revision"] != revision):
                row.update(status="stale", reason="Настройки или результаты проверки изменились")
                continue
            if time.time() >= plan["review"]["valid_until"]:
                row.update(status="expired", reason="Нужна новая UI-проверка через 24 часа")
                continue
            evidence = result.get("evidence", [])
            if len(evidence) != len(plan["evidence"]) or not evidence:
                raise ValueError("Свидетельства отсутствуют")
            for item, expected in zip(evidence, plan["evidence"], strict=True):
                path = Path(item["artifact_path"]).resolve()
                root = (out_dir / "jobs" / "manual_review_evidence").resolve()
                if (not root.is_relative_to(out_dir.resolve()) or not path.is_relative_to(root)
                        or item["sha256"] != expected["sha256"]
                        or _file_hash(path) != expected["sha256"]):
                    raise ValueError("Сохранённое свидетельство повреждено")
            row.update(status="verified_by_reviewer", checked_at=plan["review"]["checked_at"],
                       valid_until=plan["review"]["valid_until"], evidence=evidence)
        return {"complete": True, "actions": list(rows.values()), "live_account_checked": False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"complete": False, "error": str(exc), "live_account_checked": False,
                "actions": [{"action_id": key, "rule": a["rule"], "status": "pending_ui"}
                            for key, a in actions.items()]}


def current(out_dir: Path, job: dict) -> dict:
    from . import verification

    checked = verification.latest(out_dir, job)
    job = _effective(job, checked)
    review = assess(out_dir, job)
    state = workflow.states(job.get("result") or {},
                            readback=checked["readback"] if checked else None,
                            scope="campaign" if job["kind"] == "apply" else "repair",
                            job_id=job["job_id"], ui_review=review)
    if job.get("status") != "done" or job.get("uncertain"):
        state["setup"] = "incomplete"
    return {"manual_review": review, "workflow": state,
            "required_manual_actions": job["result"]["required_manual_actions"],
            "setup_complete": state["setup"] == "complete"}
