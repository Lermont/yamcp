"""Bounded MCP views and paged access to full JSON artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .identifiers import wire

MAX_FINDINGS = 30
MAX_CAMPAIGNS = 20


def _small(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 400 else value[:400] + "… [см. артефакт]"
    if isinstance(value, (dict, list)) and depth >= 3:
        return {"items_count": len(value), "detail": "см. артефакт"}
    if isinstance(value, dict):
        items = list(value.items())
        result = {key: _small(item, depth + 1) for key, item in items[:12]}
        if len(items) > 12:
            result["fields_truncated"] = True
        return result
    if isinstance(value, list):
        result = [_small(item, depth + 1) for item in value[:5]]
        if len(value) > 5:
            result.append({"omitted_items": len(value) - 5})
        return result
    return value


def compact(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep decisions, totals and exact artifact pointers; omit raw API objects."""
    result = {
        key: _small(payload[key]) for key in (
            "schema", "client_login", "plan_hash", "status", "executed", "ready", "policy",
            "summary", "campaigns_count", "source_truncated", "generated_at", "artifact_path",
            "incomplete_sources", "data_complete",
            "confirmation_required", "confirmation_ttl_seconds", "api_units_estimate",
            "preflight", "required_manual_actions", "creation_report", "fatal_error", "message",
            "api_creation_complete", "setup_complete",
        ) if key in payload
    }
    findings = []
    for i, row in enumerate(payload.get("findings", [])):
        if row.get("status") != "PASS":
            findings.append((f"/findings/{i}", row))
    overview = []
    for i, item in enumerate(payload.get("campaigns", [])):
        campaign = item.get("campaign", item)
        overview.append({
            "id": campaign.get("id", campaign.get("Id")),
            "name": str(campaign.get("name", campaign.get("Name", "")))[:200],
            "channel": item.get("channel"), "ready": item.get("ready"),
            "pointer": f"/campaigns/{i}",
        })
        for j, row in enumerate(item.get("findings", [])):
            if row.get("status") != "PASS":
                findings.append((f"/campaigns/{i}/findings/{j}", row))
    findings.sort(key=lambda item: {"BLOCK": 0, "WARNING": 1}.get(item[1].get("status"), 2))
    result["campaigns"] = overview[:MAX_CAMPAIGNS]
    result["campaigns_total"] = len(overview)
    result["findings"] = [
        {"pointer": pointer, **{
            key: _small(row[key]) for key in ("rule", "status", "message") if key in row
        }} for pointer, row in findings[:MAX_FINDINGS]
    ]
    result["findings_total"] = len(findings)
    result["findings_truncated"] = len(findings) > MAX_FINDINGS
    result["detail_omitted"] = True
    result["hint"] = (
        "Полный JSON и факты проверок: direct_read_artifact(path=artifact_path, pointer=...). "
        "Перед подтверждением прочитайте нужные разделы; сводка содержит не все настройки."
    )
    return result


def read(path: Path, pointer: str = "", offset: int = 0, limit: int = 8000) -> dict[str, Any]:
    """Page serialized JSON by characters, including arbitrarily large leaf values."""
    if offset < 0 or not 1 <= limit <= 16000:
        raise ValueError("offset >= 0; limit от 1 до 16000 символов")
    if pointer and not pointer.startswith("/"):
        raise ValueError("pointer должен быть пустым или JSON Pointer с начальным /")
    value = json.loads(path.read_text(encoding="utf-8"))
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ValueError("Некорректный индекс JSON Pointer")
            index = int(token)
            if index >= len(value):
                raise ValueError("JSON Pointer вне массива")
            value = value[index]
        elif isinstance(value, dict) and token in value:
            value = value[token]
        else:
            raise ValueError("JSON Pointer не найден")
    serialized = json.dumps(wire(value, pointer.rsplit("/", 1)[-1]), ensure_ascii=False, indent=2)
    window = serialized[offset:offset + limit]
    return {
        "path": str(path), "pointer": pointer, "offset": offset,
        "characters_total": len(serialized), "returned": len(window),
        "json_fragment": window,
        "next_offset": offset + len(window) if offset + len(window) < len(serialized) else None,
    }
