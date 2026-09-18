# ruff: noqa: E501
"""Client-facing HTML report listing objects created by a guarded run.

Technical plan-versus-readback comparison stays in the internal JSON audit and
is intentionally not rendered in this client document.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shlex
import subprocess
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from . import products, store

_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_SSH_HOST_RE = re.compile(r"[A-Za-z0-9_.@-]+")
_TYPE_NAMES = {
    "UNIFIED_CAMPAIGN": "Единая перфоманс-кампания",
    "UNIFIED_AD_GROUP": "Группа объявлений",
    "RESPONSIVE_AD": "Комбинаторное объявление",
    "SHOPPING_AD": "Товарное объявление",
    "LISTING_AD": "Объявление страницы каталога",
    "TEXT_AD": "Текстово-графическое объявление",
}

_CHANNEL_NAMES = {"search": "Поиск", "network": "РСЯ", "maps": "Карты",
                  "product": "Товарная галерея и РСЯ"}


def _login(value: Any) -> str:
    login = str(value or "").strip()
    if not _LOGIN_RE.fullmatch(login):
        raise ValueError("client_login содержит недопустимые символы для URL отчёта")
    return login
_STATUS_NAMES = {
    "OFF": "Выключена",
    "ON": "Включена",
    "DRAFT": "Черновик",
    "ACCEPTED": "Принято",
    "MODERATION": "На модерации",
    "REJECTED": "Отклонено модерацией",
    "SUSPENDED": "Приостановлена",
    "ENDED": "Завершена",
    "ELIGIBLE": "Может показываться",
    "RARELY_SERVED": "Показы ограничены",
}
_STRATEGY_NAMES = {
    "WB_MAXIMUM_CLICKS": "Максимум кликов с недельным бюджетом",
    "AVERAGE_CPC": "Средняя цена клика",
    "HIGHEST_POSITION": "Наивысшая доступная позиция",
    "SERVING_OFF": "Показы выключены",
}
_PLACEMENT_NAMES = {
    "SearchResults": "Результаты поиска Яндекса",
    "ProductGallery": "Товарная галерея",
    "DynamicPlaces": "Динамические места на поиске",
    "Maps": "Яндекс Карты",
    "SearchOrganizationList": "Список организаций",
}
_AUTOTARGETING_CATEGORY_NAMES = {
    "Exact": "Целевые запросы",
    "Narrow": "Узкие запросы",
    "Broader": "Широкие запросы",
    "Alternative": "Альтернативные запросы",
    "Accessory": "Сопутствующие запросы",
}
_AUTOTARGETING_BRAND_NAMES = {
    "WithoutBrands": "Без упоминания брендов",
    "WithAdvertiserBrand": "С упоминанием бренда рекламодателя",
    "WithCompetitorsBrand": "С упоминанием брендов конкурентов",
}



def public_url(client_login: str, base_url: str) -> str:
    login = _login(client_login)
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Публичный базовый URL должен быть HTTP(S)")
    return f"{base_url.rstrip('/')}/{quote(login, safe='._-')}/create/"


def _action_id(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    identifier = value.get("Id")
    return int(identifier) if isinstance(identifier, int) else None


def _indexed(rows: list[dict[str, Any]], key: str) -> dict[int, dict[str, Any]]:
    return {
        int(row[key]): row
        for row in rows
        if isinstance(row, dict) and row.get(key) is not None
    }


def _planned_ad(payload: dict[str, Any]) -> dict[str, Any]:
    source = products.payload(payload) or payload.get("TextAd") or {}
    titles = source.get("Titles") or [source.get("Title")]
    texts = source.get("DefaultTexts") or source.get("Texts") or [source.get("Text")]
    return {
        "type": products.READ_TYPES.get(products.kind(payload)) or (
            "RESPONSIVE_AD" if payload.get("ResponsiveAd") else "TEXT_AD"),
        "feed_id": source.get("FeedId"),
        "titles": [value for value in titles if value],
        "texts": [value for value in texts if value],
        "href": source.get("Href"),
        "display_url_path": source.get("DisplayUrlPath"),
        "business_id": source.get("BusinessId"),
        "sitelinks": source.get("SitelinkSetId") is not None,
        "image": bool(source.get("AdImageHashes") or source.get("AdImageHash")),
        "extensions": bool(source.get("AdExtensionIds")),
        "video": bool(source.get("VideoExtensionIds")),
    }


def build_model(
    plan: dict[str, Any],
    execution: dict[str, Any],
    readback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable presentation model without mutating execution artifacts."""
    login = _login(execution.get("client_login") or plan.get("client_login"))
    objects = (readback or {}).get("objects") or {}
    actual_campaigns = _indexed(objects.get("campaigns") or [], "id")
    actual_groups = _indexed(objects.get("ad_groups") or [], "id")
    actual_ads = _indexed(objects.get("ads") or [], "id")
    actual_keywords = _indexed(objects.get("keywords") or [], "Id")
    region_names = {
        int(row["id"]): row.get("name")
        for row in (execution.get("preflight") or {}).get("regions") or []
        if row.get("id") is not None
    }

    campaigns_out: list[dict[str, Any]] = []
    for executed_campaign in execution.get("campaigns") or []:
        plan_index = int(executed_campaign["plan_index"])
        planned = plan["campaigns"][plan_index]
        planned_campaign = planned["campaign"]
        unified = planned_campaign.get("UnifiedCampaign") or {}
        campaign_id = executed_campaign.get("id")
        actual_campaign = actual_campaigns.get(int(campaign_id)) if campaign_id else None
        campaign_source = actual_campaign or {
            "name": planned_campaign.get("Name"),
            "type": "UNIFIED_CAMPAIGN",
            "state": None,
            "status": None,
            "start_date": planned_campaign.get("StartDate"),
            "end_date": planned_campaign.get("EndDate"),
            "time_zone": planned_campaign.get("TimeZone"),
            "negative_keywords": (planned_campaign.get("NegativeKeywords") or {}).get(
                "Items", []
            ),
            "excluded_sites": (planned_campaign.get("ExcludedSites") or {}).get(
                "Items", []
            ),
            "settings": {
                row.get("Option"): row.get("Value")
                for row in unified.get("Settings") or []
            },
            "counter_ids": (unified.get("CounterIds") or {}).get("Items", []),
            "priority_goals": (unified.get("PriorityGoals") or {}).get("Items", []),
            "tracking_params": unified.get("TrackingParams"),
            "attribution_model": unified.get("AttributionModel"),
            "bidding_strategy": unified.get("BiddingStrategy"),
        }

        groups_out: list[dict[str, Any]] = []
        for executed_group in executed_campaign.get("groups") or []:
            group_index = int(executed_group["plan_index"])
            planned_group = planned["groups"][group_index]
            planned_ad_group = planned_group["ad_group"]
            group_id = executed_group.get("id")
            actual_group = actual_groups.get(int(group_id)) if group_id else None
            group_source = actual_group or {
                "name": planned_ad_group.get("Name"),
                "type": "UNIFIED_AD_GROUP",
                "status": None,
                "serving_status": None,
                "region_ids": planned_ad_group.get("RegionIds") or [],
                "restricted_region_ids": [],
                "negative_keywords": (
                    planned_ad_group.get("NegativeKeywords") or {}
                ).get("Items", []),
                "negative_keyword_shared_set_ids": [],
                "tracking_params": planned_ad_group.get("TrackingParams"),
                "offer_retargeting": (
                    planned_ad_group.get("UnifiedAdGroup") or {}
                ).get("OfferRetargeting"),
            }

            keywords_out: list[dict[str, Any]] = []
            for keyword_index, action in enumerate(executed_group.get("keywords") or []):
                identifier = _action_id(action)
                actual = actual_keywords.get(identifier) if identifier else None
                planned_keyword = (
                    planned_group["keywords"][keyword_index]
                    if keyword_index < len(planned_group["keywords"])
                    else {}
                )
                source = actual or planned_keyword
                phrase = source.get("Keyword")
                keywords_out.append({
                    "id": identifier,
                    "keyword": phrase,
                    "autotargeting": phrase == "---autotargeting",
                    "state": source.get("State"),
                    "status": source.get("Status"),
                    "serving_status": source.get("ServingStatus"),
                    "strategy_priority": source.get("StrategyPriority"),
                    "search_bid_is_auto": source.get(
                        "AutotargetingSearchBidIsAuto"
                    ),
                    "settings": source.get("AutotargetingSettings"),
                    "source": "Direct API" if actual else "план",
                })

            ads_out: list[dict[str, Any]] = []
            for ad_index, action in enumerate(executed_group.get("ads") or []):
                identifier = _action_id(action)
                actual = actual_ads.get(identifier) if identifier else None
                planned_ad = (
                    _planned_ad(planned_group["ads"][ad_index])
                    if ad_index < len(planned_group["ads"])
                    else {}
                )
                source = actual or planned_ad
                titles = source.get("titles") or [source.get("title")]
                texts = source.get("texts") or [source.get("text")]
                ads_out.append({
                    "id": identifier,
                    "type": source.get("type"),
                    "feed_id": source.get("feed_id"),
                    "feed_processing_status": source.get("feed_processing_status"),
                    "state": source.get("state"),
                    "status": source.get("status"),
                    "titles": [value for value in titles if value],
                    "texts": [value for value in texts if value],
                    "href": source.get("href"),
                    "display_url_path": source.get("display_url_path"),
                    "business_id": source.get("business_id"),
                    "sitelinks": bool(source.get("sitelinks")),
                    "image": bool(source.get("image")),
                    "extensions": bool(source.get("extensions")),
                    "video": bool(source.get("video")),
                    "source": "Direct API" if actual else "план",
                })

            groups_out.append({
                "id": group_id,
                "name": group_source.get("name"),
                "type": group_source.get("type"),
                "status": group_source.get("status"),
                "serving_status": group_source.get("serving_status"),
                "region_ids": group_source.get("region_ids") or [],
                "region_labels": [
                    (
                        f"{region_names.get(abs(int(value)), 'Регион')} "
                        f"({int(value)})"
                    )
                    for value in group_source.get("region_ids") or []
                ],
                "restricted_region_ids": group_source.get(
                    "restricted_region_ids"
                ) or [],
                "negative_keywords": group_source.get("negative_keywords") or [],
                "negative_keyword_shared_set_ids": group_source.get(
                    "negative_keyword_shared_set_ids"
                ) or [],
                "tracking_params": group_source.get("tracking_params"),
                "offer_retargeting": group_source.get("offer_retargeting"),
                "keywords": [row for row in keywords_out if not row["autotargeting"]],
                "autotargeting": [row for row in keywords_out if row["autotargeting"]],
                "ads": ads_out,
                "source": "Direct API" if actual_group else "план",
            })

        campaigns_out.append({
            "id": campaign_id,
            "name": campaign_source.get("name"),
            "channel": planned.get("channel"),
            "channel_name": _CHANNEL_NAMES.get(
                planned.get("channel"), planned.get("channel")
            ),
            "type": campaign_source.get("type"),
            "state": campaign_source.get("state"),
            "status": campaign_source.get("status"),
            "start_date": campaign_source.get("start_date"),
            "end_date": campaign_source.get("end_date"),
            "time_zone": campaign_source.get("time_zone"),
            "settings": campaign_source.get("settings") or {},
            "counter_ids": campaign_source.get("counter_ids") or [],
            "priority_goals": campaign_source.get("priority_goals") or [],
            "tracking_params": campaign_source.get("tracking_params"),
            "attribution_model": campaign_source.get("attribution_model"),
            "negative_keywords": campaign_source.get("negative_keywords") or [],
            "excluded_sites": campaign_source.get("excluded_sites") or [],
            "negative_keyword_shared_set_ids": campaign_source.get(
                "negative_keyword_shared_set_ids"
            ) or [],
            "bidding_strategy": campaign_source.get("bidding_strategy") or {},
            "groups": groups_out,
            "source": "Direct API" if actual_campaign else "план",
        })

    verified = readback.get("verified") if isinstance(readback, dict) else None
    return {
        "schema": "direct_campaign_creation_report_v1",
        "client_login": login,
        "generated_at": datetime.now(
            timezone(timedelta(hours=3), name="MSK")
        ).isoformat(
            timespec="seconds"
        ),
        "execution_status": execution.get("status"),
        "verified": verified,
        "activated": bool(execution.get("activated")),
        "plan_hash": plan.get("plan_hash"),
        "summary": execution.get("summary") or {},
        "fatal_stage": (execution.get("fatal_error") or {}).get("stage"),
        "campaigns": campaigns_out,
    }


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"), quote=True)


def _yes_no(value: Any) -> str:
    if value in (True, "YES"):
        return "Да"
    if value in (False, "NO"):
        return "Нет"
    return "—" if value is None else str(value)


def _chips(values: list[Any], *, empty: str = "Не заданы") -> str:
    if not values:
        return f'<span class="muted">{_e(empty)}</span>'
    return '<span class="chips">' + "".join(
        f'<span class="chip">{_e(value)}</span>' for value in values
    ) + "</span>"


def _details(title: str, values: list[Any]) -> str:
    if not values:
        return ""
    return (
        f'<details><summary>{_e(title)} · {len(values)}</summary>'
        f'<div class="details-body">{_chips(values)}</div></details>'
    )


def _json(value: Any) -> str:
    return _e(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))

def _names_by_switch(
    values: dict[str, Any], labels: dict[str, str]
) -> tuple[list[str], list[str]]:
    enabled = [
        label for key, label in labels.items() if values.get(key) == "YES"
    ]
    disabled = [
        label for key, label in labels.items() if values.get(key) == "NO"
    ]
    return enabled, disabled


def _weekly_limits(value: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "WeeklySpendLimit":
                found.append(nested)
            else:
                found.extend(_weekly_limits(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_weekly_limits(nested))
    return found


def _campaign_settings(values: dict[str, Any]) -> list[str]:
    labels = {
        "ADD_METRICA_TAG": "Разметка переходов для Метрики",
        "ENABLE_SITE_MONITORING": "Контроль доступности сайта",
        "ENABLE_COMPANY_INFO": "Информация об организации в объявлении",
        "ALTERNATIVE_TEXTS_ENABLED": "Дополнительные варианты текста от Директа",
    }
    return [
        f"{label}: {_yes_no(values[key])}"
        for key, label in labels.items()
        if key in values
    ]



def _money_from_micros(value: Any) -> str:
    try:
        amount = int(value) / 1_000_000
    except (TypeError, ValueError):
        return "—"
    return f"{amount:,.2f} ₽".replace(",", " ").replace(".00", "")


def _status(value: Any) -> str:
    if value is None:
        return "не получен"
    return _e(_STATUS_NAMES.get(str(value), value))


def _type_name(value: Any) -> str:
    return _e(_TYPE_NAMES.get(str(value), value or "—"))


def _render_ad(ad: dict[str, Any]) -> str:
    if ad.get("type") in products.READ_TYPES.values():
        return f"""<article class="ad-card"><h4>{_type_name(ad['type'])} #{_e(ad.get('id'))}</h4>
        <p>Объявления формируются из товарного источника #{_e(ad.get('feed_id'))}.
        Заголовки, цены, изображения и страницы перехода зависят от выбранных товаров.</p>
        <p>Текст по умолчанию: {_e(' '.join(ad.get('texts') or []))}</p>
        <p>Обработка товаров: {_e(ad.get('feed_processing_status') or 'ещё не проверена')}.</p>
        </article>"""
    titles = ad["titles"]
    texts = ad["texts"]
    title_items = "".join(
        f"<li>{_e(value)}</li>" for value in titles
    ) or "<li>—</li>"
    text_items = "".join(
        f"<li>{_e(value)}</li>" for value in texts
    ) or "<li>—</li>"
    href = ad.get("href")
    try:
        parsed = urlsplit(str(href or ""))
        safe_href = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        safe_href = False
    landing = (
        f'<a href="{_e(href)}" rel="noopener noreferrer">{_e(href)}</a>'
        if safe_href
        else (f'Профиль организации {_e(ad.get("business_id"))}' if ad.get("business_id") else "—")
    )
    additions = [
        label
        for enabled, label in (
            (ad.get("image"), "Изображение"),
            (ad.get("sitelinks"), "Быстрые ссылки"),
            (ad.get("extensions"), "Уточнения"),
            (ad.get("video"), "Видео"),
        )
        if enabled
    ]
    return f"""
    <article class="ad-card">
      <div class="object-head"><div><p class="eyebrow">{_type_name(ad.get('type'))}</p><strong>Объявление #{_e(ad.get('id'))}</strong></div><span>{_status(ad.get('status'))}</span></div>
      <p class="explain">Директ автоматически выбирает подходящую комбинацию заголовка и текста для каждого показа.</p>
      <div class="component-grid">
        <div class="component-list"><strong>Варианты заголовков · {len(titles)}</strong><ol>{title_items}</ol></div>
        <div class="component-list"><strong>Варианты текстов · {len(texts)}</strong><ol>{text_items}</ol></div>
      </div>
      <p class="landing"><span>Страница перехода</span>{landing}</p>
      <p class="muted">Короткая подпись ссылки: {_e(ad.get('display_url_path'))}</p>
      <div><span class="label">Дополнения объявления</span>{_chips(additions, empty='Не добавлены')}</div>
    </article>"""


def _render_autotargeting(row: dict[str, Any]) -> str:
    settings = row.get("settings") or {}
    categories = settings.get("categories") or settings.get("Categories") or {}
    brands = settings.get("brand_options") or settings.get("BrandOptions") or {}
    enabled_categories, disabled_categories = _names_by_switch(
        categories, _AUTOTARGETING_CATEGORY_NAMES
    )
    enabled_brands, disabled_brands = _names_by_switch(
        brands, _AUTOTARGETING_BRAND_NAMES
    )
    return f"""
    <div class="target-card autotargeting-card">
      <div class="object-head"><strong>Автотаргетинг</strong><span class="muted">ID {_e(row.get('id'))}</span></div>
      <p>Директ сопоставляет запрос пользователя с содержанием объявления и страницы сайта. Это дополняет ручные ключевые фразы.</p>
      <div><span class="label">Используются категории</span>{_chips(enabled_categories, empty='Не выбраны')}</div>
      <div><span class="label">Не используются</span>{_chips(disabled_categories, empty='Нет')}</div>
      <div><span class="label">Запросы по упоминанию брендов</span>{_chips(enabled_brands, empty='Не выбраны')}</div>
      <div><span class="label">Исключены по брендам</span>{_chips(disabled_brands, empty='Нет')}</div>
      <span>Ставка для автотаргетинга рассчитывается автоматически: <strong>{_e(_yes_no(row.get('search_bid_is_auto')))}</strong></span>
    </div>"""


def _render_group(group: dict[str, Any]) -> str:
    keywords = group["keywords"]
    autotargeting = group["autotargeting"]
    keyword_rows = "".join(
        f"<tr><td>{index}</td><td>{_e(row.get('keyword'))}</td></tr>"
        for index, row in enumerate(keywords, start=1)
    ) or '<tr><td colspan="2" class="muted">Ключевые фразы не созданы</td></tr>'
    auto_rows = "".join(
        _render_autotargeting(row) for row in autotargeting
    ) or '<p class="muted">Автоматический подбор запросов не настроен.</p>'
    ads_html = "".join(_render_ad(row) for row in group["ads"])
    region_values = group.get("region_labels") or group.get("region_ids") or []
    return f"""
    <section class="group">
      <div class="object-head"><div><p class="eyebrow">Группа объявлений</p><h3>{_e(group.get('name'))}</h3></div><span class="id">ID {_e(group.get('id'))}</span></div>
      <div class="facts">
        <div><span>Тип</span><strong>{_type_name(group.get('type'))}</strong></div>
        <div><span>Статус</span><strong>{_status(group.get('status'))}</strong></div>
        <div><span>Готовность к показам</span><strong>{_status(group.get('serving_status'))}</strong></div>
        <div><span>Объявлений</span><strong>{len(group['ads'])}</strong></div>
      </div>
      <div class="target-grid">
        <div class="target-card"><strong>Где находится аудитория</strong><p>Показы настроены для пользователей из выбранных регионов.</p>{_chips(region_values)}</div>
        <div class="target-card"><strong>Как подбираются запросы</strong><p>{len(keywords)} ключевых фраз заданы вручную. Дополнительно работает {len(autotargeting)} настройка автотаргетинга.</p></div>
      </div>
      {_details('Минус-фразы группы: исключают нецелевые запросы', group.get('negative_keywords') or [])}
      <h4 class="section-title">Автоматический подбор запросов</h4>{auto_rows}
      <h4 class="section-title">Ключевые фразы · {len(keywords)}</h4>
      <p class="muted">По этим фразам Директ сопоставляет объявления с поисковыми запросами пользователей.</p>
      <div class="table-wrap"><table><thead><tr><th>№</th><th>Ключевая фраза</th></tr></thead><tbody>{keyword_rows}</tbody></table></div>
      <h4 class="section-title">Комбинаторные объявления · {len(group['ads'])}</h4>
      <p class="muted">Каждое объявление содержит несколько вариантов. Директ тестирует сочетания и выбирает подходящее для конкретного показа.</p>
      <div class="ads-grid">{ads_html}</div>
    </section>"""


def _render_campaign(campaign: dict[str, Any]) -> str:
    strategy = campaign.get("bidding_strategy") or {}
    search = strategy.get("Search") or {}
    network = strategy.get("Network") or {}
    strategy_code = search.get("BiddingStrategyType")
    strategy_name = _STRATEGY_NAMES.get(strategy_code, strategy_code or "Не определена")
    placements = search.get("PlacementTypes") or {}
    enabled_placements, disabled_placements = _names_by_switch(
        placements, _PLACEMENT_NAMES
    )
    if not placements and strategy_code and strategy_code != "SERVING_OFF":
        enabled_placements = ["Поиск Яндекса"]
    budgets = [
        f"{_money_from_micros(value)} в неделю"
        for value in dict.fromkeys(_weekly_limits(strategy))
    ]
    network_code = network.get("BiddingStrategyType")
    network_note = (
        "Выключена — кампания работает только на Поиске"
        if network_code == "SERVING_OFF"
        else _STRATEGY_NAMES.get(network_code, network_code or "Не настроена")
    )
    settings = _campaign_settings(campaign["settings"])
    goals = [
        f"Цель {row.get('GoalId')}"
        for row in campaign["priority_goals"]
    ]
    goals_note = goals or ["Отдельные цели не выбраны: стратегия оптимизирует клики"]
    attribution = {
        "AUTO": "Автоматическая",
        "FC": "Первый переход",
        "LC": "Последний переход",
        "LSC": "Последний значимый переход",
    }.get(campaign.get("attribution_model"), campaign.get("attribution_model") or "Не указана")
    tracking_note = (
        "Включена: переходы размечаются для последующего анализа"
        if campaign.get("tracking_params")
        else "Дополнительная разметка не задана"
    )
    return f"""
    <section class="campaign">
      <div class="object-head"><div><p class="eyebrow">Канал: {_e(campaign.get('channel_name'))}</p><h2>{_e(campaign.get('name'))}</h2></div><span class="id">ID {_e(campaign.get('id'))}</span></div>
      <p class="explain">Кампания привлекает посетителей из результатов поиска Яндекса. Показы в рекламной сети и дополнительных размещениях отключены.</p>
      <div class="facts">
        <div><span>Тип кампании</span><strong>{_type_name(campaign.get('type'))}</strong></div>
        <div><span>Состояние</span><strong>{_status(campaign.get('state'))}</strong></div>
        <div><span>Статус</span><strong>{_status(campaign.get('status'))}</strong></div>
        <div><span>Период</span><strong>{_e(campaign.get('start_date'))} — {_e(campaign.get('end_date') or 'без даты окончания')}</strong></div>
      </div>
      <div class="settings-grid">
        <div><span>Как расходуется бюджет</span><strong>{_e(strategy_name)}</strong><p>Директ старается получить максимум переходов в пределах заданного бюджета.</p></div>
        <div><span>Лимит расходов</span>{_chips(budgets)}</div>
        <div><span>Где идут показы</span>{_chips(enabled_placements)}</div>
        <div><span>Где показы отключены</span>{_chips(disabled_placements, empty='Дополнительных ограничений нет')}</div>
        <div><span>Рекламная сеть Яндекса</span><strong>{_e(network_note)}</strong></div>
        <div><span>Яндекс Метрика</span>{_chips(campaign['counter_ids'], empty='Счётчик не указан')}</div>
        <div><span>Как учитываются результаты</span><strong>{_e(attribution)}</strong></div>
        <div><span>Часовой пояс</span><strong>{_e(campaign.get('time_zone') or 'Москва')}</strong></div>
        <div><span>Разметка переходов</span><strong>{_e(tracking_note)}</strong></div>
        <div><span>Дополнительные настройки</span>{_chips(settings)}</div>
        <div><span>Цели оптимизации</span>{_chips(goals_note)}</div>
      </div>
      <p class="muted">Минус-фразы не дают показывать объявления по заведомо нецелевым запросам.</p>
      {_details('Минус-фразы кампании', campaign['negative_keywords'])}
      {_details('Исключённые рекламные площадки', campaign['excluded_sites'])}
      <div class="groups">{''.join(_render_group(row) for row in campaign['groups'])}</div>
    </section>"""


def render(model: dict[str, Any]) -> str:
    summary = model.get("summary") or {}
    summary_cards = "".join(
        f"<div><span>{_e(label)}</span><strong>{_e((summary.get(key) or {}).get('created', 0))}</strong></div>"
        for key, label in (
            ("campaigns", "Кампании"),
            ("ad_groups", "Группы"),
            ("keywords", "Ключевые фразы"),
            ("ads", "Объявления"),
        )
    )
    campaigns = "".join(_render_campaign(row) for row in model["campaigns"])
    activated = bool(model.get("activated"))
    launch_class = "ok" if activated else "warn"
    launch_note = (
        "Показы запущены: объявления могут участвовать в аукционе."
        if activated
        else "Кампании созданы как черновики. Показы и расход бюджета пока не начались."
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Создание кампаний · {_e(model['client_login'])}</title>
<style>
:root{{--ink:#162033;--muted:#667085;--line:#dde3ec;--paper:#fff;--bg:#f3f6fa;--blue:#1258ca;--soft:#eaf1ff;--ok:#087f5b;--warn:#9a6700;--danger:#b42318}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 Inter,Segoe UI,Arial,sans-serif}}main{{max-width:1180px;margin:auto;padding:42px 22px 80px}}h1,h2,h3,h4,p{{margin-top:0}}h1{{font-size:clamp(30px,5vw,52px);line-height:1.05;max-width:850px}}h2{{font-size:28px}}h3{{font-size:21px;margin-bottom:4px}}a{{color:var(--blue);overflow-wrap:anywhere}}code,pre{{font:13px/1.45 Consolas,monospace}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#111827;color:#e5e7eb;padding:14px;border-radius:10px}}.hero{{background:#0f2450;color:#fff;padding:38px;border-radius:24px;box-shadow:0 18px 45px #12244a26}}.hero p{{color:#c9d8fa}}.meta,.object-head,.facts,.settings-grid,.target-grid,.summary-grid,.ads-grid,.component-grid{{display:grid;gap:14px}}.meta{{grid-template-columns:repeat(4,minmax(0,1fr));margin-top:25px}}.meta span,.facts span,.settings-grid>div>span,.summary-grid span{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}}.meta strong{{color:#fff}}.summary-grid{{grid-template-columns:repeat(4,minmax(0,1fr));margin:22px 0}}.summary-grid>div,.settings-grid>div,.target-card{{background:var(--paper);border:1px solid var(--line);padding:16px;border-radius:14px}}.summary-grid strong{{display:inline-block;font-size:30px;margin-right:6px}}.summary-grid small{{color:var(--muted)}}.notice{{padding:14px 18px;border-radius:12px;margin:20px 0;background:#fff;border-left:5px solid}}.notice.ok{{border-color:var(--ok)}}.notice.warn{{border-color:var(--warn)}}.notice.danger{{border-color:var(--danger)}}.danger-text{{color:var(--danger);font-weight:700}}.campaign{{background:var(--paper);border:1px solid var(--line);border-radius:20px;padding:26px;margin:28px 0;box-shadow:0 8px 24px #12244a0d}}.object-head{{grid-template-columns:1fr auto;align-items:start}}.id{{background:var(--soft);color:var(--blue);padding:6px 10px;border-radius:999px;font-weight:700}}.eyebrow{{color:var(--blue);font-size:12px;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}}.facts{{grid-template-columns:repeat(4,minmax(0,1fr));margin:16px 0}}.facts>div{{border-top:1px solid var(--line);padding-top:10px}}.settings-grid{{grid-template-columns:repeat(2,minmax(0,1fr));margin:20px 0}}.settings-grid strong,.settings-grid code{{display:block;margin-top:6px;overflow-wrap:anywhere}}.chips{{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}}.chip{{background:#eef2f7;border-radius:999px;padding:4px 9px;font-size:13px}}details{{border-top:1px solid var(--line);padding:12px 0}}summary{{cursor:pointer;font-weight:700}}.details-body{{padding-top:8px}}.group{{border:1px solid var(--line);border-radius:16px;padding:20px;margin-top:22px;background:#fbfcfe}}.target-grid{{grid-template-columns:repeat(2,minmax(0,1fr));margin:14px 0}}.target-card{{display:grid;gap:8px}}.section-title{{margin:24px 0 10px}}.table-wrap{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:680px}}th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line)}}th{{font-size:12px;text-transform:uppercase;color:var(--muted)}}.ads-grid{{grid-template-columns:1fr}}.component-grid{{grid-template-columns:repeat(2,minmax(0,1fr));margin:14px 0}}.component-list{{background:#f7f9fc;border:1px solid var(--line);border-radius:12px;padding:14px}}.component-list ol{{margin:10px 0 0;padding-left:24px}}.component-list li{{padding:3px 0}}.ad-card{{border:1px solid var(--line);padding:18px;border-radius:14px;background:#fff}}.ad-card h4{{margin:12px 0 6px}}.ad-card p{{margin-bottom:8px}}.landing{{font-weight:600;display:grid;gap:4px}}.landing span,.label{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}}.explain{{background:var(--soft);padding:12px 14px;border-radius:10px}}.autotargeting-card p{{margin-bottom:4px}}.muted{{color:var(--muted)}}footer{{color:var(--muted);margin-top:35px;text-align:center}}@media(max-width:760px){{main{{padding:18px 12px 50px}}.hero,.campaign{{padding:20px;border-radius:16px}}.meta,.summary-grid,.facts,.settings-grid,.target-grid,.ads-grid,.component-grid{{grid-template-columns:1fr}}.object-head{{grid-template-columns:1fr}}.id{{justify-self:start}}}}@media print{{body{{background:#fff}}main{{max-width:none;padding:0}}.hero{{box-shadow:none}}details{{display:block}}summary{{list-style:none}}}}
</style></head><body><main>
<header class="hero"><p class="eyebrow">Яндекс Директ · итог настройки</p><h1>Отчёт о создании рекламных кампаний</h1><p>Что именно настроено и как это будет работать — без технических обозначений.</p><p>Клиентский кабинет: <strong>{_e(model['client_login'])}</strong></p>
<div class="meta"><div><span>Дата отчёта</span><strong>{_e(model['generated_at'])}</strong></div><div><span>Результат работы</span><strong>{_e(model.get('execution_status'))}</strong></div><div><span>Показы</span><strong>{'Запущены' if activated else 'Пока не запущены'}</strong></div></div></header>
<section class="summary-grid">{summary_cards}</section>
<section class="notice {launch_class}"><strong>{_e(launch_note)}</strong></section>
{campaigns}
<footer>Подготовлено по фактическим настройкам рекламных кампаний в Яндекс Директе.</footer>
</main></body></html>"""


def persist(model: dict[str, Any], out_dir: Path) -> Path:
    login = _login(model.get("client_login"))
    target = out_dir / store.safe_stem(login) / "create" / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(model), encoding="utf-8", newline="\n")
    return target


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    )


def publish(
    local_path: Path,
    *,
    client_login: str,
    ssh_host: str,
    remote_root: str,
    public_base_url: str,
) -> dict[str, Any]:
    """Back up, atomically publish, and verify the public HTML byte-for-byte."""
    login = _login(client_login)
    if not _SSH_HOST_RE.fullmatch(ssh_host):
        raise ValueError("SSH host содержит недопустимые символы")
    root = remote_root.rstrip("/")
    if not root.startswith("/") or ".." in Path(root).parts:
        raise ValueError("remote_root должен быть безопасным абсолютным путём")
    if not local_path.is_file():
        raise ValueError("Локальный HTML-отчёт не найден")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    remote_dir = f"{root}/{login}/create"
    remote_index = f"{remote_dir}/index.html"
    remote_tmp = f"{remote_dir}/.index.html.{stamp}.tmp"
    remote_backup = f"{remote_dir}/index.html.backup-{stamp}"
    url = public_url(login, public_base_url)

    _run([
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", ssh_host,
        f"mkdir -p {shlex.quote(remote_dir)} && chmod 0755 {shlex.quote(remote_dir)}",
    ])
    try:
        _run([
            "scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            str(local_path), f"{ssh_host}:{remote_tmp}",
        ])
        finalize = (
            f"if [ -f {shlex.quote(remote_index)} ]; then "
            f"cp -p {shlex.quote(remote_index)} {shlex.quote(remote_backup)}; fi && "
            f"chmod 0644 {shlex.quote(remote_tmp)} && "
            f"mv -f {shlex.quote(remote_tmp)} {shlex.quote(remote_index)}"
        )
        _run([
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", ssh_host,
            finalize,
        ])
    except Exception:
        with suppress(Exception):
            _run([
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", ssh_host,
                f"rm -f -- {shlex.quote(remote_tmp)}",
            ])
        raise

    expected = local_path.read_bytes()
    response = httpx.get(url, timeout=20, follow_redirects=True)
    response.raise_for_status()
    expected_sha = hashlib.sha256(expected).hexdigest()
    actual_sha = hashlib.sha256(response.content).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError("Публичный HTML не совпадает с локальным артефактом")
    return {
        "status": "published",
        "public_url": url,
        "remote_path": remote_index,
        "backup_path": remote_backup,
        "sha256": expected_sha,
        "http_status": response.status_code,
        "verified": True,
    }
