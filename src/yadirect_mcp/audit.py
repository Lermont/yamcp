"""Детерминированный аудит кампаний по версионируемой агентской политике."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    account,
    adgroups,
    ads,
    assets,
    campaigns,
    completeness,
    creative,
    keywords,
    link_checks,
    negatives,
    policy,
    products,
    schedule,
    store,
    tracking,
)
from .identifiers import wire

INACTIVE_STRATEGIES = {None, "SERVING_OFF", "UNKNOWN"}


def _finding(rule: str, status: str, message: str, **evidence: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"rule": rule, "status": status, "message": message}
    if evidence:
        out["evidence"] = evidence
    return out


def _active_strategy(strategy: dict[str, Any] | None, placement: str) -> str | None:
    return (strategy or {}).get(placement, {}).get("BiddingStrategyType")


def classify_channel(campaign: dict[str, Any]) -> dict[str, Any]:
    strategy = campaign.get("bidding_strategy") or {}
    search_type = _active_strategy(strategy, "Search")
    network_type = _active_strategy(strategy, "Network")
    search_on = search_type not in INACTIVE_STRATEGIES
    network_on = network_type not in INACTIVE_STRATEGIES
    placements = (strategy.get("Search") or {}).get("PlacementTypes") or {}
    maps_on = any(
        placements.get(key) == "YES" for key in ("Maps", "SearchOrganizationList")
    )
    regular_search_on = search_on and (
        not placements
        or any(
            placements.get(key) == "YES"
            for key in ("SearchResults", "ProductGallery", "DynamicPlaces")
        )
    )

    if (search_on and placements.get("ProductGallery") == "YES"
            and placements.get("SearchResults") == "NO"
            and placements.get("DynamicPlaces") == "NO" and not maps_on):
        channel = "product"
    elif network_on and (regular_search_on or maps_on):
        channel = "mixed_search_network"
    elif regular_search_on and maps_on:
        channel = "mixed_search_maps"
    elif maps_on:
        channel = "maps"
    elif regular_search_on:
        channel = "search"
    elif network_on:
        channel = "network"
    else:
        channel = "disabled_or_unknown"
    return {
        "channel": channel,
        "search_strategy": search_type,
        "network_strategy": network_type,
        "search_placements": placements,
    }


def _weekly_budgets(value: Any) -> list[int]:
    found: list[int] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "WeeklySpendLimit" and isinstance(nested, int):
                found.append(nested)
            else:
                found.extend(_weekly_budgets(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_weekly_budgets(nested))
    return found


def _strategy_types(channel: dict[str, Any]) -> list[str]:
    return [
        value
        for value in (channel["search_strategy"], channel["network_strategy"])
        if value not in INACTIVE_STRATEGIES
    ]


def _is_24_7(value: Any) -> bool:
    return schedule.is_24_7(value)


def _audit_ads(campaign_id: int | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    campaign_ads = [row for row in rows if row.get("campaign_id") == campaign_id]
    findings: list[dict[str, Any]] = []
    if not campaign_ads:
        return [
            _finding(
                "ads.present",
                policy.BLOCK,
                "В кампании не найдены объявления.",
            )
        ]
    product_ads = [r for r in campaign_ads if r.get("type") in products.READ_TYPES.values()]
    if product_ads:
        counts = Counter((r.get("ad_group_id"), r.get("type")) for r in product_ads
                         if r.get("state") != "ARCHIVED")
        invalid = [r.get("id") for r in product_ads if not r.get("feed_id")
                   or r.get("feed_processing_status") != "PROCESSED"
                   or len(r.get("texts", [])) != 1 or not r.get("texts", [None])[0]]
        findings.append(_finding("ads.product", policy.BLOCK if invalid or any(
            n > 1 for n in counts.values()) else policy.PASS,
            "Проверка товарного источника, обработки и числа объявлений каждого типа.",
            invalid_ads=invalid))
        findings.append(_finding("product.generated_offers", policy.MANUAL,
            "Проверить фактические товары, цены, изображения и URL после фильтра в интерфейсе."))
    campaign_ads = [r for r in campaign_ads if r not in product_ads]
    if not campaign_ads:
        return findings
    wrong_type = [
        row.get("id") for row in campaign_ads if row.get("type") != "RESPONSIVE_AD"
    ]
    group_counts = Counter(
        row["ad_group_id"] for row in campaign_ads
        if row.get("type") == "RESPONSIVE_AD" and row.get("state") != "ARCHIVED"
        and row.get("ad_group_id") is not None
    )
    over_limit = {str(key): value for key, value in group_counts.items()
                  if value > policy.RESPONSIVE_ADS_MAX_NON_ARCHIVED}
    missing_group_id = sum(
        1 for row in campaign_ads
        if row.get("type") == "RESPONSIVE_AD" and row.get("ad_group_id") is None
    )
    findings.append(_finding(
        "ads.responsive_group_limit",
        policy.MANUAL if over_limit or missing_group_id else policy.PASS,
        "При создании допустимы 1–3 объявления в группе. Существующие группы с "
        "большим числом могут быть перенесены Яндексом; проверьте историю миграции.",
        groups_over_limit=over_limit, maximum_new_non_archived=3,
        maximum_total_including_archived=10, archived_inventory_verified=False,
        ads_without_group_id=missing_group_id,
    ))
    findings.append(
        _finding(
            "ads.responsive_format",
            policy.BLOCK if wrong_type else policy.PASS,
            (
                "Найдены объявления неактуального формата."
                if wrong_type
                else "Все объявления имеют актуальный тип ResponsiveAd."
            ),
            ad_ids=wrong_type,
        )
    )
    invalid_assets = []
    underused_titles = []
    for row in campaign_ads:
        titles = row.get("titles") or []
        texts = row.get("texts") or []
        unique_titles = len({str(value).strip().casefold() for value in titles})
        unique_texts = len({str(value).strip().casefold() for value in texts})
        utilization = policy.responsive_title_utilization(titles)
        if not utilization["passes"]:
            underused_titles.append({
                "ad_id": row.get("id"),
                **utilization,
            })
        if (
            not 3 <= len(titles) <= 7
            or len(texts) != 3
            or unique_titles != len(titles)
            or unique_texts != len(texts)
        ):
            invalid_assets.append(
                {
                    "ad_id": row.get("id"),
                    "titles": len(titles),
                    "unique_titles": unique_titles,
                    "texts": len(texts),
                    "unique_texts": unique_texts,
                }
            )
    findings.append(
        _finding(
            "ads.responsive_assets",
            policy.BLOCK if invalid_assets else policy.PASS,
            (
                "Комбинаторные объявления должны содержать 3–7 уникальных "
                "заголовков и ровно 3 уникальных текста."
                if invalid_assets
                else "Набор заголовков и текстов комбинаторных объявлений полный."
            ),
            invalid_ads=invalid_assets,
        )
    )
    findings.append(
        _finding(
            "ads.responsive_title_utilization",
            policy.WARNING if underused_titles else policy.PASS,
            (
                "Минимум три заголовка в каждом объявлении должны использовать "
                "45–56 символов, чтобы содержательно задействовать лимит Директа."
                if underused_titles
                else "Заголовки содержательно используют доступный лимит Директа."
            ),
            ads=underused_titles,
        )
    )
    button_targets = [
        str(row["id"])
        for row in campaign_ads
        if row.get("href")
        and (row.get("ad_image_hashes") or row.get("video_extension_ids"))
        and row.get("state") != "ARCHIVED"
        and row.get("id") is not None
    ]
    if button_targets:
        findings.append(_finding(
            "ads.action_button", policy.MANUAL,
            "Проверить в сохранённом объявлении релевантную кнопку и явный URL "
            "(основной либо страницы контактов). API не подтверждает кнопку.",
            ad_ids=button_targets, api_supported=False,
        ))
    missing_href = [row.get("id") for row in campaign_ads
                    if not row.get("href") and not row.get("business_id")]
    findings.append(
        _finding(
            "ads.href",
            policy.BLOCK if missing_href else policy.PASS,
            (
                "У всех объявлений есть ссылка."
                if not missing_href
                else "У части объявлений нет ссылки."
            ),
            ad_ids=missing_href,
        )
    )
    missing_sitelinks = [
        row.get("id") for row in campaign_ads if not row.get("sitelinks")
    ]
    findings.append(
        _finding(
            "ads.sitelinks",
            policy.WARNING if missing_sitelinks else policy.PASS,
            (
                "В API части объявлений нет явного набора быстрых ссылок; "
                "проверьте наследование в интерфейсе."
                if missing_sitelinks
                else "Быстрые ссылки подключены ко всем объявлениям."
            ),
            ad_ids=missing_sitelinks,
        )
    )
    counts = [
        {"ad_id": row.get("id"), "sitelink_set_id": row.get("sitelink_set_id"),
         "count": row.get("sitelink_count"), "error": row.get("sitelink_check_error")}
        for row in campaign_ads
    ]
    invalid = [row for row in counts if isinstance(row["count"], int)
               and not policy.SITELINKS_MINIMUM <= row["count"] <= policy.SITELINKS_MAXIMUM]
    unknown = [row for row in counts if row["count"] is None]
    below_recommended = [
        row for row in counts if isinstance(row["count"], int)
        and policy.SITELINKS_MINIMUM <= row["count"] < policy.SITELINKS_RECOMMENDED
    ]
    count_status = policy.PASS
    if invalid:
        count_status = policy.BLOCK
    elif unknown:
        count_status = policy.MANUAL
    elif below_recommended:
        count_status = policy.WARNING
    count_message = "Быстрых ссылок должно быть от 4 до 8; рекомендуется 8 полезных ссылок."
    if unknown:
        count_message = (
            "Количество быстрых ссылок подтверждено не для всех объявлений; "
            "проверьте наборы и наследование в интерфейсе. Минимум 4, рекомендуется 8."
        )
    findings.append(_finding(
        "ads.sitelink_count",
        count_status, count_message,
        minimum=policy.SITELINKS_MINIMUM, recommended=policy.SITELINKS_RECOMMENDED,
        ads=counts,
    ))
    missing_extensions = [
        row.get("id") for row in campaign_ads if not row.get("extensions")
    ]
    findings.append(
        _finding(
            "ads.extensions",
            policy.WARNING if missing_extensions else policy.PASS,
            (
                "У части объявлений нет уточнений/дополнений."
                if missing_extensions
                else "Уточнения/дополнения подключены ко всем объявлениям."
            ),
            ad_ids=missing_extensions,
        )
    )
    return findings


def _audit_keywords(
    campaign_id: int | None,
    rows: list[dict[str, Any]],
    group_ids: set[int],
) -> list[dict[str, Any]]:
    campaign_rows = [row for row in rows if row.get("campaign_id") == campaign_id]
    manual = [row for row in campaign_rows if row.get("keyword") != "---autotargeting"]
    autotargeting = [
        row for row in campaign_rows if row.get("keyword") == "---autotargeting"
    ]
    by_group = Counter(row.get("ad_group_id") for row in autotargeting)
    missing_groups = sorted(group_ids - set(by_group))
    duplicate_groups = sorted(
        group_id for group_id, count in by_group.items() if group_id and count != 1
    )
    findings = [
        _finding(
            "keywords.manual_present",
            policy.PASS if manual else policy.BLOCK,
            (
                "Ручные ключевые фразы присутствуют."
                if manual
                else "Ручные ключевые фразы не найдены."
            ),
            manual_count=len(manual),
        ),
        _finding(
            "autotargeting.one_per_group",
            policy.BLOCK if missing_groups or duplicate_groups else policy.PASS,
            (
                "В каждой группе есть ровно один автотаргетинг."
                if not missing_groups and not duplicate_groups
                else "Не во всех группах найден ровно один автотаргетинг."
            ),
            missing_group_ids=missing_groups,
            duplicate_group_ids=duplicate_groups,
        ),
    ]
    expanded = []
    competitor = []
    for row in autotargeting:
        settings = row.get("autotargeting") or {}
        categories = settings.get("categories") or {}
        brands = settings.get("brand_options") or {}
        if any(
            categories.get(name) == "YES"
            for name in ("Alternative", "Accessory", "Broader")
        ):
            expanded.append(row.get("id"))
        if brands.get("WithCompetitorsBrand") == "YES":
            competitor.append(row.get("id"))
    findings.append(
        _finding(
            "autotargeting.expanded_categories",
            policy.WARNING if expanded else policy.PASS,
            (
                "Включены расширяющие категории автотаргетинга."
                if expanded
                else "Расширяющие категории автотаргетинга выключены."
            ),
            keyword_ids=expanded,
        )
    )
    findings.append(
        _finding(
            "autotargeting.competitor_brands",
            policy.WARNING if competitor else policy.PASS,
            (
                "Включены запросы с брендами конкурентов."
                if competitor
                else "Запросы с брендами конкурентов выключены."
            ),
            keyword_ids=competitor,
        )
    )
    return findings


def audit_campaign(
    campaign: dict[str, Any],
    *,
    selected_policy: dict[str, Any],
    landing_pages: list[dict[str, Any]] | None = None,
    groups: list[dict[str, Any]] | None = None,
    ad_rows: list[dict[str, Any]] | None = None,
    keyword_rows: list[dict[str, Any]] | None = None,
    bid_modifiers: list[dict[str, Any]] | None = None,
    approved_budget_campaign_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Проверить одну нормализованную кампанию; функция не вызывает API."""
    findings: list[dict[str, Any]] = []
    channel = classify_channel(campaign)
    channel_name = channel["channel"]
    if campaign.get("type") not in {None, "TEXT_CAMPAIGN", "UNIFIED_CAMPAIGN"}:
        findings = [_finding(
            "coverage.unsupported_campaign_type", policy.MANUAL,
            "Тип кампании не поддерживается этим аудитом; настройки требуют ручной проверки.",
            campaign_type=campaign.get("type"),
        )]
        return {
            "campaign": campaign, "channel": channel, "supported": False,
            "tracking_profile": {}, "landing_page_checks": [], "findings": findings,
            "summary": {status: int(status == policy.MANUAL) for status in policy.STATUSES},
            "ready": True,
        }

    if "profile" in selected_policy:
        findings.append(_finding(
            "profile.evidence", policy.MANUAL,
            "Для проверки типа бизнес-целей, истории и готовности измерения нужен "
            "исходный profile_context и просмотр источников статистики.",
            profile=selected_policy["profile"],
        ))
    if channel_name.startswith("mixed_"):
        findings.append(_finding(
            "structure.separate_channels",
            policy.BLOCK,
            "В одной кампании объединены места показа, которые регламент требует разделять.",
            channel=channel_name,
        ))
    elif channel_name == "disabled_or_unknown":
        findings.append(_finding(
            "structure.separate_channels",
            policy.MANUAL,
            "По стратегии нельзя подтвердить активный канал показов.",
        ))
    else:
        findings.append(_finding(
            "structure.separate_channels",
            policy.PASS,
            f"Кампания относится к одному каналу: {channel_name}.",
        ))

    budget_policy = selected_policy["budget"]
    micros = _weekly_budgets(campaign.get("bidding_strategy"))
    amounts = [round(value / 1_000_000, 2) for value in micros]
    if amounts and budget_policy["range_enforcement"] == "client_total":
        findings.append(_finding(
            "budget.client_total", policy.MANUAL,
            "Общий лимит проверяется по исходному плану; отдельный аудит не знает его сумму.",
            amounts=amounts,
        ))
    elif amounts and all(
        budget_policy["minimum"] <= value <= budget_policy["maximum"]
        for value in amounts
    ):
        findings.append(_finding(
            "budget.weekly_range",
            policy.PASS,
            "Недельный бюджет находится в диапазоне регламента.",
            amounts=amounts,
        ))
    elif amounts and campaign.get("id") in (approved_budget_campaign_ids or set()):
        findings.append(_finding(
            "budget.explicit_override",
            policy.PASS,
            "Недельный бюджет вне базового диапазона, но явно утверждён.",
            amounts=amounts,
        ))
    elif amounts:
        findings.append(_finding(
            "budget.weekly_range",
            policy.WARNING,
            "Недельный бюджет выходит за рекомендуемый диапазон 2 000–5 000.",
            amounts=amounts,
        ))
    elif campaign.get("daily_budget_micros") is not None:
        daily = round(campaign["daily_budget_micros"] / 1_000_000, 2)
        findings.append(_finding(
            "budget.weekly_range",
            policy.WARNING,
            "Используется дневной бюджет; недельный эквивалент нужно согласовать вручную.",
            daily_amount=daily,
            weekly_equivalent=round(daily * 7, 2),
        ))
    else:
        findings.append(_finding(
            "budget.weekly_range",
            policy.BLOCK,
            "В настройках стратегии не найден недельный бюджет.",
        ))

    types = _strategy_types(channel)
    blocked = sorted(set(types) & set(selected_policy["strategy"]["blocked"]))
    warned = sorted(
        set(types) & set(selected_policy["strategy"]["allowed_with_warning"])
    )
    if blocked:
        findings.append(_finding(
            "strategy.no_target_cpa",
            policy.BLOCK,
            "Выбрана стратегия с целевой CPA/конверсией вместо ограничения расхода бюджетом.",
            strategies=blocked,
        ))
    elif warned:
        findings.append(_finding(
            "strategy.no_target_cpa",
            policy.WARNING,
            "Стратегия допустима только как осознанное отклонение от рекомендуемой.",
            strategies=warned,
        ))
    elif types:
        findings.append(_finding(
            "strategy.no_target_cpa",
            policy.PASS,
            "Стратегия не использует запрещённое ограничение по целевой CPA.",
            strategies=types,
        ))
    else:
        findings.append(_finding(
            "strategy.no_target_cpa",
            policy.MANUAL,
            "Тип стратегии не удалось определить.",
        ))

    time_targeting = campaign.get("time_targeting")
    if time_targeting is None:
        findings.append(
            _finding(
                "schedule.coverage",
                policy.MANUAL,
                "API не вернул временной таргетинг.",
            )
        )
    else:
        full_time = _is_24_7(time_targeting)
        findings.append(
            _finding(
                "schedule.coverage",
                policy.PASS if full_time else policy.WARNING,
                (
                    "Расписание покрывает 24 часа все семь дней."
                    if full_time
                    else "Расписание ограничено; сопоставьте его с утверждённым брифом."
                ),
            )
        )

    profiles = selected_policy["tracking_profiles"]
    checks = {name: tracking.compare_profile(campaign.get("tracking_params"), spec["params"])
              for name, spec in profiles.items()}
    profile_name = next((name for name, result in checks.items() if result["matches"]),
                        selected_policy.get("tracking_default_profile", "regulation_v1"))
    tracking_check = {**checks[profile_name], "profile": profile_name}
    if tracking_check["matches"]:
        findings.append(_finding(
            "tracking.campaign_profile",
            policy.PASS,
            f"Кампанийная разметка соответствует {profile_name}.",
        ))
    else:
        findings.append(_finding(
            "tracking.campaign_profile",
            policy.BLOCK,
            f"Кампанийная разметка отсутствует или не совпадает с профилем {profile_name}.",
            missing=tracking_check["missing"],
            mismatched=tracking_check["mismatched"],
        ))

    if channel_name in {"search", "maps", "mixed_search_maps", "mixed_search_network"}:
        actual = campaign.get("negative_keywords") or []
        actual_negatives = {str(value).strip().casefold() for value in actual}
        findings.append(_finding(
            "search.negative_keywords",
            policy.MANUAL,
            "Универсального обязательного списка нет. Сверьте минус-фразы с "
            "товарами, услугами, посадочной и поисковыми запросами; отсутствие "
            "слова из старого снимка не является ошибкой.",
            actual_count=len(actual_negatives),
            universal_required_list=False,
        ))
        campaign_ads = [row for row in ad_rows or []
                        if row.get("campaign_id") == campaign.get("id")]
        campaign_keywords = [row for row in keyword_rows or []
                             if row.get("campaign_id") == campaign.get("id")]
        campaign_pages = [row for row in landing_pages or []
                          if campaign.get("id") in row.get("campaigns", [])]
        context = negatives.audit_contexts(campaign_ads, campaign_keywords, campaign_pages)
        conflicts = negatives.conflicts(actual, context)
        for group in groups or []:
            if group.get("campaign_id") != campaign.get("id"):
                continue
            group_context = negatives.audit_contexts(
                [row for row in campaign_ads if row.get("ad_group_id") == group.get("id")],
                [row for row in campaign_keywords if row.get("ad_group_id") == group.get("id")],
                [],
            )
            conflicts.extend(negatives.conflicts(
                group.get("negative_keywords") or [], group_context,
            ))
        if conflicts:
            findings.append(_finding(
                "search.negative_context_conflict", policy.WARNING,
                "Минус-фразы пересекаются с предложением или семантикой. Это "
                "кандидаты на проверку, а не доказательство блокировки показов.",
                conflicts=conflicts,
            ))
        risky_present = sorted(actual_negatives & policy.RISKY_NEGATIVES)
        if risky_present:
            findings.append(_finding(
                "search.risky_negative_keywords",
                policy.WARNING,
                "Найдены общие слова, способные отсечь полезный информационный спрос.",
                items=risky_present,
            ))

    if channel_name in {"network", "mixed_search_network"}:
        network_ads = [row for row in ad_rows or []
                       if row.get("campaign_id") == campaign.get("id")
                       and row.get("state") != "ARCHIVED"
                       and row.get("type") == "RESPONSIVE_AD"]
        invalid_images = [
            {"ad_id": str(row.get("id")),
             "unique_images": len(creative.image_hashes(row.get("ad_image_hashes")))}
            for row in network_ads if not creative.image_count_ok(row.get("ad_image_hashes"))
        ]
        findings.append(_finding(
            "ads.network_image",
            policy.BLOCK if invalid_images else policy.PASS if network_ads else policy.MANUAL,
            "В каждом комбинаторном объявлении РСЯ требуется от 3 до 5 разных изображений.",
            invalid_ads=invalid_images, inspected_ads=len(network_ads),
        ))
        findings.append(_finding(
            "network.carousel",
            policy.MANUAL,
            "Карусель обязательна для каждого объявления РСЯ. Проверить сохранённые "
            "2–10 слайдов в интерфейсе; Direct API и AdImageHashes её не подтверждают.",
            ad_ids=[
                str(row["id"])
                for row in ad_rows or []
                if row.get("campaign_id") == campaign.get("id")
                and row.get("id") is not None
            ],
            api_supported=False,
            requirements=selected_policy["creative"]["network_carousel"],
        ))
        sites = campaign.get("excluded_sites") or []
        actual_sites = {str(value).strip().casefold() for value in sites}
        explicit_sites = selected_policy.get("network_exclusions") == "explicit_with_reason"
        expected_sites = set() if explicit_sites else {
            value.strip().casefold()
            for value in (*policy.load_snapshot("network_excluded_sites"), "AdsNative", "BidSwitch")
        }
        missing_sites = sorted(expected_sites - actual_sites)
        findings.append(_finding(
            "network.excluded_sites",
            policy.MANUAL if explicit_sites else policy.PASS if not missing_sites else policy.BLOCK,
            (
                "Исключения площадок задаются по данным клиента; проверить обоснование."
                if explicit_sites else "Базовый список исключённых площадок присутствует."
                if not missing_sites
                else "В кампании отсутствует часть базового списка исключённых площадок."
            ),
            actual_count=len(actual_sites),
            expected_count=len(expected_sites),
            missing_count=len(missing_sites),
            missing_preview=missing_sites[:30],
        ))

    counters = campaign.get("counter_ids") or []
    priority_goals = campaign.get("priority_goals") or []
    clicks_without_goals = not priority_goals and policy.is_maximum_clicks_strategy(
        campaign.get("bidding_strategy")
    )
    if clicks_without_goals:
        goal_message = "Максимум кликов: счётчик и бизнес-цели необязательны; цели не выбраны."
    elif counters and priority_goals:
        goal_message = "Счётчик и приоритетные бизнес-цели заданы."
    else:
        goal_message = "Не заданы счётчик Метрики и/или приоритетные бизнес-цели."
    findings.append(_finding(
        "goals.explicit_business_selection",
        policy.PASS if clicks_without_goals or (counters and priority_goals) else policy.BLOCK,
        goal_message,
        counter_ids=counters,
        priority_goals=priority_goals,
    ))
    findings.append(_finding(
        "maps.office_required",
        policy.MANUAL,
        "Наличие офиса нельзя определить из Direct API; при офисе нужна отдельная кампания Карт.",
        current_channel=channel_name,
    ))
    if bid_modifiers is not None:
        campaign_modifiers = [
            row
            for row in bid_modifiers
            if row.get("campaign_id") == campaign.get("id")
        ]
        under_18_excluded = any(
            row.get("type") == "DEMOGRAPHICS_ADJUSTMENT"
            and row.get("bid_modifier") == 0
            and (row.get("details") or {}).get("Age") == "AGE_0_17"
            and row.get("enabled", "YES") == "YES"
            for row in campaign_modifiers
        )
        findings.append(
            _finding(
                "targeting.age_18_plus",
                policy.PASS if under_18_excluded else policy.MANUAL,
                (
                    "Возраст 0–17 исключён корректировкой −100%."
                    if under_18_excluded
                    else "Возраст 0–17 не исключён; сопоставьте с утверждённым брифом."
                ),
            )
        )

    campaign_groups = [
        group for group in groups or [] if group.get("campaign_id") == campaign.get("id")
    ]
    groups_without_regions = [
        group.get("id") for group in campaign_groups if not group.get("region_ids")
    ]
    if campaign_groups:
        findings.append(_finding(
            "groups.region_ids",
            policy.BLOCK if groups_without_regions else policy.PASS,
            (
                "У части групп не заданы регионы."
                if groups_without_regions
                else "Во всех прочитанных группах заданы регионы."
            ),
            groups_count=len(campaign_groups),
            groups_without_regions=groups_without_regions,
        ))
        tracking_overrides = [
            group.get("id") for group in campaign_groups if group.get("tracking_params")
        ]
        if tracking_overrides:
            findings.append(_finding(
                "groups.tracking_override",
                policy.WARNING,
                "В группах заданы TrackingParams; проверьте конфликты с кампанией.",
                ad_group_ids=tracking_overrides,
            ))

    campaign_group_ids = {
        int(group["id"]) for group in campaign_groups if group.get("id") is not None
    }
    findings.append(_finding(
        "semantics.evidence_unavailable", policy.MANUAL,
        "API не хранит обоснование структуры и источники ключей. "
        "Для этой проверки нужен сохранённый semantic_review исходного плана; "
        "его отсутствие не доказывает ошибку существующей кампании.",
    ))
    if ad_rows is not None:
        findings.extend(_audit_ads(campaign.get("id"), ad_rows))
    if (
        keyword_rows is not None
        and channel_name
        in {"search", "maps", "mixed_search_maps", "mixed_search_network"}
    ):
        findings.extend(
            _audit_keywords(
                campaign.get("id"), keyword_rows or [], campaign_group_ids
            )
        )

    groups_by_id = {group.get("id"): group for group in campaign_groups}
    url_checks = []
    for page in landing_pages or []:
        if campaign.get("id") not in page.get("campaigns", []):
            continue
        relevant_groups = [
            groups_by_id[group_id]
            for group_id in page.get("ad_groups", [])
            if group_id in groups_by_id
        ] or [None]
        for group in relevant_groups:
            effective = tracking.effective_params(
                page.get("base_url") or page.get("url"),
                campaign.get("tracking_params"),
                group.get("tracking_params") if group else None,
            )
            url_checks.append({
                "url": page.get("url"),
                "ad_group_id": group.get("id") if group else None,
                **effective,
            })
    effective_pages = [p for p in landing_pages or []
                       if campaign.get("id") in p.get("campaigns", [])
                       and p.get("effective_url_check")]
    needs_urls = any(a.get("href") or a.get("sitelink_set_id") for a in ad_rows or []
                     if a.get("campaign_id") == campaign.get("id"))
    if effective_pages or needs_urls:
        checked_summary = link_checks.summary(effective_pages)
        verified = bool(effective_pages) and checked_summary["all_ok"]
        findings.append(_finding(
            "landing.effective_urls", policy.PASS if verified else policy.BLOCK,
            "Конечные URL с метками проверены, включая быстрые ссылки и мобильный вариант."
            if verified else "Конечные URL с метками недоступны или проверены не полностью. "
            "Успешная проверка Href без меток недостаточна.",
            checks=checked_summary,
        ))
    relevant_site_checks = [
        page.get("site_check")
        for page in landing_pages or []
        if campaign.get("id") in page.get("campaigns", []) and page.get("site_check")
    ]
    if relevant_site_checks:
        unavailable = [
            row.get("url") for row in relevant_site_checks if not row.get("ok")
        ]
        findings.append(
            _finding(
                "landing.http",
                policy.BLOCK if unavailable else policy.PASS,
                (
                    "Все посадочные страницы доступны."
                    if not unavailable
                    else "Часть посадочных страниц недоступна."
                ),
                urls=unavailable,
            )
        )
        counter_optional = clicks_without_goals and not counters
        missing_counter = [
            row.get("url")
            for row in relevant_site_checks
            if not counter_optional and row.get("ok")
            and not set(counters).intersection(row.get("counter_ids") or [])
        ]
        findings.append(
            _finding(
                "landing.metrika_counter",
                policy.MANUAL if missing_counter else policy.PASS,
                (
                    "Максимум кликов без выбранных целей: счётчик на сайте необязателен."
                    if counter_optional
                    else "Кампанийный счётчик найден на посадочных страницах."
                    if not missing_counter
                    else "Счётчик не найден в статическом HTML: "
                    "проверьте загрузку через GTM/JavaScript."
                ),
                urls=missing_counter,
            )
        )
        missing_action = [
            row.get("url")
            for row in relevant_site_checks
            if row.get("ok")
            and not any((row.get("conversion_actions") or {}).values())
        ]
        findings.append(
            _finding(
                "landing.conversion_action",
                policy.WARNING if missing_action else policy.PASS,
                (
                    "На страницах найдены форма, телефон или email."
                    if not missing_action
                    else "На части страниц не найдено явное контактное действие."
                ),
                urls=missing_action,
            )
        )
        seo_missing = [
            {
                "url": row.get("url"),
                "missing": [
                    name
                    for name, value in {
                        "title": row.get("title"),
                        "h1": row.get("h1"),
                        "meta_description": row.get("meta_description"),
                        "canonical": row.get("canonical"),
                        "viewport": row.get("viewport"),
                    }.items()
                    if not value
                ],
            }
            for row in relevant_site_checks
            if row.get("ok")
            and not all(
                (
                    row.get("title"),
                    row.get("h1"),
                    row.get("meta_description"),
                    row.get("canonical"),
                    row.get("viewport"),
                )
            )
        ]
        findings.append(
            _finding(
                "landing.technical_basics",
                policy.WARNING if seo_missing else policy.PASS,
                (
                    "Базовые элементы посадочных страниц заполнены."
                    if not seo_missing
                    else "На посадочных страницах отсутствуют отдельные базовые элементы."
                ),
                pages=seo_missing,
            )
        )
    if url_checks and any(check["conflicts"] for check in url_checks):
        findings.append(_finding(
            "tracking.url_conflicts",
            policy.WARNING,
            "В ссылке объявления и TrackingParams есть конфликтующие параметры.",
            urls=[check["url"] for check in url_checks if check["conflicts"]],
        ))

    counts = Counter(finding["status"] for finding in findings)
    return {
        "campaign": campaign,
        "channel": channel,
        "tracking_profile": tracking_check,
        "landing_page_checks": url_checks,
        "findings": findings,
        "summary": {status: counts.get(status, 0) for status in policy.STATUSES},
        "ready": counts.get(policy.BLOCK, 0) == 0,
    }


def audit_payload(
    settings_payload: dict[str, Any],
    *,
    policy_name: str = "agency_default_v1",
    landing_pages: list[dict[str, Any]] | None = None,
    groups: list[dict[str, Any]] | None = None,
    ad_rows: list[dict[str, Any]] | None = None,
    keyword_rows: list[dict[str, Any]] | None = None,
    bid_modifiers: list[dict[str, Any]] | None = None,
    approved_budget_campaign_ids: set[int] | None = None,
) -> dict[str, Any]:
    selected = policy.get(policy_name)
    audited = [
        audit_campaign(
            item,
            selected_policy=selected,
            landing_pages=landing_pages,
            groups=groups,
            ad_rows=ad_rows,
            keyword_rows=keyword_rows,
            bid_modifiers=bid_modifiers,
            approved_budget_campaign_ids=approved_budget_campaign_ids,
        )
        for item in settings_payload.get("campaigns", [])
    ]
    totals = Counter(
        finding["status"]
        for campaign in audited
        for finding in campaign["findings"]
    )
    return {
        "client_login": settings_payload.get("client_login"),
        "policy": {"name": selected["name"], "version": selected["version"]},
        "generated_at": datetime.now(UTC).isoformat(),
        "campaigns": audited,
        "campaigns_count": len(audited),
        "summary": {status: totals.get(status, 0) for status in policy.STATUSES},
        "ready": totals.get(policy.BLOCK, 0) == 0 and not settings_payload.get("truncated"),
        "source_truncated": bool(settings_payload.get("truncated")),
    }


async def read_and_audit(
    api: Any,
    client_login: str,
    *,
    campaign_ids: list[int] | None = None,
    include_archived: bool = False,
    include_landing_pages: bool = True,
    limit: int = 50,
    policy_name: str = "agency_default_v1",
    approved_budget_campaign_ids: list[int] | None = None,
) -> dict[str, Any]:
    settings_payload = await campaigns.read_settings(
        api,
        client_login,
        campaign_ids=campaign_ids,
        include_archived=include_archived,
        limit=limit,
    )
    landing_pages: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    ad_rows: list[dict[str, Any]] = []
    keyword_rows: list[dict[str, Any]] = []
    bid_modifiers: list[dict[str, Any]] = []
    incomplete = completeness.sources(campaigns=settings_payload)
    supported_campaigns = [row for row in settings_payload["campaigns"]
                           if row.get("type") in {None, "TEXT_CAMPAIGN", "UNIFIED_CAMPAIGN"}]
    if supported_campaigns:
        selected_ids = [
            int(item["id"])
            for item in supported_campaigns
            if item.get("id") is not None
        ]
        ads_payload = await ads.read(
            api,
            client_login,
            campaign_ids=selected_ids,
            include_archived=include_archived,
            preview_limit=None,
            limit=10000,
        )
        ad_rows = ads_payload.get("ads", [])
        sitelink_sets = await assets.enrich_ads(api, client_login, ad_rows)
        groups_payload = await adgroups.read(
            api,
            client_login,
            campaign_ids=selected_ids,
            limit=10000,
        )
        groups = groups_payload.get("groups", [])
        if include_landing_pages:
            landing_pages = await link_checks.check_live(
                api, client_login, supported_campaigns, groups, ad_rows,
                sitelink_sets=sitelink_sets,
            )
        keywords_payload = await keywords.read(
            api,
            client_login,
            campaign_ids=selected_ids,
            limit=10000,
        )
        keyword_rows = keywords_payload.get("keywords", [])
        account_payload = await account.read(
            api,
            client_login,
            sections=["bid_modifiers"],
            campaign_ids=selected_ids,
        )
        bid_modifiers = (
            account_payload.get("bid_modifiers", {}).get("items", [])
            if not account_payload.get("bid_modifiers", {}).get("error")
            else []
        )
        incomplete.extend(completeness.sources(
            ads=ads_payload, adgroups=groups_payload, keywords=keywords_payload,
            bid_modifiers=account_payload.get("bid_modifiers", {}),
        ))
    payload = audit_payload(
        settings_payload,
        policy_name=policy_name,
        landing_pages=landing_pages,
        groups=groups,
        ad_rows=ad_rows,
        keyword_rows=keyword_rows,
        bid_modifiers=bid_modifiers,
        approved_budget_campaign_ids={
            int(value) for value in approved_budget_campaign_ids or []
        },
    )
    payload["landing_pages_included"] = include_landing_pages
    payload["adgroups_included"] = True
    payload["ads_included"] = True
    payload["keywords_included"] = True
    payload["bid_modifiers_included"] = True
    payload["incomplete_sources"] = incomplete
    payload["source_truncated"] = bool(incomplete)
    payload["data_complete"] = not incomplete
    if incomplete:
        payload["ready"] = False
        payload["findings"] = [_finding(
            "coverage.incomplete_data", policy.BLOCK,
            "Часть данных API не прочитана; аудит не подтверждает полную настройку кабинета.",
            sources=incomplete,
        )]
        payload["summary"][policy.BLOCK] += 1
    return payload


def persist(payload: dict[str, Any], out_dir: Path) -> Path:
    """Сохранить воспроизводимый JSON-аудит рядом с TSV-отчётами."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    login = store.safe_stem(str(payload.get("client_login") or "client"))
    path = out_dir / f"audit_{login}_{stamp}.json"
    path.write_text(
        json.dumps(wire(payload), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path
