"""Persisted monthly search forecast, without advertising account mutations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .client import DirectError
from .wordstat import prepare

INSTRUCTIONS = (
    "Прогноз: сначала прочитай direct://kb/forecast-quality. Если приоритет — бюджет, "
    "подбирай CPC и уровни каждой фразы для максимума релевантных кликов в бюджете; "
    "при равных кликах выбирай меньший расход. Предложенные пороги сравнивай как "
    "сценарии; явный жёсткий предел пользователя соблюдай. Нужны отдельные CPC, "
    "клики и расход всех доступных уровней. Legacy direct_forecast done/complete "
    "подтверждает исходные строки, не готовность медиаплана: недостающие уровни "
    "получи через интерфейс Директа и сверь выбранные строки и суммы. Ставка Bid "
    "не равна списанию Price; бюджет/CPC не является прогнозом кликов. Частотность "
    "бери через MCP, все фразы с уровнями и цифрами показывай на основной странице "
    "отчёта. При новых ограничениях пересчитай подбор; не раскрывай внутренние редакции."
)


def _save(path: Path, record: dict) -> None:
    body = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
    stage = path.with_suffix(".tmp")
    stage.write_bytes(body)
    stage.replace(path)


def _summary(path: Path, record: dict) -> dict:
    return {
        "status": record["status"], "artifact_path": str(path),
        "forecast_id": record.get("forecast_id"),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "request": record["request"], "collected_at": record["collected_at"],
        "rows": len(record.get("response", {}).get("Phrases", [])),
        "complete": record.get("complete", False),
        "warnings": record.get("warnings", []),
        "quality_contract": {
            "version": "2026-10-01",
            "requirements_uri": "direct://kb/forecast-quality",
            "data_scope": "legacy_position_estimates",
            "complete_scope": "provider_response_rows_only",
            "traffic_level_clicks_available": False,
            "ready_for_budget_optimization": False,
            "next_action": (
                "Read requirements; obtain per-phrase traffic-level CPC, clicks and "
                "cost from the Direct forecast UI. When budget is primary, maximize "
                "relevant clicks within budget, choosing lower cost on ties. Respect "
                "explicit hard CPC limits; suggested caps are comparison scenarios. "
                "Verify selected levels and totals before reporting."
            ),
        },
        "note": "Monthly legacy forecast; no guaranteed positions or conversions. "
                "Auction Bid is not charged Price. Complete is not a finished media plan. "
                "Read the saved response and quality_contract before modelling.",
    }


async def run(api, *, out_dir: Path, action: str, phrases: list[str] | None = None,
              geo_ids: list[int] | None = None, artifact_path: str | None = None,
              probe_pending: bool = False) -> dict:
    if action not in {"create", "get"}:
        raise ValueError("action must be create or get")
    if action == "create":
        if artifact_path:
            raise ValueError("create does not accept artifact_path")
        cleaned = prepare(phrases or [])
        if not geo_ids or any(type(x) is not int or x <= 0 for x in geo_ids):
            raise ValueError("Explicit positive geo_ids required; verify with direct_regions")
        request = {"Phrases": cleaned, "GeoID": sorted(set(geo_ids)),
                   "Currency": "RUB", "AuctionBids": "Yes"}
        path = out_dir / ("forecast_" + uuid4().hex + ".json")
        record = {"source": "direct_live_v4", "schema": 1, "request": request,
                  "collected_at": datetime.now(UTC).isoformat(), "status": "requested"}
        await asyncio.to_thread(_save, path, record)
        # No automatic retry: an interrupted create may have reached the provider.
        rid = await api.call_v4("CreateNewForecast", request)
        if type(rid) is not int or rid <= 0:
            raise DirectError("CreateNewForecast returned an invalid identifier")
        record.update(forecast_id=rid, status="pending")
        await asyncio.to_thread(_save, path, record)
        return await asyncio.to_thread(_summary, path, record)
    if phrases is not None or geo_ids is not None:
        raise ValueError("get accepts only artifact_path")
    if not artifact_path:
        raise ValueError("artifact_path required")
    path = await asyncio.to_thread(Path(artifact_path).resolve)
    root = await asyncio.to_thread(out_dir.resolve)
    if not path.is_relative_to(root) or path.suffix != ".json":
        raise ValueError("Artifact must be a JSON file inside YD_OUT_DIR")
    record = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    if record.get("source") != "direct_live_v4" or record.get("schema") != 1:
        raise ValueError("Not a forecast artifact")
    if record["status"] == "done":
        return await asyncio.to_thread(_summary, path, record)
    rid = record.get("forecast_id")
    if type(rid) is not int or rid <= 0:
        raise ValueError("Creation outcome unknown; do not automatically recreate")
    listing = await api.call_v4("GetForecastList")
    matching = [x for x in (listing or []) if x.get("ForecastID") == rid]
    if len(matching) != 1:
        raise DirectError("Forecast missing or duplicated in provider queue")
    status = matching[0].get("StatusForecast")
    if status in {"Pending", "New"} and not probe_pending:
        return await asyncio.to_thread(_summary, path, record)
    if status not in {"Done", "Pending", "New"}:
        raise DirectError("Unexpected forecast status: " + str(status))
    data = await api.call_v4("GetForecast", rid)
    if not isinstance(data, dict) or not isinstance(data.get("Phrases"), list):
        raise DirectError("Invalid forecast response")
    rows = data["Phrases"]
    names = [x.get("Phrase") for x in rows]
    complete = (len(rows) == len(record["request"]["Phrases"])
                and all(isinstance(x, str) for x in names) and len(set(names)) == len(names)
                and all(x.get("Currency") == "RUB" for x in rows))
    record.update(status="done", response=data, complete=complete,
                  completed_at=datetime.now(UTC).isoformat(), warnings=[])
    if not complete:
        record["warnings"].append("Incomplete, duplicate or currency-mismatched rows")
    # Persist before cleaning up only our own temporary provider report.
    await asyncio.to_thread(_save, path, record)
    try:
        await api.call_v4("DeleteForecastReport", rid)
        record["provider_report_deleted"] = True
    except DirectError:
        record["warnings"].append("Own temporary forecast report cleanup failed")
    await asyncio.to_thread(_save, path, record)
    return await asyncio.to_thread(_summary, path, record)
