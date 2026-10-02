"""Полное read-only чтение настроек кампаний через JSON API v501."""

from __future__ import annotations

from typing import Any

MAX_LIMIT = 10000

COMMON_FIELDS = [
    "Id",
    "Name",
    "Type",
    "State",
    "Status",
    "StartDate",
    "EndDate",
    "TimeZone",
    "TimeTargeting",
    "NegativeKeywords",
    "ExcludedSites",
    "BlockedIps",
    "DailyBudget",
]
TEXT_FIELDS = [
    "BiddingStrategy",
    "Settings",
    "CounterIds",
    "PriorityGoals",
    "AttributionModel",
    "PackageBiddingStrategy",
    "NegativeKeywordSharedSetIds",
]
UNIFIED_FIELDS = [
    "BiddingStrategy",
    "Settings",
    "CounterIds",
    "PriorityGoals",
    "TrackingParams",
    "AttributionModel",
    "PackageBiddingStrategy",
    "NegativeKeywordSharedSetIds",
]
TEXT_SEARCH_PLACEMENTS = ["SearchResults", "ProductGallery", "DynamicPlaces"]
UNIFIED_SEARCH_PLACEMENTS = [
    "SearchResults",
    "ProductGallery",
    "DynamicPlaces",
    "Maps",
    "SearchOrganizationList",
]
UNIFIED_PACKAGE_PLATFORMS = [
    "SearchResult",
    "ProductGallery",
    "Maps",
    "SearchOrganizationList",
    "Network",
    "DynamicPlaces",
]


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.get("Items") or [])
    return list(value) if isinstance(value, list) else []


def _shape(item: dict[str, Any]) -> dict[str, Any]:
    campaign_type = item.get("Type")
    typed = item.get("UnifiedCampaign") or item.get("TextCampaign") or {}
    daily = item.get("DailyBudget") or {}
    return {
        "id": item.get("Id"),
        "name": item.get("Name"),
        "type": campaign_type,
        "state": item.get("State"),
        "status": item.get("Status"),
        "start_date": item.get("StartDate"),
        "end_date": item.get("EndDate"),
        "time_zone": item.get("TimeZone"),
        "time_targeting": item.get("TimeTargeting"),
        "daily_budget_micros": daily.get("Amount"),
        "daily_budget_mode": daily.get("Mode"),
        "negative_keywords": _items(item.get("NegativeKeywords")),
        "excluded_sites": _items(item.get("ExcludedSites")),
        "blocked_ips": _items(item.get("BlockedIps")),
        "settings": {
            row.get("Option"): row.get("Value")
            for row in typed.get("Settings") or []
            if row.get("Option")
        },
        "counter_ids": _items(typed.get("CounterIds")),
        "priority_goals": _items(typed.get("PriorityGoals")),
        "tracking_params": typed.get("TrackingParams"),
        "attribution_model": typed.get("AttributionModel"),
        "negative_keyword_shared_set_ids": _items(
            typed.get("NegativeKeywordSharedSetIds")
        ),
        "bidding_strategy": typed.get("BiddingStrategy"),
        "package_bidding_strategy": typed.get("PackageBiddingStrategy"),
    }


async def read_settings(
    api: Any,
    client_login: str,
    *,
    campaign_ids: list[int] | None = None,
    include_archived: bool = False,
    limit: int = 100,
    report_context_only: bool = False,
) -> dict[str, Any]:
    """Получить нормализованные настройки TextCampaign и UnifiedCampaign."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit должен быть от 1 до {MAX_LIMIT}")
    if campaign_ids and len(campaign_ids) > 1000:
        raise ValueError("campaign_ids принимает не более 1000 идентификаторов")

    if campaign_ids:
        selection: dict[str, Any] = {"Ids": [int(value) for value in campaign_ids]}
    else:
        states = ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED"]
        if include_archived:
            states.append("ARCHIVED")
        selection = {"States": states}

    params = {
        "SelectionCriteria": selection,
        "FieldNames": COMMON_FIELDS,
        "TextCampaignFieldNames": TEXT_FIELDS,
        "TextCampaignSearchStrategyPlacementTypesFieldNames": TEXT_SEARCH_PLACEMENTS,
        "UnifiedCampaignFieldNames": UNIFIED_FIELDS,
        "UnifiedCampaignSearchStrategyPlacementTypesFieldNames": (
            UNIFIED_SEARCH_PLACEMENTS
        ),
        "UnifiedCampaignPackageBiddingStrategyPlatformsFieldNames": (
            UNIFIED_PACKAGE_PLATFORMS
        ),
        "Page": {"Limit": limit},
    }
    if report_context_only:
        fields = ["BiddingStrategy", "CounterIds", "PriorityGoals",
                  "AttributionModel", "PackageBiddingStrategy"]
        params = {
            "SelectionCriteria": selection, "FieldNames": ["Id", "Type", "TimeZone"],
            "TextCampaignFieldNames": fields, "UnifiedCampaignFieldNames": fields,
            "Page": {"Limit": limit},
        }
    result = await api.call_v501(
        "campaigns",
        "get",
        params,
        client_login=client_login,
    )
    campaigns = [_shape(item) for item in result.get("Campaigns", [])]
    payload: dict[str, Any] = {
        "client_login": client_login,
        "api_version": "v501",
        "campaigns": campaigns,
        "count": len(campaigns),
    }
    if result.get("LimitedBy") is not None:
        payload.update({
            "truncated": True,
            "limited_by": result["LimitedBy"],
            "note": "Выдача ограничена Direct API; сузьте campaign_ids или увеличьте limit.",
        })
    return payload
