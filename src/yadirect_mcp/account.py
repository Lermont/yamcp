"""Настройки кабинета, которых нет в отчётах.

Плейбук донастройки велит проверять корректировки ставок и ретаргетинг, а
Reports API их не отдаёт: отчёт покажет статистику в разрезе Device, Gender и
Age, но не коэффициент, который на самом деле выставлен. Разница принципиальна.
«Мобильных конверсий нет» и «на мобильные стоит −100%» — это одно и то же в
отчёте и совершенно разные диагнозы в жизни. Так же и с минус-фразами: общий
набор применён ко всем группам, но ни в одном отчёте не виден.

Три сервиса в одном туле, потому что читаются они в один момент — при разборе
«почему кампания не работает» — и по отдельности не нужны. Отдельные тулы на
каждый `get` раздували бы список инструментов, который едет в контекст модели
на каждом запросе.

Секции изолированы друг от друга: нет доступа к ретаргетингу — это не повод
потерять корректировки, поэтому ошибка секции остаётся внутри секции.
"""

from __future__ import annotations

from typing import Any

SECTIONS = ("bid_modifiers", "retargeting_lists", "negative_keyword_sets")

# BidModifiers.get принимает не более 10 CampaignIds за вызов — режем на пачки.
CAMPAIGNS_PER_CALL = 10
# Потолок автоподбора, когда campaign_ids не передан: 50 кампаний — это 5
# вызовов, дальше растёт расход баллов на ровном месте.
MAX_AUTO_CAMPAIGNS = 50
# NegativeKeywordSharedSets.get: в наборе бывают тысячи фраз, целиком в контекст
# они не нужны — отдаём количество и начало списка.
KEYWORDS_PREVIEW = 50

_MODIFIER_FIELDS = ["Id", "CampaignId", "AdGroupId", "Level", "Type"]

# Коэффициент лежит не в самом объекте, а во вложенном по типу корректировки,
# и каждый такой блок запрашивается своим параметром. Не запросить блок —
# получить корректировку без единственного значащего числа.
_MODIFIER_NESTED: dict[str, list[str]] = {
    "MobileAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
    "TabletAdjustmentFieldNames": ["BidModifier", "OperatingSystemType"],
    "DesktopAdjustmentFieldNames": ["BidModifier"],
    "DesktopOnlyAdjustmentFieldNames": ["BidModifier"],
    "DemographicsAdjustmentFieldNames": ["Gender", "Age", "BidModifier", "Enabled"],
    "RetargetingAdjustmentFieldNames": [
        "RetargetingConditionId",
        "BidModifier",
        "Enabled",
    ],
    "RegionalAdjustmentFieldNames": ["RegionId", "BidModifier", "Enabled"],
    "VideoAdjustmentFieldNames": ["BidModifier"],
    "SmartAdAdjustmentFieldNames": ["BidModifier"],
    "SerpLayoutAdjustmentFieldNames": ["SerpLayout", "BidModifier", "Enabled"],
    "IncomeGradeAdjustmentFieldNames": ["Grade", "BidModifier", "Enabled"],
    "AdGroupAdjustmentFieldNames": ["BidModifier"],
}


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _shape_modifier(item: dict[str, Any]) -> dict[str, Any]:
    """Плоский вид корректировки: тип, коэффициент, детали.

    Вложенный блок ищем по суффиксу имени, а не по списку известных типов:
    Директ добавляет типы корректировок регулярно, и неизвестный тип должен
    доехать до модели как есть, а не превратиться в null.
    """
    out: dict[str, Any] = {
        "id": item.get("Id"),
        "level": item.get("Level"),
        "type": item.get("Type"),
        "campaign_id": item.get("CampaignId"),
        "ad_group_id": item.get("AdGroupId"),
    }
    for key, value in item.items():
        if not key.endswith("Adjustment") or not isinstance(value, dict):
            continue
        bid_modifier = value.get("BidModifier")
        out["bid_modifier"] = bid_modifier
        out["percent"] = bid_modifier  # backward-compatible raw API multiplier
        if isinstance(bid_modifier, (int, float)):
            out["adjustment_percent"] = bid_modifier - 100
            out["effect"] = "excluded" if bid_modifier == 0 else "adjusted"
        if "Enabled" in value:
            out["enabled"] = value.get("Enabled")
            if value.get("Enabled") == "NO":
                out["effect"] = "disabled"
        details = {
            k: v for k, v in value.items() if k not in {"BidModifier", "Enabled"}
        }
        if details:
            out["details"] = details
    return out


async def _campaign_ids(api: Any, client_login: str) -> tuple[list[int], int]:
    """ID кампаний для отбора корректировок, когда их не передали явно."""
    result = await api.call(
        "campaigns",
        "get",
        {
            "SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED", "ENDED"]},
            "FieldNames": ["Id"],
            "Page": {"Limit": 10000},
        },
        client_login=client_login,
    )
    ids = [c["Id"] for c in result.get("Campaigns", []) if c.get("Id") is not None]
    return ids[:MAX_AUTO_CAMPAIGNS], len(ids)


async def _bid_modifiers(
    api: Any, client_login: str, campaign_ids: list[int] | None
) -> dict[str, Any]:
    total = None
    if campaign_ids:
        ids = list(campaign_ids)
        scanned_source = "передан явно"
    else:
        ids, total = await _campaign_ids(api, client_login)
        scanned_source = "все неархивные кампании"

    if not ids:
        return {"items": [], "count": 0, "campaigns_scanned": 0,
                "note": "У клиента нет кампаний, к которым применимы корректировки."}

    params_base: dict[str, Any] = {"FieldNames": _MODIFIER_FIELDS, **_MODIFIER_NESTED}
    items: list[dict[str, Any]] = []
    limited: dict[str, Any] = {}
    for chunk in _chunks(ids, CAMPAIGNS_PER_CALL):
        result = await api.call(
            "bidmodifiers",
            "get",
            {
                "SelectionCriteria": {
                    "CampaignIds": chunk,
                    "Levels": ["CAMPAIGN", "AD_GROUP"],
                },
                **params_base,
            },
            client_login=client_login,
        )
        items.extend(_shape_modifier(m) for m in result.get("BidModifiers", []))
        _limited(result, limited)

    section: dict[str, Any] = {
        "items": items,
        "count": len(items),
        "campaigns_scanned": len(ids),
        "campaigns_source": scanned_source,
        **limited,
    }
    if total is not None and total > len(ids):
        section["campaigns_total"] = total
        section["truncated"] = True
        section["note"] = (
            f"Просмотрены первые {len(ids)} кампаний из {total}. "
            f"Для остальных передайте campaign_ids явно."
        )
    return section


def _limited(result: dict[str, Any], section: dict[str, Any]) -> dict[str, Any]:
    """Директ сам обрезает выдачу и сообщает об этом полем LimitedBy.

    Молча обрезанный список читается как полный, а «условия ретаргетинга есть,
    но не все» и «условий нет» ведут к разным решениям.
    """
    limited_by = result.get("LimitedBy")
    if limited_by is not None:
        section["truncated"] = True
        section["limited_by"] = limited_by
    return section


async def _retargeting_lists(api: Any, client_login: str) -> dict[str, Any]:
    # Page не передаём: он необязателен, метод и так отдаёт до 10 000 объектов,
    # а лишний параметр — лишний повод получить ошибку вызова в 20 баллов.
    result = await api.call(
        "retargetinglists",
        "get",
        {"FieldNames": ["Id", "Name", "Type", "IsAvailable", "Scope"]},
        client_login=client_login,
    )
    items = [
        {
            "id": r.get("Id"),
            "name": r.get("Name"),
            "type": r.get("Type"),
            "available": r.get("IsAvailable"),
            "scope": r.get("Scope"),
        }
        for r in result.get("RetargetingLists", [])
    ]
    return _limited(result, {"items": items, "count": len(items)})


async def _negative_keyword_sets(api: Any, client_login: str) -> dict[str, Any]:
    result = await api.call(
        "negativekeywordsharedsets",
        "get",
        {"FieldNames": ["Id", "Name", "NegativeKeywords", "Associated"]},
        client_login=client_login,
    )
    items = []
    for s in result.get("NegativeKeywordSharedSets", []):
        keywords = s.get("NegativeKeywords") or []
        item: dict[str, Any] = {
            "id": s.get("Id"),
            "name": s.get("Name"),
            "associated": s.get("Associated"),
            "keywords_count": len(keywords),
            "keywords": keywords[:KEYWORDS_PREVIEW],
        }
        if len(keywords) > KEYWORDS_PREVIEW:
            item["keywords_truncated"] = True
        items.append(item)
    return _limited(result, {"items": items, "count": len(items)})


async def read(
    api: Any,
    client_login: str,
    *,
    sections: list[str] | None = None,
    campaign_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Прочитать выбранные секции настроек кабинета."""
    wanted = list(sections) if sections else list(SECTIONS)
    unknown = [s for s in wanted if s not in SECTIONS]
    if unknown:
        raise ValueError(
            f"Неизвестные секции: {unknown}. Доступны: {', '.join(SECTIONS)}"
        )

    readers = {
        "bid_modifiers": lambda: _bid_modifiers(api, client_login, campaign_ids),
        "retargeting_lists": lambda: _retargeting_lists(api, client_login),
        "negative_keyword_sets": lambda: _negative_keyword_sets(api, client_login),
    }

    payload: dict[str, Any] = {"client_login": client_login}
    for name in wanted:
        try:
            payload[name] = await readers[name]()
        except Exception as exc:  # noqa: BLE001
            # Секция могла упасть на правах доступа или на пустом кабинете.
            # Остальные при этом уже прочитаны — терять их нельзя.
            payload[name] = {"error": str(exc)}
    return payload
