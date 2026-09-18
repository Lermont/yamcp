"""Безопасная сборка legacy TextCampaign с актуальными ResponsiveAd.

Модели неудобно вручную протаскивать ID между Campaigns.add, AdGroups.add,
Ads.add и Keywords.add. Этот модуль принимает дерево объектов, проверяет его,
Старые preview/apply удалены; запись выполняется только через типизированный executor.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from . import limits, policy, semantics

MAX_BATCH = 1000

# Директ сверяет StartDate со своим «сегодня», а оно московское, а не то, что
# на машине с сервером. Для кабинета из Владивостока или Калининграда разница
# ровно в те сутки, из-за которых валидный план получает отказ (или наоборот).
# Москва с 2014 года часы не переводит, поэтому фиксированного смещения хватает.
MOSCOW = timezone(timedelta(hours=3))

RESPONSIVE_AD_FIELDS = {
    "Titles",
    "Texts",
    "Href",
    "AgeLabel",
    "DisplayUrlPath",
    "AdImageHashes",
    "SitelinkSetId",
    "AdExtensionIds",
    "VideoExtensionIds",
    "PriceExtension",
    "BusinessId",
    "ErirAdDescription",
}
def moscow_today() -> date:
    """Сегодняшняя дата по часам Директа."""
    return datetime.now(MOSCOW).date()


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} должен быть непустой строкой")
    return value.strip()


def _validate_responsive_ad(
    value: dict[str, Any], prefix: str, *, partial: bool = False,
) -> None:
    unknown = sorted(set(value) - RESPONSIVE_AD_FIELDS)
    if unknown:
        raise ValueError(
            f"{prefix}.ResponsiveAd содержит неизвестные поля: {', '.join(unknown)}"
        )

    if not partial or "Titles" in value:
        titles = value.get("Titles")
        if not isinstance(titles, list) or not 3 <= len(titles) <= 7:
            raise ValueError(
                f"{prefix}.ResponsiveAd.Titles должен содержать от 3 до 7 заголовков"
            )
        normalized_titles = []
        for index, raw in enumerate(titles):
            title = _nonempty_string(raw, f"{prefix}.ResponsiveAd.Titles[{index}]")
            normalized_titles.append(title)
            limits.validate_title(title, f"{prefix}.ResponsiveAd.Titles[{index}]")
        if len({title.casefold() for title in normalized_titles}) != len(titles):
            raise ValueError(f"{prefix}.ResponsiveAd.Titles должны быть уникальными")
        utilization = policy.responsive_title_utilization(normalized_titles)
        if not utilization["passes"]:
            raise ValueError(
                f"{prefix}.ResponsiveAd.Titles: минимум "
                f"{utilization['required_near_limit_count']} заголовка должны "
                f"использовать 45–56 символов"
            )

    if not partial or "Texts" in value:
        texts = value.get("Texts")
        if not isinstance(texts, list) or len(texts) != 3:
            raise ValueError(
                f"{prefix}.ResponsiveAd.Texts должен содержать ровно 3 текста"
            )
        normalized_texts = []
        for index, raw in enumerate(texts):
            text = _nonempty_string(raw, f"{prefix}.ResponsiveAd.Texts[{index}]")
            normalized_texts.append(text)
            limits.validate_text(text, f"{prefix}.ResponsiveAd.Texts[{index}]")
        if len({text.casefold() for text in normalized_texts}) != len(texts):
            raise ValueError(f"{prefix}.ResponsiveAd.Texts должны быть уникальными")

    href = value.get("Href")
    business_id = value.get("BusinessId")
    if not partial and href is None and business_id is None:
        raise ValueError(f"{prefix}.ResponsiveAd требует Href или BusinessId")
    if href is not None:
        href = _nonempty_string(href, f"{prefix}.ResponsiveAd.Href")
        parsed = urlsplit(href)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or len(href) > 1024
        ):
            raise ValueError(
                f"{prefix}.ResponsiveAd.Href должен быть полным HTTP(S) URL до 1024 знаков"
            )
    if business_id is not None and int(business_id) <= 0:
        raise ValueError(f"{prefix}.ResponsiveAd.BusinessId должен быть положительным")

    display_path = value.get("DisplayUrlPath")
    if display_path is not None:
        display_path = str(display_path)
        if not partial and href is None:
            raise ValueError(
                f"{prefix}.ResponsiveAd.DisplayUrlPath допустим только с Href"
            )
        limits.validate_display_path(display_path, f"{prefix}.ResponsiveAd.DisplayUrlPath")


def _normalize_ad(ad: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Validate the only current ad creation format."""
    if "TextAd" in ad:
        raise ValueError(
            f"{prefix}.TextAd больше не поддерживается для создания; "
            "передайте ResponsiveAd с 3–7 Titles и ровно 3 Texts"
        )
    responsive_ad = ad.get("ResponsiveAd")
    if not isinstance(responsive_ad, dict):
        raise ValueError(f"{prefix}.ResponsiveAd должен быть JSON-объектом")
    responsive_ad = deepcopy(responsive_ad)

    _validate_responsive_ad(responsive_ad, prefix)
    normalized = {
        key: deepcopy(value)
        for key, value in ad.items()
        if key not in {"TextAd", "ResponsiveAd"}
    }
    normalized["ResponsiveAd"] = responsive_ad
    return normalized


def plan_hash(
    client_login: str,
    campaign: dict[str, Any],
    ad_groups: list[dict[str, Any]],
) -> str:
    """SHA-256 нормализованного raw-плана, включая целевой логин."""
    normalized_campaign, normalized_groups = normalize_plan(campaign, ad_groups)
    raw = json.dumps(
        {
            "client_login": client_login,
            "campaign": normalized_campaign,
            "ad_groups": normalized_groups,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_plan(
    campaign: dict[str, Any],
    ad_groups: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Проверить план и вернуть независимую нормализованную копию."""
    if not isinstance(campaign, dict):
        raise ValueError("campaign должен быть JSON-объектом")
    campaign = deepcopy(campaign)
    groups = deepcopy(ad_groups)

    if not str(campaign.get("Name", "")).strip():
        raise ValueError("campaign.Name обязателен")
    if "Id" in campaign:
        raise ValueError("campaign.Id задавать нельзя: инструмент создаёт новую кампанию")
    if "TextCampaign" not in campaign or not isinstance(campaign["TextCampaign"], dict):
        raise ValueError("поддерживается новая текстово-графическая кампания: задайте TextCampaign")
    text_campaign = campaign["TextCampaign"]
    if not text_campaign.get("BiddingStrategy") and not text_campaign.get("PackageBiddingStrategy"):
        raise ValueError(
            "campaign.TextCampaign требует BiddingStrategy или PackageBiddingStrategy"
        )
    counters = (text_campaign.get("CounterIds") or {}).get("Items") or []
    priority_goals = (text_campaign.get("PriorityGoals") or {}).get("Items") or []
    clicks_without_goals = not priority_goals and policy.is_maximum_clicks_strategy(
        text_campaign.get("BiddingStrategy")
    )
    if not counters and not clicks_without_goals:
        raise ValueError("campaign.TextCampaign.CounterIds требует счётчик Метрики")
    if not priority_goals and not clicks_without_goals:
        raise ValueError(
            "campaign.TextCampaign.PriorityGoals требует приоритетную бизнес-цель"
        )
    unsupported_types = {
        "UnifiedCampaign", "MobileAppCampaign", "DynamicTextCampaign",
        "CpmBannerCampaign", "SmartCampaign",
    }
    if unsupported_types.intersection(campaign):
        raise ValueError("в campaign нельзя смешивать TextCampaign с другим типом кампании")

    raw_start = campaign.get("StartDate")
    if not isinstance(raw_start, str):
        raise ValueError("campaign.StartDate обязателен в формате YYYY-MM-DD")
    try:
        start = date.fromisoformat(raw_start)
    except ValueError as exc:
        raise ValueError("campaign.StartDate должен иметь формат YYYY-MM-DD") from exc
    if start < (today or moscow_today()):
        raise ValueError("campaign.StartDate не может быть в прошлом")

    if not isinstance(groups, list) or not groups:
        raise ValueError("ad_groups должен содержать хотя бы одну группу")
    if len(groups) > MAX_BATCH:
        raise ValueError(f"за один запуск можно создать не более {MAX_BATCH} групп")

    for group_index, group in enumerate(groups):
        prefix = f"ad_groups[{group_index}]"
        if not isinstance(group, dict):
            raise ValueError(f"{prefix} должен быть JSON-объектом")
        if "CampaignId" in group:
            raise ValueError(f"{prefix}.CampaignId задавать нельзя")
        if not str(group.get("Name", "")).strip():
            raise ValueError(f"{prefix}.Name обязателен")
        region_ids = group.get("RegionIds")
        if not isinstance(region_ids, list) or not region_ids:
            raise ValueError(f"{prefix}.RegionIds должен содержать хотя бы один регион")

        ads = group.get("Ads")
        if not isinstance(ads, list) or not ads:
            raise ValueError(f"{prefix}.Ads должен содержать хотя бы одно объявление")
        if len(ads) > policy.RESPONSIVE_ADS_MAX_NON_ARCHIVED:
            raise ValueError(f"{prefix}.Ads: не более 3 новых комбинаторных объявлений в группе")
        normalized_ads: list[dict[str, Any]] = []
        for ad_index, ad in enumerate(ads):
            ad_prefix = f"{prefix}.Ads[{ad_index}]"
            if not isinstance(ad, dict):
                raise ValueError(f"{ad_prefix} должен быть JSON-объектом")
            if "AdGroupId" in ad:
                raise ValueError(f"{ad_prefix}.AdGroupId задавать нельзя")
            normalized_ads.append(_normalize_ad(ad, ad_prefix))
        group["Ads"] = normalized_ads

        keywords = group.get("Keywords", [])
        if not isinstance(keywords, list):
            raise ValueError(f"{prefix}.Keywords должен быть массивом")
        normalized_keywords: list[dict[str, Any]] = []
        for keyword_index, keyword in enumerate(keywords):
            kw_prefix = f"{prefix}.Keywords[{keyword_index}]"
            if isinstance(keyword, str):
                keyword = {"Keyword": keyword}
            if not isinstance(keyword, dict):
                raise ValueError(f"{kw_prefix} должен быть строкой или JSON-объектом")
            if "AdGroupId" in keyword:
                raise ValueError(f"{kw_prefix}.AdGroupId задавать нельзя")
            if not str(keyword.get("Keyword", "")).strip():
                raise ValueError(f"{kw_prefix}.Keyword обязателен")
            normalized_keywords.append(keyword)
        group["Keywords"] = normalized_keywords
        semantics.validate_keywords(normalized_keywords, f"{prefix}.Keywords")

    return campaign, groups
