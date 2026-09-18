"""Carry incomplete API evidence through composite audit/readback results."""

from typing import Any


def sources(**payloads: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"source": name, "limited_by": value.get("limited_by", value.get("LimitedBy")),
         "reason": value.get("error") or value.get("note") or "API response truncated"}
        for name, value in payloads.items()
        if value.get("truncated") or value.get("limited_by") is not None
        or value.get("LimitedBy") is not None or value.get("error")
    ]
