"""Каталог целей Метрики, доступных кампании, через API v4 GetStatGoals."""

from __future__ import annotations

from typing import Any


async def read(api: Any, campaign_id: int) -> dict[str, Any]:
    """Вернуть ID и названия целей; цвет и дата создания в ответ v4 не входят."""
    value = int(campaign_id)
    if value <= 0:
        raise ValueError("campaign_id должен быть положительным")
    result = await api.call_v4("GetStatGoals", {"CampaignID": value}) or []
    goals = [
        {"id": row.get("GoalID"), "name": row.get("Name")}
        for row in result
        if isinstance(row, dict)
    ]
    return {
        "campaign_id": value,
        "goals": goals,
        "count": len(goals),
        "api_version": "v4",
        "limitations": [
            "API возвращает только GoalID и Name, без цвета цели в интерфейсе.",
            "API не возвращает дату создания, поэтому новые серые цели нужно проверять вручную.",
        ],
    }


async def read_retargeting(api: Any, client_login: str) -> dict[str, Any]:
    """Client-scoped catalog; segments are not optimization/conversion goals."""
    from .identifiers import parse_id

    result = await api.call_v4("GetRetargetingGoals", {"Logins": [client_login]})
    if not isinstance(result, list):
        raise ValueError("GetRetargetingGoals: incomplete or invalid catalog")
    rows = []
    seen = set()
    for row in result:
        if not isinstance(row, dict) or row.get("Login") not in (None, "", client_login):
            raise ValueError("GetRetargetingGoals: foreign or invalid catalog row")
        identifier = parse_id(row.get("GoalID"), "GetRetargetingGoals.GoalID")
        if identifier in seen:
            raise ValueError("GetRetargetingGoals: duplicate ID")
        seen.add(identifier)
        rows.append({"id": identifier, "name": row.get("Name"),
                     "type": row.get("Type"), "domain": row.get("GoalDomain")})
    return {"client_login": client_login, "goals": rows, "count": len(rows),
            "api_version": "live_v4", "complete": True,
            "limitations": ["Audience size and readiness require separate verification."]}
