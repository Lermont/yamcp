"""Read all referenced assets and real currency constraints before writes."""

from __future__ import annotations

from typing import Any

from . import launch_checks, policy


async def _references(
    api,
    login,
    service,
    collection,
    ids,
    *,
    key="Id",
    fields=None,
    selection="Ids",
    batch_size=10000,
    page_limit=10000,
    filters=None,
):
    wanted = set(ids)
    found = []
    for start in range(0, len(wanted), batch_size):
        chunk = sorted(wanted)[start : start + batch_size]
        result = await api.call_v501(
            service,
            "get",
            {
                "SelectionCriteria": {selection: chunk, **(filters or {})},
                "FieldNames": fields or [key],
                "Page": {"Limit": page_limit},
            },
            client_login=login,
        )
        if result.get("LimitedBy") is not None:
            raise ValueError(f"{service}: неполный список дополнений")
        found.extend(result.get(collection, []))
    if {row[key] for row in found} != wanted:
        raise ValueError(f"{service}: часть дополнений отсутствует в кабинете {login}")
    return found


async def assets(
    api, login: str, ads: list[dict], *, business_profiles: list[dict] | None = None,
) -> dict:
    images = {h for ad in ads for h in ad.get("AdImageHashes", [])}
    extensions = {v for ad in ads for v in ad.get("AdExtensionIds", [])}
    videos = {v for ad in ads for v in ad.get("VideoExtensionIds", [])}
    businesses = {ad["BusinessId"] for ad in ads if ad.get("BusinessId")}
    await _references(
        api,
        login,
        "adimages",
        "AdImages",
        images,
        key="AdImageHash",
        selection="AdImageHashes",
        fields=["AdImageHash", "Type"],
    )
    callouts = await _references(
        api,
        login,
        "adextensions",
        "AdExtensions",
        extensions,
        fields=["Id", "Type", "Status"],
        filters={"States": ["ON"]},
    )
    if any(row.get("Status") in {"REJECTED", "UNKNOWN"} for row in callouts):
        raise ValueError("Уточнение отклонено модерацией или имеет неизвестный статус")
    creatives = await _references(
        api, login, "creatives", "Creatives", videos, fields=["Id", "Type"]
    )
    if any(row.get("Type") != "VIDEO_EXTENSION_CREATIVE" for row in creatives):
        raise ValueError("Выбранный CreativeId не является видеодополнением")
    business_rows = await _references(
        api,
        login,
        "businesses",
        "Businesses",
        businesses,
        fields=["Id", "Name", "IsPublished", "Phone", "Address", "HasOffice"],
        batch_size=1000,
        page_limit=1000,
    )
    if any(row.get("IsPublished") != "YES" for row in business_rows):
        raise ValueError("Профиль организации не опубликован и не может быть привязан")
    contacts = (launch_checks.check_businesses(business_profiles, business_rows)
                if business_profiles is not None else {"status": "not_configured"})
    return {
        "status": policy.PASS,
        "business_contacts": contacts,
        "images": len(images),
        "callouts": len(extensions),
        "videos": len(videos),
        "businesses": business_rows,
        "business_scope": "available_ad_profile_not_campaign_binding",
    }


def _weekly_limits(value: Any):
    if isinstance(value, dict):
        for key, val in value.items():
            if key == "WeeklySpendLimit":
                yield int(val)
            else:
                yield from _weekly_limits(val)
    elif isinstance(value, list):
        for val in value:
            yield from _weekly_limits(val)


async def currency(api, plan: dict) -> dict:
    login = plan["client_login"]
    clients = await api.call(
        "clients", "get", {"FieldNames": ["Login", "Currency"]}, client_login=login
    )
    rows = clients.get("Clients", [])
    if len(rows) != 1 or not rows[0].get("Currency"):
        raise ValueError("Не удалось однозначно определить валюту клиента")
    if rows[0].get("Login", "").casefold() != login.casefold():
        raise ValueError("Clients.get вернул другого рекламодателя")
    code = rows[0]["Currency"]
    budget_check = launch_checks.check_budget(plan.get("client_budget"), plan["campaigns"], code)
    dictionary = await api.call(
        "dictionaries", "get", {"DictionaryNames": ["Currencies"]}, client_login=login
    )
    found = next((r for r in dictionary.get("Currencies", []) if r["Currency"] == code), None)
    props = {r["Name"]: r["Value"] for r in (found or {}).get("Properties", [])}
    if "MinimumWeeklySpendLimit" not in props:
        raise ValueError(f"Не найден MinimumWeeklySpendLimit для валюты {code}")
    minimum = int(props["MinimumWeeklySpendLimit"])
    maximum = int(props["MaxAutobudget"]) if "MaxAutobudget" in props else None
    budgets = list(_weekly_limits(plan["campaigns"]))
    if not budgets or any(v < minimum or (maximum is not None and v > maximum) for v in budgets):
        raise ValueError(
            f"Недельный бюджет вне ограничений валюты {code}: минимум {minimum / 1_000_000:g}"
        )
    return {
        "status": policy.PASS,
        "currency": code,
        "client_budget": budget_check,
        "minimum_weekly_micros": minimum,
        "budgets_micros": budgets,
        "agency_range_currency": "RUB",
        "agency_range_applies": code == "RUB",
    }
