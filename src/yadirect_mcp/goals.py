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
