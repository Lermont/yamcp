"""Прогноз и представление расхода баллов Yandex Direct API v5/v501."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any

RATE_SNAPSHOT = json.loads(
    (Path(__file__).parent / "policy_data" / "api_add_units.json").read_text(encoding="utf-8")
)
ADD_RATES = RATE_SNAPSHOT["rates"]


def rates_current() -> bool:
    verified = datetime.fromisoformat(RATE_SNAPSHOT["verified_on"]).date()
    age = (datetime.now(UTC).date() - verified).days
    return 0 <= age <= RATE_SNAPSHOT["maximum_age_days"]

ADD_BATCH_LIMITS = {
    "campaigns": 10,
    "bid_modifiers": 1000,
    "ad_groups": 1000,
    "ads": 1000,
    "keywords": 1000,
    "retargeting_lists": 1000,
    "audience_targets": 1000,
}


def estimate_add(summary: dict[str, Any]) -> dict[str, Any]:
    """Оценить баллы успешной write-фазы по числу создаваемых объектов."""
    services = []
    total = 0
    total_calls = 0
    for key, rate in ADD_RATES.items():
        objects = int(summary.get(key, 0) or 0)
        if key == "keywords":
            objects = (int(summary.get("criteria", objects) or 0)
                       - int(summary.get("audience_targets", 0)))
        if key in {"audience_targets", "retargeting_lists"} and not objects:
            continue
        if objects < 0:
            raise ValueError(f"summary.{key} не может быть отрицательным")
        calls = ceil(objects / ADD_BATCH_LIMITS[key]) if objects else 0
        call_units = calls * rate["per_call"]
        object_units = objects * rate["per_object"]
        estimated = call_units + object_units
        services.append({
            "service": rate["label"],
            "objects": objects,
            "calls": calls,
            "call_units": call_units,
            "object_units": object_units,
            "estimated_units": estimated,
        })
        total += estimated
        total_calls += calls
    return {
        "scope": "successful_add_calls_only",
        "rates_source": RATE_SNAPSHOT["source"],
        "rates_verified_on": RATE_SNAPSHOT["verified_on"],
        "rates_current": rates_current(),
        "estimated_units": total,
        "estimated_requests": total_calls,
        "services": services,
        "not_included": [
            "get-вызовы preflight и readback",
            "вызовы Вордстата v4",
            "штрафы за ошибочные операции",
            "создание дополнений вне этого bundle",
        ],
    }
