"""Read-only Direct rechecks, stored beside an immutable original write job."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from . import creative, executor, jobs, repair, workflow


class ReadOnlyAPI:
    def __init__(self, api):
        self.api = api

    async def call(self, service, method, params=None, **kwargs):
        if method != "get":
            raise PermissionError("Повторная проверка допускает только get")
        return await self.api.call(service, method, params, **kwargs)

    async def call_v501(self, service, method, params=None, **kwargs):
        if method != "get":
            raise PermissionError("Повторная проверка допускает только get")
        return await self.api.call_v501(service, method, params, **kwargs)


def _native_ids(value, key=""):
    is_id = key.lower() in {"id", "ids"} or bool(
        re.search(r"(?:[a-z](?:Ids?|IDs?)$|_ids?$|_id_list$)", key))
    if isinstance(value, dict):
        return {k: _native_ids(v, key if is_id and k == "Items" else k)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_native_ids(v, key) for v in value]
    if is_id and isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest(out_dir: Path, job: dict) -> dict | None:
    directory = out_dir / "jobs" / "verifications" / job["job_id"]
    paths = sorted(directory.glob("*.json"), reverse=True)
    if not paths:
        return None
    record = json.loads(paths[0].read_text(encoding="utf-8"))
    if (record.get("job_id") != job["job_id"]
            or record.get("plan_hash") != job["plan_hash"]
            or record.get("client_login", "").casefold() != job["client_login"].casefold()
            or record.get("source_journal_sha256") != _digest(
                out_dir / "jobs" / (job["job_id"] + ".json"))):
        raise ValueError("Повторная проверка не соответствует исходному журналу")
    return record


async def run(api, out_dir: Path, client_login: str, job_id: str,
              *, legacy_plan: dict | None = None) -> dict:
    job = jobs.read(out_dir, job_id, client_login)
    if job["status"] != "done" or job.get("uncertain"):
        raise ValueError("Нужен завершённый job без неопределённого результата записи")
    if job["kind"] not in {"apply", "repair"}:
        raise ValueError("Повторная проверка поддерживает apply и repair")
    original = job.get("result") or {}
    if not original.get("executed"):
        raise ValueError("Job не выполнял запись")
    encoded = job.get("verification_plan_json")
    if encoded:
        if hashlib.sha256(encoded.encode()).hexdigest() != job.get("verification_plan_sha256"):
            raise ValueError("Нарушена целостность сохранённого плана")
        plan = json.loads(encoded)
        if legacy_plan is not None and legacy_plan != plan:
            raise ValueError("Переданный план отличается от сохранённого")
    elif legacy_plan is not None:
        plan = legacy_plan
    else:
        raise ValueError("Старый job не содержит плана: передайте исходный bundle")
    if (plan.get("plan_hash") != job["plan_hash"]
            or plan.get("client_login", "").casefold() != client_login.casefold()):
        raise ValueError("План не соответствует хешу или логину исходного job")
    source = out_dir / "jobs" / (job_id + ".json")
    source_hash = _digest(source)
    lock = out_dir / "jobs" / (
        "login-" + hashlib.sha256(client_login.casefold().encode()).hexdigest() + ".lock")
    lock_owner = "verification:" + job_id + ":" + uuid.uuid4().hex
    try:
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(lock_owner)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ValueError("Для логина выполняется запись/проверка или нужна сверка") from exc
    try:
        if _digest(source) != source_hash:
            raise ValueError("Исходный job изменился перед проверкой")
        execution = _native_ids(original)
        guarded = ReadOnlyAPI(api)
        try:
            if job["kind"] == "apply":
                check = await executor.readback(guarded, plan, execution)
                execution["required_manual_actions"] = creative.executed_actions(
                    plan, execution.get("campaigns", []))
            else:
                before = (execution.get("preflight") or {}).get("before")
                if before is None:
                    raise ValueError(
                        "В старом repair job нет снимка before; полная проверка невозможна")
                check = await repair.readback(guarded, plan, before=before)
        except Exception as exc:  # noqa: BLE001 - failed rechecks also supersede older success
            check = {"verified": False, "error": str(exc)}
        check.pop("objects", None)
        if not lock.exists() or lock.read_text(encoding="utf-8") != lock_owner:
            raise ValueError("Блокировка логина изменилась во время проверки")
        if _digest(source) != source_hash:
            raise ValueError("Исходный job изменился во время проверки")
        state = workflow.states(execution, readback=check,
                                scope="campaign" if job["kind"] == "apply" else "repair")
        record = {"schema": "direct_job_verification_v1", "verification_id": uuid.uuid4().hex,
                  "job_id": job_id, "client_login": client_login, "plan_hash": job["plan_hash"],
                  "checked_at": time.time(), "source_journal_sha256": source_hash,
                  "original_status": original.get("status"), "readback": check,
                  "workflow": state, "setup_complete": state["setup"] == "complete",
                  "status": "verified" if check.get("verified") is True else "unverified",
                  "writes_performed": False}
        directory = out_dir / "jobs" / "verifications" / job_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (str(time.time_ns()) + "-" + record["verification_id"] + ".json")
        record["artifact_path"] = str(path)
        jobs._atomic(path, record)
        return record
    finally:
        if lock.exists() and lock.read_text(encoding="utf-8") == lock_owner:
            lock.unlink(missing_ok=True)
