"""Pure checks for plan budgets, declared business contacts and strategy goals."""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from .identifiers import parse_id


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"{field}: требуется конечное число") from exc
    if not number.is_finite():
        raise ValueError(f"{field}: требуется конечное число")
    return number


def normalize_budget(raw: Any) -> dict | None:
    if raw is None:
        return None
    required = {"amount", "period", "currency", "includes_vat"}
    if not isinstance(raw, dict) or not required <= raw.keys() or (
        raw.keys() - required - {"vat_percent"}
    ):
        raise ValueError("client_budget: amount, period, currency, includes_vat; "
                         "vat_percent обязателен для суммы с НДС")
    amount = _decimal(raw["amount"], "client_budget.amount")
    if not 0 < amount <= Decimal(2**63 - 1) / 1_000_000 or (
        amount * 1_000_000 != (amount * 1_000_000).to_integral_value()
    ):
        raise ValueError("client_budget.amount: положительная сумма с точностью до микроединицы")
    period = raw["period"]
    if not isinstance(period, str) or period not in {"weekly", "monthly"}:
        raise ValueError("client_budget.period: weekly или monthly")
    currency = raw["currency"]
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("client_budget.currency: трёхбуквенный код валюты кабинета")
    if type(raw["includes_vat"]) is not bool:
        raise ValueError("client_budget.includes_vat: true или false")
    if raw["includes_vat"] and "vat_percent" not in raw:
        raise ValueError("client_budget.vat_percent: явно укажите ставку НДС")
    vat = _decimal(raw.get("vat_percent", 0), "client_budget.vat_percent")
    if not 0 <= vat <= 100 or (not raw["includes_vat"] and vat != 0):
        raise ValueError("client_budget.vat_percent: 0–100; без НДС допускается только 0")
    weekly = amount / (1 + vat / 100)
    if period == "monthly":
        weekly = weekly * 12 / 52
    micros = int((weekly * 1_000_000).to_integral_value(rounding=ROUND_FLOOR))
    if micros <= 0:
        raise ValueError("client_budget: недельный лимит меньше микроединицы")
    return {"amount": str(amount), "period": period, "currency": currency,
            "includes_vat": raw["includes_vat"], "vat_percent": str(vat),
            "scope": "planned_campaigns", "weekly_net_limit_micros": micros,
            "conversion": "monthly_times_12_divided_by_52" if period == "monthly" else "weekly",
            "calendar_month_spend_cap": False}


def weekly_total(campaigns: list[dict]) -> int:
    total = 0
    for item in campaigns:
        strategy = item.get("bidding_strategy")
        if strategy is None:
            strategy = item["campaign"]["UnifiedCampaign"]["BiddingStrategy"]
        for branch in strategy.values():
            if branch.get("BiddingStrategyType") == "NETWORK_DEFAULT":
                search = strategy.get("Search", {})
                if search.get("BiddingStrategyType") not in {
                        "WB_MAXIMUM_CLICKS", "WB_MAXIMUM_CONVERSION_RATE"}:
                    raise ValueError("NETWORK_DEFAULT требует бюджетной стратегии Search")
                if any(isinstance(v, dict) and "WeeklySpendLimit" in v for v in branch.values()):
                    raise ValueError("NETWORK_DEFAULT не должен иметь отдельный бюджет")
                continue
            if branch.get("BiddingStrategyType") == "SERVING_OFF":
                continue
            amounts = [value["WeeklySpendLimit"] for value in branch.values()
                       if isinstance(value, dict) and "WeeklySpendLimit" in value]
            if len(amounts) != 1 or type(amounts[0]) is not int or amounts[0] <= 0:
                raise ValueError("client_budget: нельзя достоверно определить недельный бюджет")
            total += amounts[0]
    return total


def check_budget(contract: dict | None, campaigns: list[dict], currency: str | None = None) -> dict:
    total = weekly_total(campaigns)
    if contract is None:
        return {"status": "not_configured", "scope": "planned_campaigns",
                "weekly_net_total_micros": total}
    # Recompute derived values; never trust a caller-supplied limit in a stored plan.
    source = {key: contract[key] for key in
              ("amount", "period", "currency", "includes_vat", "vat_percent")}
    expected = normalize_budget(source)
    if contract != expected:
        raise ValueError("client_budget: нарушена целостность бюджетного ограничения")
    if currency is not None and currency != contract["currency"]:
        raise ValueError("client_budget.currency не совпадает с валютой рекламодателя")
    if total > contract["weekly_net_limit_micros"]:
        raise ValueError("client_budget: сумма недельных бюджетов плана превышает общий лимит")
    return {**contract, "status": "PASS", "weekly_net_total_micros": total,
            "weekly_net_remaining_micros": contract["weekly_net_limit_micros"] - total}


def phone(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[+0-9() .\-]+", value):
        raise ValueError("business_profiles.phone: нужен один телефон без добавочного номера")
    digits = re.sub(r"[^0-9]", "", value)
    if not 7 <= len(digits) <= 15:
        raise ValueError("business_profiles.phone: от 7 до 15 цифр")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def address(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("business_profiles.address: непустой адрес или null")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_businesses(raw: Any, referenced: set[int]) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("business_profiles: требуется массив ожидаемых профилей организаций")
    result = []
    fields = {"business_id", "phone", "address", "has_office"}
    for row in raw:
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("business_profiles: нужны business_id, phone, address и has_office")
        identifier = parse_id(row["business_id"], "business_profiles.business_id")
        if type(row["has_office"]) is not bool:
            raise ValueError("business_profiles.has_office: true или false")
        normalized_address = address(row["address"])
        if row["has_office"] and normalized_address is None:
            raise ValueError("business_profiles.address обязателен для организации с офисом")
        result.append({"business_id": identifier, "phone": phone(row["phone"]),
                       "address": normalized_address, "has_office": row["has_office"]})
    ids = [row["business_id"] for row in result]
    if len(ids) != len(set(ids)) or set(ids) != referenced:
        raise ValueError("business_profiles: нужен ровно один профиль для каждого BusinessId плана")
    return sorted(result, key=lambda row: row["business_id"])


def check_businesses(expected: list[dict], actual: list[dict]) -> dict:
    ids = [row.get("Id") for row in actual]
    if len(ids) != len(set(ids)) or set(ids) != {row["business_id"] for row in expected}:
        raise ValueError("business_profiles: неполная или неоднозначная выборка организаций")
    by_id = {row["Id"]: row for row in actual}
    for wanted in expected:
        row = by_id[wanted["business_id"]]
        if row.get("IsPublished") != "YES":
            raise ValueError("business_profiles: организация не опубликована")
        if phone(row.get("Phone")) != wanted["phone"]:
            raise ValueError(f"business_profiles: телефон организации {row['Id']} не совпадает")
        if "Address" not in row or address(row["Address"]) != wanted["address"]:
            raise ValueError(f"business_profiles: адрес организации {row['Id']} не совпадает")
        if row.get("HasOffice") != ("YES" if wanted["has_office"] else "NO"):
            raise ValueError(f"business_profiles: HasOffice организации {row['Id']} не совпадает")
    return {"status": "PASS", "checked": len(expected), "profiles": actual}


def validate_conversion_goal(goal_id: int, priority_ids: set[int]) -> None:
    if 13 in priority_ids:
        raise ValueError("priority_goals: 13 — селектор ключевых целей, не отдельная цель")
    if goal_id == 13:
        if not priority_ids - {12}:
            raise ValueError("GoalId=13 требует бизнес-цель в priority_goals, отличную от 12")
    elif goal_id <= 0 or goal_id not in priority_ids:
        raise ValueError("strategy.goal_id должен входить в bundle.priority_goals")
