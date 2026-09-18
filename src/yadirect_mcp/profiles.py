"""Versioned business profiles; evidence is caller-declared, never implied API proof."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .identifiers import parse_id

BUSINESSES = {
    "services_b2b": {
        "label": "Услуги / B2B",
        "goal_kinds": ["qualified_lead", "lead", "order"],
        "primary_kpi": "qualified_lead_cost",
        "maps_required": False,
        "semantic_profile": "small_business",
        "recommendations": [
            "Точная семантика по услуге и намерению покупателя",
            "Расширение РСЯ проверять отдельной гипотезой",
        ],
    },
    "local_business": {
        "label": "Локальный бизнес",
        "goal_kinds": ["phone_call", "appointment", "order"],
        "primary_kpi": "appointment_or_call_cost",
        "maps_required": True,
        "semantic_profile": "small_business",
        "recommendations": [
            "Фактические звонки и записи, не клики по телефону",
            "География и контакты действующего офиса",
        ],
    },
    "ecommerce": {
        "label": "E-commerce",
        "goal_kinds": ["purchase"],
        "primary_kpi": "purchase_cost_and_revenue",
        "maps_required": False,
        "semantic_profile": "catalogue",
        "recommendations": [
            "Группы по товарным категориям и посадочным",
            "Корзина и начало оплаты не подменяют покупку",
            "Фиды, Shopping/Listing и CRR требуют расширения компилятора",
        ],
    },
}


def build_policies(base: dict) -> dict[str, dict]:
    result = {}
    for business, spec in BUSINESSES.items():
        for stage in ("new", "established"):
            name = f"{business}_{stage}_v1"
            selected = deepcopy(base)
            selected.update(name=name, version="1.0.0")
            selected["profile"] = {
                **deepcopy(spec),
                "business": business,
                "stage": stage,
                "base_name": base["name"],
                "base_version": base["version"],
                "history_min_days": 7,
                "history_max_age_days": 30,
                "auto_conversion_min_per_week": 10,
                "thresholds_are_agency_policy": True,
                "evidence_verification": "caller_declared_review_not_independent_api_proof",
                "strategy_selection": "per_campaign_channel_regions_goal",
            }
            selected["budget"].update(range_enforcement="client_total", minimum=None, maximum=None)
            selected["network_exclusions"] = "explicit_with_reason"
            selected["campaign_structure"]["conditional_channel"] = (
                "maps_required" if spec["maps_required"] else "maps_optional_if_office"
            )
            selected["campaign_structure"]["default_profile"] = spec["semantic_profile"]
            if business == "local_business":
                selected["campaign_structure"]["profiles"]["small_business"] = {
                    "search": {"groups": [1, 5], "keywords_per_group": [5, 20]},
                    "network": {"groups": [1, 3], "keywords_per_group": [5, 15]},
                }
            selected["tracking_profiles"] = {"utm_v1": selected["tracking_profiles"]["utm_v1"]}
            result[name] = selected
    for stage in ("new", "established"):
        name = f"ecommerce_{stage}_v2"
        selected = deepcopy(result[f"ecommerce_{stage}_v1"])
        selected.update(name=name, version="2.0.0")
        selected["profile"]["product_formats"] = ["ShoppingAd", "ListingAd"]
        selected["profile"]["recommendations"][-1] = (
            "Товарная ЕПК (галерея + РСЯ) и отдельный Поиск; источник — фид или сайт")
        selected["campaign_structure"]["product_channel"] = "gallery_and_network_shared_budget"
        result[name] = selected
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"profile_context.{field}: нужна непустая строка")
    return value.strip()


def _object(value: Any, fields: set[str], field: str) -> dict:
    if not isinstance(value, dict) or value.keys() - fields:
        raise ValueError(f"profile_context.{field}: неверный объект или неизвестные поля")
    return deepcopy(value)


def _regions(values: list) -> list[int]:
    result = []
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, (str, int))
            or not re.fullmatch(r"-?[0-9]+", str(value))
            or abs(int(value)) > 10**12
        ):
            raise ValueError("profile_context.history.region_ids: неверный ID региона")
        result.append(int(value))
    result = sorted(set(result))
    if (0 in result and len(result) > 1) or all(i < 0 for i in result):
        raise ValueError("profile_context.history.region_ids: неверное сочетание регионов")
    return result


def context(
    raw: Any, selected: dict, *, login: str, goals: set[int], counters: set[int], today: date
) -> dict:
    spec = selected["profile"]
    value = _object(
        raw,
        {
            "measurement_verified",
            "goals",
            "history",
            "catalog_reviewed",
            "revenue_verified",
            "exclusions_reason",
            "schedule_reason",
            "autotexts_reason",
        },
        "",
    )
    for key in ("measurement_verified", "catalog_reviewed", "revenue_verified"):
        value.setdefault(key, False)
        if type(value[key]) is not bool:
            raise ValueError(f"profile_context.{key}: требуется boolean")
    for key in ("exclusions_reason", "schedule_reason", "autotexts_reason"):
        if key in value:
            value[key] = _text(value[key], key)
    if spec["business"] == "ecommerce" and not value["catalog_reviewed"]:
        raise ValueError("profile_context.catalog_reviewed: проверьте товарные посадочные")
    rows = value.setdefault("goals", [])
    if not isinstance(rows, list):
        raise ValueError("profile_context.goals: нужен массив")
    declared = []
    for row in rows:
        row = _object(row, {"goal_id", "kind"}, "goals")
        identifier = parse_id(row.get("goal_id"), "profile_context.goals.goal_id")
        if row.get("kind") not in spec["goal_kinds"]:
            raise ValueError("profile_context.goals.kind не соответствует бизнес-профилю")
        declared.append({"goal_id": identifier, "kind": row["kind"]})
    ids = [row["goal_id"] for row in declared]
    if len(ids) != len(set(ids)) or set(ids) != goals or goals & {12, 13}:
        raise ValueError("profile_context.goals: классифицируйте все реальные priority_goals")
    value["goals"] = sorted(declared, key=lambda row: row["goal_id"])
    if value["measurement_verified"] and (not goals or not counters):
        raise ValueError("profile_context.measurement_verified требует счётчик и бизнес-цели")
    history = value.setdefault("history", [])
    if not isinstance(history, list):
        raise ValueError("profile_context.history: нужен массив")
    if spec["stage"] == "new" and history:
        raise ValueError("При наличии истории выберите профиль established")
    if spec["stage"] == "established" and not history:
        raise ValueError("Профиль established требует историю; без истории выберите new")
    normalized = []
    seen = set()
    fields = {
        "client_login",
        "channel",
        "region_ids",
        "goal_id",
        "counter_id",
        "date_from",
        "date_to",
        "conversions",
        "source",
        "complete",
        "reviewed",
        "reason",
    }
    for row in history:
        row = _object(row, fields, "history")
        if set(row) != fields:
            raise ValueError("profile_context.history: заполните все поля источника статистики")
        channels = {"search", "network", "maps"}
        if spec.get("product_formats"):
            channels.add("product")
        if row["client_login"] != login or row["channel"] not in channels:
            raise ValueError("profile_context.history: другой логин или неизвестный канал")
        regions = row["region_ids"]
        if not isinstance(regions, list) or not regions:
            raise ValueError("profile_context.history.region_ids: нужна география данных")
        row["region_ids"] = _regions(regions)
        row["goal_id"] = parse_id(row["goal_id"], "history.goal_id")
        row["counter_id"] = parse_id(row["counter_id"], "history.counter_id")
        if row["goal_id"] not in goals or row["counter_id"] not in counters:
            raise ValueError("profile_context.history: цель/счётчик отсутствует в плане")
        try:
            start, end = date.fromisoformat(row["date_from"]), date.fromisoformat(row["date_to"])
        except (ValueError, TypeError) as exc:
            raise ValueError("profile_context.history: даты нужны в ISO-формате") from exc
        if not start <= end <= today:
            raise ValueError("profile_context.history: неверный период или дата в будущем")
        try:
            count = Decimal(str(row["conversions"]))
        except InvalidOperation as exc:
            raise ValueError("profile_context.history.conversions: требуется число") from exc
        if not count.is_finite() or not 0 <= count <= 10**12:
            raise ValueError("profile_context.history.conversions: неверное число")
        row["conversions"] = str(count)
        for key in ("complete", "reviewed"):
            if type(row[key]) is not bool:
                raise ValueError(f"profile_context.history.{key}: требуется boolean")
        for key in ("source", "reason"):
            row[key] = _text(row[key], f"history.{key}")
        identity = (row["channel"], tuple(row["region_ids"]), row["goal_id"])
        if identity in seen:
            raise ValueError("profile_context.history: неоднозначные срезы одной цели и географии")
        seen.add(identity)
        normalized.append(row)
    value["history"] = sorted(
        normalized, key=lambda row: (row["channel"], row["region_ids"], row["goal_id"])
    )
    return value


def eligible_goals(
    value: dict, spec: dict, channel: str, regions: list[int], today: date
) -> set[int]:
    if not value["measurement_verified"] or spec["stage"] != "established":
        return set()
    result = set()
    for row in value["history"]:
        start, end = date.fromisoformat(row["date_from"]), date.fromisoformat(row["date_to"])
        days = (end - start).days + 1
        if (
            row["channel"] == channel
            and row["region_ids"] == sorted(set(regions))
            and row["complete"]
            and row["reviewed"]
            and days >= spec["history_min_days"]
            and 0 <= (today - end).days <= spec["history_max_age_days"]
            and Decimal(row["conversions"]) * 7 >= spec["auto_conversion_min_per_week"] * days
        ):
            result.add(row["goal_id"])
    return result


def strategy(
    source: Any, value: dict, selected: dict, *, channel: str, regions: list[int], today: date
) -> tuple[Any, dict]:
    eligible = eligible_goals(value, selected["profile"], channel, regions, today)
    goals = {row["goal_id"] for row in value["goals"]}
    explicit = source is not None
    if not explicit:
        source = (
            {"type": "maximum_conversion_rate", "goal_id": next(iter(goals))}
            if len(goals) == 1 and goals <= eligible
            else {"type": "maximum_clicks"}
        )
    source = {"type": source} if isinstance(source, str) else deepcopy(source)
    if not isinstance(source, dict):
        raise ValueError("strategy: требуется объект или имя стратегии")
    kind = str(source.get("type") or "maximum_clicks").lower().removeprefix("wb_")
    if kind == "maximum_conversion_rate":
        goal = parse_id(source.get("goal_id"), "strategy.goal_id")
        required = goals if goal == 13 else {goal}
        if not required or not required <= eligible:
            raise ValueError(
                "profile.history: для конверсионной стратегии нет проверенной "
                "достаточной истории этой цели, канала и географии"
            )
    return source, {
        "channel": channel,
        "region_ids": sorted(set(regions)),
        "selection": "explicit" if explicit else "profile_default",
        "strategy": deepcopy(source),
        "eligible_goal_ids": sorted(eligible),
        "reason": (
            "Явный выбор стратегии"
            if explicit
            else "Проверенная история цели, канала и географии"
            if kind == "maximum_conversion_rate"
            else "Нет достаточной истории одной выбранной цели для этого среза"
        ),
        "evidence_status": "caller_declared_review",
    }


def validate_campaigns(
    campaigns: list[dict],
    value: dict,
    selected: dict,
    *,
    client_budget: dict | None,
    office: bool,
    today: date,
    allow_unverified_goals: bool,
) -> None:
    spec = selected["profile"]
    if client_budget is None:
        raise ValueError("Для бизнес-профиля обязателен общий client_budget")
    if spec["maps_required"] and (not office or not any(c["channel"] == "maps" for c in campaigns)):
        raise ValueError("local_business требует офис, business_profiles и канал maps")
    for item in campaigns:
        campaign = item["campaign"]
        unified = campaign["UnifiedCampaign"]
        expected_goals = {row["goal_id"] for row in value["goals"]}
        actual_goals = {row["GoalId"] for row in unified.get("PriorityGoals", {}).get("Items", [])}
        if actual_goals != expected_goals:
            raise ValueError("Цели кампании не соответствуют profile_context.goals")
        if campaign.get("ExcludedSites", {}).get("Items") and not value.get("exclusions_reason"):
            raise ValueError("profile_context.exclusions_reason: обоснуйте исключения площадок")
        if campaign.get("TimeTargeting") and not value.get("schedule_reason"):
            raise ValueError("profile_context.schedule_reason: обоснуйте ограничение расписания")
        settings = {r["Option"]: r["Value"] for r in unified.get("Settings", [])}
        if settings.get("ALTERNATIVE_TEXTS_ENABLED") == "YES" and not value.get("autotexts_reason"):
            raise ValueError("profile_context.autotexts_reason: обоснуйте включение автотекстов")
        if item["channel"] == "maps" and any(
            not ad["ResponsiveAd"].get("BusinessId")
            for group in item["groups"]
            for ad in group["ads"]
        ):
            raise ValueError("Карты в бизнес-профиле требуют BusinessId каждого объявления")
        regions = sorted({i for g in item["groups"] for i in g["ad_group"]["RegionIds"]})
        for branch in unified["BiddingStrategy"].values():
            if branch.get("BiddingStrategyType") == "WB_MAXIMUM_CONVERSION_RATE":
                if allow_unverified_goals:
                    raise ValueError(
                        "Конверсионный профиль требует live-каталог целей; "
                        "allow_unverified_goals должен быть false"
                    )
                strategy(
                    {
                        "type": "maximum_conversion_rate",
                        "goal_id": branch["WbMaximumConversionRate"]["GoalId"],
                    },
                    value,
                    selected,
                    channel=item["channel"],
                    regions=regions,
                    today=today,
                )
