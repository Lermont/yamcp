"""Локальная проверка контракта Reports API v501.

Невалидный запрос к Reports стоит API-баллов и часто обнаруживается только
после постановки отчёта в очередь. Поэтому MCP проверяет перечисления,
допустимость полей для типа отчёта и документированные несовместимости до
сетевого вызова. Таблица соответствует официальному ``fields-list`` v501.
"""

from __future__ import annotations

from typing import Any

ACCOUNT = "ACCOUNT_PERFORMANCE_REPORT"
AD = "AD_PERFORMANCE_REPORT"
ADGROUP = "ADGROUP_PERFORMANCE_REPORT"
CAMPAIGN = "CAMPAIGN_PERFORMANCE_REPORT"
CRITERIA = "CRITERIA_PERFORMANCE_REPORT"
CUSTOM = "CUSTOM_REPORT"
REACH = "REACH_AND_FREQUENCY_PERFORMANCE_REPORT"
SEARCH_QUERY = "SEARCH_QUERY_PERFORMANCE_REPORT"

REPORT_TYPES = frozenset({
    ACCOUNT,
    AD,
    ADGROUP,
    CAMPAIGN,
    CRITERIA,
    CUSTOM,
    REACH,
    SEARCH_QUERY,
})
ALL_TYPES = REPORT_TYPES


def _types(*values: str) -> frozenset[str]:
    return frozenset(values)


# Поле -> типы отчётов, в которых таблица Direct помечает его не прочерком.
FIELD_REPORT_TYPES: dict[str, frozenset[str]] = {
    "AdFormat": _types(ACCOUNT, AD, ADGROUP, CAMPAIGN, CUSTOM),
    "AdGroupId": _types(AD, ADGROUP, CRITERIA, CUSTOM, REACH, SEARCH_QUERY),
    "AdGroupName": _types(AD, ADGROUP, CRITERIA, CUSTOM, REACH, SEARCH_QUERY),
    "AdId": _types(AD, CUSTOM, REACH, SEARCH_QUERY),
    "AdNetworkType": ALL_TYPES - {REACH, SEARCH_QUERY},
    "Age": ALL_TYPES - {SEARCH_QUERY},
    "AudienceTargetId": _types(CRITERIA, CUSTOM),
    "AvgClickPosition": ALL_TYPES - {REACH},
    "AvgCpc": ALL_TYPES,
    "AvgCpm": _types(REACH),
    "AvgEffectiveBid": ALL_TYPES,
    "AvgImpressionFrequency": _types(REACH),
    "AvgImpressionPosition": ALL_TYPES - {REACH},
    "AvgPageviews": ALL_TYPES,
    "AvgTrafficVolume": ALL_TYPES,
    "AvgVideoCompleteCost": _types(REACH),
    "BounceRate": ALL_TYPES,
    "Bounces": ALL_TYPES,
    "CampaignId": ALL_TYPES - {ACCOUNT},
    "CampaignName": ALL_TYPES - {ACCOUNT},
    "CampaignUrlPath": ALL_TYPES - {ACCOUNT},
    "CampaignType": ALL_TYPES,
    "CarrierType": ALL_TYPES - {REACH, SEARCH_QUERY},
    "Clicks": ALL_TYPES,
    "ClickType": ALL_TYPES - {REACH, SEARCH_QUERY},
    "ClientLogin": ALL_TYPES,
    "ConversionRate": ALL_TYPES,
    "Conversions": ALL_TYPES,
    "Cost": ALL_TYPES,
    "CostPerConversion": ALL_TYPES,
    "CPV": _types(REACH),
    "Criteria": _types(CRITERIA, CUSTOM, SEARCH_QUERY),
    "CriteriaId": _types(CRITERIA, CUSTOM, SEARCH_QUERY),
    "CriteriaType": ALL_TYPES - {REACH},
    "Criterion": _types(CRITERIA, CUSTOM, SEARCH_QUERY),
    "CriterionId": _types(CRITERIA, CUSTOM, SEARCH_QUERY),
    "CriterionType": ALL_TYPES - {REACH},
    "Ctr": ALL_TYPES,
    "Date": ALL_TYPES,
    "Device": ALL_TYPES - {SEARCH_QUERY},
    "DynamicTextAdTargetId": _types(CRITERIA, CUSTOM),
    "ExternalNetworkName": ALL_TYPES - {REACH, SEARCH_QUERY},
    "Gender": ALL_TYPES - {SEARCH_QUERY},
    "GoalsRoi": ALL_TYPES,
    "ImpressionReach": _types(REACH),
    "Impressions": ALL_TYPES,
    "IncomeGrade": ALL_TYPES - {REACH},
    "Keyword": _types(CRITERIA, CUSTOM, SEARCH_QUERY),
    "LocationOfPresenceId": ALL_TYPES - {REACH, SEARCH_QUERY},
    "LocationOfPresenceName": ALL_TYPES - {REACH, SEARCH_QUERY},
    "MatchedKeyword": _types(SEARCH_QUERY),
    "MatchType": ALL_TYPES - {REACH},
    "MobilePlatform": ALL_TYPES - {REACH, SEARCH_QUERY},
    "Month": ALL_TYPES,
    "Placement": ALL_TYPES - {REACH},
    "Profit": ALL_TYPES,
    "PurchaseGoals": ALL_TYPES,
    "PurchaseRevenue": ALL_TYPES,
    "PurchaseProfit": ALL_TYPES,
    "PurchaseGoalsRoi": ALL_TYPES,
    "Quarter": ALL_TYPES,
    "Query": _types(SEARCH_QUERY),
    "Revenue": ALL_TYPES,
    "RlAdjustmentId": _types(CRITERIA, CUSTOM),
    "Sessions": ALL_TYPES - {SEARCH_QUERY},
    "Slot": ALL_TYPES - {REACH, SEARCH_QUERY},
    "SmartAdTargetId": _types(CRITERIA, CUSTOM),
    "TargetingCategory": ALL_TYPES - {REACH},
    "TargetingLocationId": ALL_TYPES - {SEARCH_QUERY},
    "TargetingLocationName": ALL_TYPES - {SEARCH_QUERY},
    "VideoComplete": _types(REACH),
    "VideoCompleteRate": _types(REACH),
    "VideoFirstQuartile": _types(REACH),
    "VideoFirstQuartileRate": _types(REACH),
    "VideoMidpoint": _types(REACH),
    "VideoMidpointRate": _types(REACH),
    "VideoThirdQuartile": _types(REACH),
    "VideoThirdQuartileRate": _types(REACH),
    "VideoViews": _types(REACH),
    "VideoViewsRate": _types(REACH),
    "Week": ALL_TYPES,
    "WeightedCtr": ALL_TYPES,
    "WeightedImpressions": ALL_TYPES,
    "Year": ALL_TYPES,
}

FILTER_ONLY = frozenset({
    "AudienceTargetId",
    "DynamicTextAdTargetId",
    "Keyword",
    "PurchaseGoals",
    "SmartAdTargetId",
})
FIELD_NAMES = frozenset(FIELD_REPORT_TYPES) - FILTER_ONLY

# В официальной таблице эти поля разрешены в FieldNames, но не в Filter.Field.
NOT_FILTERABLE = frozenset({
    "AdGroupName",
    "Bounces",
    "CampaignName",
    "CampaignUrlPath",
    "Criteria",
    "CriteriaId",
    "Criterion",
    "CriterionId",
    "Date",
    "LocationOfPresenceName",
    "Month",
    "Quarter",
    "Sessions",
    "TargetingLocationName",
    "Week",
    "Year",
})
FILTER_FIELDS = frozenset(FIELD_REPORT_TYPES) - NOT_FILTERABLE

# Criteria/CriteriaId и Criterion/CriterionId нельзя использовать в OrderBy.
ORDER_FIELDS = FIELD_NAMES - {"Criteria", "CriteriaId", "Criterion", "CriterionId"}

ATTRIBUTION_MODELS = frozenset({"FCCD", "LC", "LSCCD", "AUTO"})
FILTER_OPERATORS = frozenset({
    "EQUALS",
    "NOT_EQUALS",
    "IN",
    "NOT_IN",
    "LESS_THAN",
    "GREATER_THAN",
    "STARTS_WITH_IGNORE_CASE",
    "DOES_NOT_START_WITH_IGNORE_CASE",
    "STARTS_WITH_ANY_IGNORE_CASE",
    "DOES_NOT_START_WITH_ALL_IGNORE_CASE",
})
SORT_ORDERS = frozenset({"ASCENDING", "DESCENDING"})
TIME_FIELDS = frozenset({"Date", "Week", "Month", "Quarter", "Year"})
CLICK_TYPE_INCOMPATIBLE = frozenset({
    "Impressions",
    "Ctr",
    "AvgImpressionPosition",
    "WeightedImpressions",
    "WeightedCtr",
    "AvgTrafficVolume",
})
CRITERIA_FAMILY = frozenset({"Criteria", "CriteriaId", "CriteriaType"})
CRITERION_FAMILY = frozenset({"Criterion", "CriterionId", "CriterionType"})
EXCLUSIVE_FILTER_FIELDS = frozenset({
    "CriterionType",
    "CriteriaType",
    "AudienceTargetId",
    "DynamicTextAdTargetId",
    "Keyword",
    "SmartAdTargetId",
})


def _unknown_keys(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{field} содержит неизвестные поля: {', '.join(unknown)}")


def _validate_type(field: str, report_type: str, location: str) -> None:
    if field not in FIELD_REPORT_TYPES:
        raise ValueError(f"{location}: неизвестное поле Reports API {field!r}")
    if report_type not in FIELD_REPORT_TYPES[field]:
        raise ValueError(
            f"{location}: поле {field!r} недоступно для report_type={report_type}"
        )


def normalize(
    *,
    fields: list[str],
    report_type: str,
    goals: list[str] | None,
    attribution_models: list[str] | None,
    filters: list[dict[str, Any]] | None,
    order_by: list[dict[str, Any]] | None,
    limit: int | None,
) -> dict[str, Any]:
    """Проверить параметры и вернуть нормализованные значения для спецификации."""
    normalized_type = str(report_type).strip().upper()
    if normalized_type not in REPORT_TYPES:
        raise ValueError(
            "Неизвестный report_type. Доступны: " + ", ".join(sorted(REPORT_TYPES))
        )
    if not isinstance(fields, list) or not fields:
        raise ValueError("fields пустой — укажите хотя бы одну колонку")
    normalized_fields = []
    for index, value in enumerate(fields):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"fields[{index}] должен быть непустой строкой")
        field = value.strip()
        if field not in FIELD_NAMES:
            if field in FILTER_ONLY:
                raise ValueError(f"fields[{index}]: {field!r} доступно только в filters")
            raise ValueError(f"fields[{index}]: неизвестное поле Reports API {field!r}")
        _validate_type(field, normalized_type, f"fields[{index}]")
        normalized_fields.append(field)
    if len(set(normalized_fields)) != len(normalized_fields):
        raise ValueError("fields содержит повторяющиеся поля")
    if normalized_type == REACH and "CampaignId" not in normalized_fields:
        raise ValueError("Для REACH_AND_FREQUENCY_PERFORMANCE_REPORT обязателен CampaignId")

    time_fields = TIME_FIELDS.intersection(normalized_fields)
    if len(time_fields) > 1:
        raise ValueError(
            "Date, Week, Month, Quarter и Year взаимоисключающие; оставьте одно поле"
        )
    field_set = set(normalized_fields)
    if "ClickType" in field_set and CLICK_TYPE_INCOMPATIBLE.intersection(field_set):
        raise ValueError(
            "ClickType несовместим с полями: "
            + ", ".join(sorted(CLICK_TYPE_INCOMPATIBLE.intersection(field_set)))
        )
    if CRITERIA_FAMILY.intersection(field_set) and CRITERION_FAMILY.intersection(field_set):
        raise ValueError("Поля Criteria* несовместимы с полями Criterion*")

    normalized_goals: list[str] | None = None
    if goals is not None:
        if not isinstance(goals, list) or not 1 <= len(goals) <= 10:
            raise ValueError("goals должен содержать от 1 до 10 ID")
        normalized_goals = []
        for index, value in enumerate(goals):
            goal = str(value).strip()
            if not goal.isdigit() or int(goal) <= 0:
                raise ValueError(f"goals[{index}] должен быть положительным ID")
            normalized_goals.append(goal)
        if len(set(normalized_goals)) != len(normalized_goals):
            raise ValueError("goals содержит повторяющиеся ID")

    normalized_models: list[str] | None = None
    if attribution_models is not None:
        if normalized_goals is None:
            raise ValueError("attribution_models можно указывать только вместе с goals")
        if not isinstance(attribution_models, list) or not attribution_models:
            raise ValueError("attribution_models должен быть непустым массивом")
        normalized_models = [str(value).strip().upper() for value in attribution_models]
        unknown = sorted(set(normalized_models) - ATTRIBUTION_MODELS)
        if unknown:
            raise ValueError(
                "Устаревшие или неизвестные attribution_models: "
                + ", ".join(unknown)
                + ". Используйте FCCD, LC, LSCCD или AUTO"
            )
        if len(set(normalized_models)) != len(normalized_models):
            raise ValueError("attribution_models содержит повторяющиеся значения")

    normalized_filters: list[dict[str, Any]] | None = None
    if filters is not None:
        if not isinstance(filters, list) or not filters:
            raise ValueError("filters должен быть непустым массивом")
        normalized_filters = []
        seen_filter_fields: set[str] = set()
        for index, raw in enumerate(filters):
            if not isinstance(raw, dict):
                raise ValueError(f"filters[{index}] должен быть объектом")
            _unknown_keys(raw, {"Field", "Operator", "Values"}, f"filters[{index}]")
            field = str(raw.get("Field") or "").strip()
            if field not in FILTER_FIELDS:
                raise ValueError(f"filters[{index}].Field: поле {field!r} нельзя фильтровать")
            _validate_type(field, normalized_type, f"filters[{index}].Field")
            if field in seen_filter_fields:
                raise ValueError(f"Поле {field!r} можно использовать только в одном фильтре")
            seen_filter_fields.add(field)
            operator = str(raw.get("Operator") or "").strip().upper()
            if operator not in FILTER_OPERATORS:
                raise ValueError(f"filters[{index}].Operator содержит неизвестный оператор")
            values = raw.get("Values")
            if not isinstance(values, list) or not 1 <= len(values) <= 10_000:
                raise ValueError(f"filters[{index}].Values должен содержать 1–10000 значений")
            normalized_filters.append({
                "Field": field,
                "Operator": operator,
                "Values": [str(value) for value in values],
            })
        exclusive = EXCLUSIVE_FILTER_FIELDS.intersection(seen_filter_fields)
        if len(exclusive) > 1:
            raise ValueError(
                "В одном отчёте взаимоисключающие поля фильтра: "
                + ", ".join(sorted(exclusive))
            )

    normalized_order: list[dict[str, Any]] | None = None
    if order_by is not None:
        if not isinstance(order_by, list) or not order_by:
            raise ValueError("order_by должен быть непустым массивом")
        normalized_order = []
        for index, raw in enumerate(order_by):
            if not isinstance(raw, dict):
                raise ValueError(f"order_by[{index}] должен быть объектом")
            _unknown_keys(raw, {"Field", "SortOrder"}, f"order_by[{index}]")
            field = str(raw.get("Field") or "").strip()
            if field not in ORDER_FIELDS:
                raise ValueError(f"order_by[{index}].Field: по {field!r} нельзя сортировать")
            _validate_type(field, normalized_type, f"order_by[{index}].Field")
            if field not in field_set:
                raise ValueError(f"order_by[{index}].Field {field!r} отсутствует в fields")
            item: dict[str, Any] = {"Field": field}
            if raw.get("SortOrder") is not None:
                sort_order = str(raw["SortOrder"]).strip().upper()
                if sort_order not in SORT_ORDERS:
                    raise ValueError(
                        f"order_by[{index}].SortOrder: используйте ASCENDING или DESCENDING"
                    )
                item["SortOrder"] = sort_order
            normalized_order.append(item)

    normalized_limit: int | None = None
    if limit is not None:
        if isinstance(limit, bool):
            raise ValueError("limit должен быть целым числом")
        try:
            normalized_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("limit должен быть целым числом") from exc
        if not 1 <= normalized_limit <= 1_000_000:
            raise ValueError("limit должен быть от 1 до 1000000")

    return {
        "fields": normalized_fields,
        "report_type": normalized_type,
        "goals": normalized_goals,
        "attribution_models": normalized_models,
        "filters": normalized_filters,
        "order_by": normalized_order,
        "limit": normalized_limit,
    }
