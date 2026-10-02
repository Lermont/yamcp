"""Resolve conversion reporting against current campaign settings, read-only."""

from __future__ import annotations

import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from . import campaigns, reports

CONVERSION_FIELDS = frozenset(
    {
        "Conversions",
        "ConversionRate",
        "CostPerConversion",
        "GoalsRoi",
        "Revenue",
        "Profit",
        "PurchaseGoals",
        "PurchaseRevenue",
        "PurchaseProfit",
        "PurchaseGoalsRoi",
    }
)

CACHE_TTL_SECONDS = 300


async def _settings(api: Any, login: str, ids: list[int] | None) -> dict[str, Any]:
    cache = getattr(api, "_report_settings_cache", None)
    if cache is None:
        cache = api._report_settings_cache = {}
    key = (login.casefold(), tuple(ids or []))
    cached = cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return deepcopy(cached[1])
    payload = await campaigns.read_settings(
        api, login, campaign_ids=ids, include_archived=True,
        limit=campaigns.MAX_LIMIT, report_context_only=True,
    )
    payload["settings_read_at"] = datetime.now(UTC).isoformat()
    if not payload.get("truncated"):
        if len(cache) >= 32:
            cache.pop(next(iter(cache)))
        cache[key] = (time.monotonic(), deepcopy(payload))
    return payload


def needs_conversion_context(contract: dict[str, Any]) -> bool:
    fields = set(contract["fields"])
    fields.update(row["Field"] for row in contract.get("filters") or [])
    return bool(fields & CONVERSION_FIELDS or contract.get("goals"))


def _goal_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        if value.get("BiddingStrategyType") in {"SERVING_OFF", "UNKNOWN"}:
            return set()
        result = {str(value["GoalId"])} if value.get("GoalId") is not None else set()
        for child in value.values():
            result.update(_goal_ids(child))
        return result
    if isinstance(value, list):
        return set().union(*(_goal_ids(child) for child in value))
    return set()


def campaign_goals(campaign: dict[str, Any]) -> tuple[list[str] | None, str]:
    if campaign.get("package_bidding_strategy"):
        return None, "portfolio_strategy_requires_explicit_goals"
    priority = _goal_ids(campaign.get("priority_goals"))
    strategy = _goal_ids(campaign.get("bidding_strategy"))
    goals = strategy or priority
    source = "strategy_goal" if strategy else "priority_goals"
    if "13" in goals:
        if not priority - {"12"} or "13" in priority:
            return None, "priority_goals_unavailable"
        goals = (goals - {"13"}) | priority
        source = "strategy_priority_goals"
    if not goals or any(not value.isdigit() or int(value) <= 0 for value in goals):
        return None, "business_goals_unavailable"
    return sorted({str(int(value)) for value in goals}, key=int), source


def _campaign_filter(contract: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (row for row in contract.get("filters") or [] if row["Field"] == "CampaignId"), None
    )


async def resolve(
    api: Any,
    client_login: str,
    contract: dict[str, Any],
    *,
    conversion_scope: str = "campaign",
) -> tuple[dict[str, Any], dict[str, Any]]:
    if conversion_scope not in {"campaign", "all_goals"}:
        raise ValueError("conversion_scope: используйте campaign или all_goals")
    result = dict(contract)
    metadata: dict[str, Any] = {
        "conversion_scope": conversion_scope,
        "goals": contract["goals"],
        "attribution_models": contract["attribution_models"],
        "warnings": [],
    }
    if conversion_scope == "all_goals":
        if contract["goals"] or contract["attribution_models"]:
            raise ValueError("conversion_scope=all_goals несовместим с goals/attribution_models")
        metadata.update(
            {
                "resolution": "explicit_all_goals",
                "attribution_models": ["LC"],
                "attribution_source": "direct_api_default",
                "metric_semantics": "all_goal_completions_not_leads",
                "warnings": [
                    "Явно запрошена сумма по всем целям с атрибуцией LC по умолчанию API. "
                    "Она не является числом заявок или уникальных клиентов "
                    "и не сверена со стратегией."
                ],
            }
        )
        return result, metadata
    if not needs_conversion_context(contract):
        metadata["resolution"] = "not_applicable"
        return result, metadata

    campaign_filter = _campaign_filter(contract)
    selected_ids = None
    if campaign_filter and campaign_filter["Operator"] in {"IN", "EQUALS"}:
        values = campaign_filter["Values"]
        if any(not value.isdigit() or int(value) <= 0 for value in values):
            raise ValueError("CampaignId должен содержать положительные ID")
        selected_ids = sorted({int(value) for value in values})
    # Read the complete possible scope, including archived campaigns that may
    # have historical spend. Other report filters can only narrow this scope.
    payload = await _settings(api, client_login, selected_ids)
    if payload.get("truncated"):
        raise ValueError(
            "Настройки кампаний прочитаны не полностью; сузьте отчёт фильтром CampaignId IN"
        )
    rows = payload.get("campaigns", [])
    if selected_ids:
        found = {row.get("id") for row in rows}
        if set(selected_ids) - found:
            raise ValueError(
                "Не получены настройки всех CampaignId отчёта; проверьте доступ и ID кампаний"
            )
        rows = [row for row in rows if row.get("id") in selected_ids]
    if campaign_filter and selected_ids is None:
        operator = campaign_filter["Operator"]
        if operator in {"NOT_IN", "NOT_EQUALS"}:
            excluded = set(campaign_filter["Values"])
            rows = [row for row in rows if str(row.get("id")) not in excluded]
    if not rows:
        raise ValueError("Не найдены настройки кампаний для согласования целей и атрибуции отчёта")

    snapshots = []
    for row in rows:
        actual_goals, goal_source = campaign_goals(row)
        snapshots.append(
            {
                "campaign_id": row.get("id"),
                "campaign_type": row.get("type"),
                "goals": actual_goals,
                "goal_source": goal_source,
                "attribution_model": row.get("attribution_model"),
                "counter_ids": row.get("counter_ids") or [],
                "time_zone": row.get("time_zone"),
            }
        )
    model_values = {row["attribution_model"] for row in snapshots}
    goal_values = {tuple(row["goals"]) if row["goals"] else None for row in snapshots}
    guidance = (
        "Разделите отчёт по CampaignId либо явно задайте goals и attribution_models для сравнения."
    )
    if not contract["goals"]:
        if len(goal_values) != 1 or None in goal_values:
            raise ValueError("Цели кампаний различаются или недоступны. " + guidance)
        result["goals"] = list(next(iter(goal_values)))
    if not contract["attribution_models"]:
        if len(model_values) != 1 or not model_values <= reports.ATTRIBUTION_MODELS:
            raise ValueError("Атрибуция кампаний различается или недоступна. " + guidance)
        result["attribution_models"] = [next(iter(model_values))]
    result = reports.normalize(**result)
    warnings = [
        "Конверсии — достижения выбранных целей, "
        "не подтверждённое число заявок или уникальных клиентов.",
        "Использован текущий снимок настроек; "
        "он не подтверждает настройки за весь исторический период.",
    ]
    mismatches = [
        row["campaign_id"]
        for row in snapshots
        if set(row["goals"] or []) != set(result["goals"])
        or [row["attribution_model"]] != result["attribution_models"]
    ]
    if mismatches:
        warnings.append(
            "Явные параметры сравнения отличаются от настроек части кампаний "
            "или настройки целей неизвестны."
        )
    if "12" in result["goals"]:
        warnings.append("Цель 12 — вовлечённые сессии; она не подтверждает обращение в компанию.")
    narrowed = any(row["Field"] != "CampaignId" for row in contract.get("filters") or [])
    if narrowed or (campaign_filter and selected_ids is None):
        warnings.append(
            "Для согласования использована полная возможная выборка кампаний; "
            "остальные фильтры отчёта могут её сузить."
        )
    metadata.update(
        {
            "resolution": "explicit_comparison"
            if contract["goals"] and contract["attribution_models"]
            else "campaign_settings",
            "goals": result["goals"],
            "attribution_models": result["attribution_models"],
            "goal_source": "explicit" if contract["goals"] else "campaign_settings",
            "attribution_source": "explicit"
            if contract["attribution_models"]
            else "campaign_settings",
            "metric_semantics": "selected_goal_completions_not_verified_leads",
            "settings_read_at": payload["settings_read_at"],
            "settings_cache_ttl_seconds": CACHE_TTL_SECONDS,
            "campaign_settings": snapshots,
            "settings_scope": "possible_campaigns_before_other_report_filters",
            "comparison_mismatch_campaign_ids": mismatches,
            "warnings": warnings,
        }
    )
    return result, metadata
