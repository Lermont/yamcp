"""Lossless IDs at the MCP boundary; native integers in Direct requests."""

from __future__ import annotations

import re
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
DirectId = int | str


def parse_id(value: Any, field: str = "id") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field}: требуется целый ID или десятичная строка")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{field}: требуется десятичная строка ID")
    number = int(value)
    if not 0 < number <= 2**63 - 1:
        raise ValueError(f"{field}: ID вне диапазона положительного int64")
    return number


def wire(value: Any, key: str = "") -> Any:
    is_id = key.lower() in {"id", "ids"} or bool(
        re.search(r"(?:[a-z](?:Ids?|IDs?)$|_ids?$|_id_list$)", key)
    )
    if isinstance(value, dict):
        return {k: wire(v, key if is_id and k == "Items" else str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [wire(v, key) for v in value]
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value) if is_id or abs(value) > MAX_SAFE_INTEGER else value
    return value
