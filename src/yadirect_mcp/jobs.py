"""Durable background operations and cross-process advertiser locks.

A write is never replayed automatically. Uncertain responses keep the lock;
reconciliation records candidates but cannot prove a failed HTTP call was not
committed. The journal remains authoritative after client cancellation/restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from .client import DirectError
from .identifiers import wire

_TASKS: dict[str, asyncio.Task] = {}
PROCESS_ID = uuid.uuid4().hex


def _atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(wire(payload), stream, ensure_ascii=False, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _request(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (
                {"sha256": hashlib.sha256(str(v).encode()).hexdigest()}
                if k == "ImageData"
                else _request(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_request(v) for v in value]
    return value


class Journal:
    def __init__(self, path: Path, payload: dict):
        self.path, self.payload = path, payload

    def save(self) -> None:
        self.payload["updated_at"] = time.time()
        _atomic(self.path, self.payload)

    def event(self, **event) -> None:
        self.payload["events"].append({"at": time.time(), **event})
        self.save()

    def verification_plan(self, plan: dict) -> None:
        # Opaque JSON retains native int64 exactly across the string-ID wire layer.
        encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.payload["verification_plan_json"] = encoded
        self.payload["verification_plan_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        self.save()

    def checkpoint(self, result: dict) -> None:
        self.payload["result"] = deepcopy(result)
        self.save()


class JournalAPI:
    def __init__(self, api, journal: Journal):
        self.api, self.journal = api, journal

    def __getattr__(self, name):
        return getattr(self.api, name)

    async def call(self, service, method, params=None, *, client_login=None):
        return await self._call("call", service, method, params, client_login)

    async def call_v501(self, service, method, params=None, *, client_login=None):
        return await self._call("call_v501", service, method, params, client_login)

    async def _call(self, branch, service, method, params, login):
        call = getattr(self.api, branch)
        if method.lower() == "get":
            return await call(service, method, params, client_login=login)
        sequence = len(self.journal.payload["events"])
        self.journal.event(
            stage="request",
            sequence=sequence,
            service=service,
            method=method,
            params=_request(params),
            client_login=login,
        )
        try:
            result = await call(service, method, params, client_login=login)
        except BaseException as exc:
            if (
                isinstance(exc, DirectError)
                and exc.code is not None
                and exc.code not in {52, 506, 1000, 1001, 1002, 1020}
            ):
                self.journal.event(
                    stage="rejected", sequence=sequence, code=exc.code, error=str(exc)
                )
                raise
            self.journal.payload["uncertain"] = True
            self.journal.event(
                stage="unknown_outcome",
                sequence=sequence,
                error=type(exc).__name__ + ": " + str(exc),
            )
            if isinstance(exc, Exception):
                try:
                    evidence = await reconcile(self.api, service, method, params, login)
                    self.journal.event(stage="reconciliation", sequence=sequence, evidence=evidence)
                except Exception as read_error:  # noqa: BLE001 - retain unknown outcome
                    self.journal.event(
                        stage="reconciliation_failed", sequence=sequence, error=str(read_error)
                    )
            raise
        # Incomplete write responses may hide committed IDs too.
        action_key = {"add": "AddResults", "update": "UpdateResults"}.get(method)
        if action_key:
            expected = len(next(iter(params.values())))
            actions = result.get(action_key)
            if not isinstance(actions, list) or len(actions) != expected:
                self.journal.payload["uncertain"] = True
        # Store before returning to the executor or making another remote call.
        self.journal.event(stage="response", sequence=sequence, result=result)
        return result


async def reconcile(api, service: str, method: str, params: dict, login: str) -> dict:
    collections = {
        "campaigns": "Campaigns",
        "adgroups": "AdGroups",
        "ads": "Ads",
        "keywords": "Keywords",
    }
    collection = collections.get(service)
    if not collection:
        return {"resolved": False, "reason": "Требуется отдельная сверка этого сервиса"}
    requested = params.get(collection, [])
    if method == "update":
        filters = [{"Ids": [row["Id"] for row in requested]}]
    elif service == "campaigns":
        filters = [{"States": ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED", "ARCHIVED"]}]
    else:
        key = "CampaignId" if service == "adgroups" else "AdGroupId"
        ids = list(dict.fromkeys(row[key] for row in requested))
        filters = [{key + "s": ids[i : i + 10]} for i in range(0, len(ids), 10)]
    rows = []
    truncated = False
    for criteria in filters:
        fields = ["Id", "Name"] if service in {"campaigns", "adgroups"} else ["Id"]
        query = {"SelectionCriteria": criteria, "FieldNames": fields, "Page": {"Limit": 10000}}
        if service == "ads":
            query["ResponsiveAdFieldNames"] = ["Titles", "Texts", "Href"]
        if service == "keywords":
            query["FieldNames"] += ["Keyword", "AdGroupId"]
        result = await api.call_v501(service, "get", query, client_login=login)
        candidates = result.get(collection, [])
        if service == "campaigns":
            names = {row["Name"] for row in requested}
            candidates = [row for row in candidates if row.get("Name") in names]
        rows.extend(candidates)
        truncated |= result.get("LimitedBy") is not None
    return {
        "resolved": False,
        "candidates": rows,
        "truncated": truncated,
        "note": "Сверочное чтение; не доказательство соответствия и не разрешение повторной записи",
    }


def read(out_dir: Path, job_id: str, client_login: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Некорректный job_id")
    path = out_dir / "jobs" / (job_id + ".json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload["client_login"].casefold() != client_login.casefold():
        raise PermissionError("job_id относится к другому клиенту")
    return payload


def _can_retry_before_write(payload: dict) -> bool:
    return (
        payload["status"] == "failed"
        or (payload["status"] == "done" and payload.get("result", {}).get("status") == "blocked")
    ) and not any(event.get("stage") == "request" for event in payload.get("events", []))


def start(
    out_dir: Path,
    client_login: str,
    plan_hash: str,
    kind: str,
    operation,
    *,
    receipt: dict | None = None,
) -> str:
    directory = out_dir / "jobs"
    directory.mkdir(exist_ok=True, parents=True)
    job_id = (
        uuid.uuid4().hex
        if kind == "preview"
        else hashlib.sha256(
            (client_login.casefold() + ":" + kind + ":" + plan_hash).encode()
        ).hexdigest()[:32]
    )
    path = directory / (job_id + ".json")
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if previous and not _can_retry_before_write(previous):
        return job_id
    lock = None
    if kind != "preview":
        lock = directory / (
            "login-" + hashlib.sha256(client_login.casefold().encode()).hexdigest() + ".lock"
        )
        try:
            with lock.open("x", encoding="utf-8") as stream:
                stream.write(job_id)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            if path.exists():
                return job_id
            raise ValueError(
                "Запись для логина уже выполняется или требует сверки; job_id="
                + lock.read_text(encoding="utf-8")
            ) from exc
    # Recheck under the cross-process lock: another caller may have retried meanwhile.
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if previous and not _can_retry_before_write(previous):
        if lock:
            lock.unlink()
        return job_id
    if previous:
        _atomic(directory / (job_id + ".attempt-" + uuid.uuid4().hex + ".json"), previous)
    journal = Journal(
        path,
        {
            "schema": "direct_write_job_v1",
            "job_id": job_id,
            "kind": kind,
            "client_login": client_login,
            "plan_hash": plan_hash,
            "owner_process": PROCESS_ID,
            "owner_pid": os.getpid(),
            "status": "running",
            "created_at": time.time(),
            "events": [],
            "approval": receipt,
            "uncertain": False,
        },
    )
    try:
        journal.save()
    except BaseException:
        if lock:
            lock.unlink()
        raise

    async def run():
        try:
            result = await operation(journal)
            journal.checkpoint(result)
            journal.payload["status"] = (
                "needs_reconciliation" if journal.payload["uncertain"] else "done"
            )
        except asyncio.CancelledError:
            journal.payload["status"] = "interrupted"
            journal.payload["uncertain"] = kind != "preview"
            journal.payload["error"] = "Процесс остановлен; повторная запись запрещена до сверки"
        except Exception as exc:  # noqa: BLE001 - durable task error boundary
            journal.payload["status"] = (
                "needs_reconciliation" if journal.payload["uncertain"] else "failed"
            )
            journal.payload["error"] = str(exc)
        finally:
            journal.save()
            if lock and not journal.payload["uncertain"]:
                lock.unlink(missing_ok=True)

    task = asyncio.create_task(run(), name="direct-job-" + job_id)
    _TASKS[job_id] = task
    task.add_done_callback(lambda _: _TASKS.pop(job_id, None))
    return job_id


async def wait_briefly(job_id: str, seconds: float = 0.2) -> None:
    task = _TASKS.get(job_id)
    if task is not None:
        # wait() never propagates caller cancellation into the background task.
        await asyncio.wait({task}, timeout=seconds)
