"""Типизированный бизнес-план и компилятор нативных UnifiedCampaign v501.

На входе — понятные поля в snake_case. На выходе — отдельные кампании Поиска,
РСЯ и, при наличии офиса, Карт с точными payload API. Модуль ничего не пишет в
Директ: результат предназначен для preview и последующего guarded executor.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import (
    api_units,
    audience_setup,
    campaign_setup,
    creative,
    launch_checks,
    limits,
    negatives,
    phrases,
    policy,
    products,
    profiles,
    semantics,
    store,
)
from .identifiers import parse_id, wire

CHANNEL_LABELS = {"search": "Поиск", "network": "РСЯ", "maps": "Карты", "product": "Товарная"}
CompiledAd = tuple[dict[str, Any], list[dict[str, Any]]]
CompiledGroup = tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]
REQUIRED_MANUAL_CHECKS = (
    "goals_reviewed",
    "regions_verified",
    "landing_pages_verified",
)
TOP_LEVEL_FIELDS = {
    "policy_name",
    "profile_context",
    "semantic_plan",
    "name",
    "start_date",
    "end_date",
    "schedule",
    "time_zone",
    "settings",
    "attribution_model",
    "age_min",
    "age_max",
    "weekly_budget",
    "client_budget",
    "business_profiles",
    "region_ids",
    "counter_ids",
    "priority_goals",
    "goal_catalog_campaign_id",
    "allow_unverified_goals",
    "office",
    "allow_extended_geo",  # Deprecated input, accepted only for compatibility.
    "manual_checks",
    "tracking_profile",
    "approved_risky_negatives",
    "additional_negative_keywords",
    "negative_keyword_policy",
    "additional_excluded_sites",
    "blocked_ips",
    "channels",
    "campaign_template",
    "campaign_variants",
    "sitelink_set_id",
}
CHANNEL_FIELDS = {
    "product_source",
    "group_count_reason",
    "groups",
    "strategy",
    "name",
    "name_suffix",
    "region_ids",
    "weekly_budget",
    "budget_approved",
    "sitelink_set_id",
}
CAMPAIGN_VARIANT_FIELDS = (CHANNEL_FIELDS - {"groups"}) | {"channel"}
GROUP_FIELDS = {
    "semantic",
    "name",
    "region_ids",
    "ads",
    "keywords",
    "negative_keywords",
    "offer_retargeting",
    "negative_keyword_shared_set_ids",
    "retargeting_rules",
    "audience_interest_ids",
    "audience_priority",
    "autotargeting",
    "sitelink_set_id",
}
AD_FIELDS = {
    "action_button",
    "hypothesis",
    "titles",
    "texts",
    "href",
    "display_url_path",
    "ad_image_hash",
    "ad_image_hashes",
    "sitelink_set_id",
    "ad_extension_ids",
    "video_extension_ids",
    "business_id",
    "age_label",
    "price_extension",
    "erir_ad_description",
}
MANUAL_FIELDS = set(REQUIRED_MANUAL_CHECKS) | {
    "acknowledged_warning_rules",
    "extended_geo_verified",  # Deprecated input, not a manual requirement.
}
KEYWORD_FIELDS = {"Keyword", "UserParam1", "UserParam2", "StrategyPriority"}
AUTOTARGETING_CATEGORY_FIELDS = {
    "exact": "Exact",
    "narrow": "Narrow",
    "alternative": "Alternative",
    "accessory": "Accessory",
    "broader": "Broader",
}
AUTOTARGETING_BRAND_FIELDS = {
    "without_brands": "WithoutBrands",
    "with_advertiser_brand": "WithAdvertiserBrand",
    "with_competitors_brand": "WithCompetitorsBrand",
}
SAFE_AUTOTARGETING = {
    "categories": {
        "exact": True,
        "narrow": True,
        "alternative": False,
        "accessory": False,
        "broader": False,
    },
    "brand_options": {
        "without_brands": True,
        "with_advertiser_brand": False,
        "with_competitors_brand": False,
    },
}


def _reject_unknown(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{field} содержит неизвестные поля: {', '.join(unknown)}")


def _required(value: dict[str, Any], key: str, prefix: str = "bundle") -> Any:
    if key not in value:
        raise ValueError(f"{prefix}.{key} обязателен")
    return value[key]


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} должен быть непустой строкой")
    return value.strip()


def _ids(value: Any, field: str) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} должен содержать хотя бы один ID")
    result = []
    for item in value:
        number = int(item)
        if abs(number) > 10**12:
            raise ValueError(f"{field} содержит некорректный ID {item!r}")
        result.append(number)
    if 0 in result and len(result) > 1:
        raise ValueError(f"{field}: регион 0 нельзя смешивать с другими регионами")
    if all(number < 0 for number in result):
        raise ValueError(f"{field} не может состоять только из исключённых регионов")
    return result


def _micros(value: Any, field: str) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} должен быть числом") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{field} должен быть положительным конечным числом")
    micros = amount * 1_000_000
    if micros != micros.to_integral_value():
        raise ValueError(f"{field} содержит дробь меньше одной микроединицы")
    return int(micros)


def _weekly_budget(
    value: Any,
    channel: str,
    default_weekly_budget: float | None,
) -> tuple[int, Decimal, bool]:
    raw = value.get(channel) if isinstance(value, dict) else value
    used_default = raw is None and default_weekly_budget is not None
    if used_default:
        raw = default_weekly_budget
    if raw is None:
        raise ValueError(f"bundle.weekly_budget.{channel} обязателен")
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"bundle.weekly_budget.{channel} должен быть числом") from exc
    return _micros(raw, f"bundle.weekly_budget.{channel}"), amount, used_default


def _tracking_string(profile_name: str) -> str:
    profiles = policy.get()["tracking_profiles"]
    if profile_name not in profiles:
        known = ", ".join(sorted(profiles))
        raise ValueError(f"Неизвестный tracking_profile {profile_name!r}. Доступны: {known}")
    params = profiles[profile_name]["params"]
    # TrackingParams — строка GET-параметров, а не фрагмент URL. Вопросительный
    # знак Директ добавляет сам при объединении с Href.
    return "&".join(f"{key}={value}" for key, value in params.items())


def _negative_phrases(
    values: list[str],
    field: str,
    *,
    maximum_total_length: int,
) -> list[str]:
    """Validate Direct's common negative-keyword constraints."""
    normalized: list[str] = []
    counted_length = 0
    for index, raw in enumerate(values):
        phrase = _nonempty_string(raw, f"{field}[{index}]")
        if phrase.startswith("-"):
            raise ValueError(f"{field}[{index}] не должен начинаться с минуса")
        phrases.validate(phrase, f"{field}[{index}]", negative=True)
        words = phrase.split()
        if len(words) > 7:
            raise ValueError(f"{field}[{index}] содержит больше 7 слов")
        for word in words:
            # Operators and hyphens do not count toward the API word limit.
            counted_word = re.sub(r'[!+\-\[\]()"#]', "", word)
            if len(counted_word) > 35:
                raise ValueError(
                    f"{field}[{index}] содержит слово длиннее 35 символов"
                )
        counted_length += len(re.sub(r'[\s!+\-\[\]()"#]', "", phrase))
        normalized.append(phrase)
    if counted_length > maximum_total_length:
        raise ValueError(
            f"{field}: суммарная длина превышает {maximum_total_length} символов"
        )
    return normalized


def _negative_keywords(bundle: dict[str, Any]) -> list[str]:
    selection = negatives.select(bundle)
    result = [item["phrase"] for item in selection["selected"]]
    return _negative_phrases(
        result,
        "bundle.additional_negative_keywords",
        maximum_total_length=20_000,
    )


def _excluded_sites(bundle: dict[str, Any]) -> list[str]:
    selected = policy.get(bundle.get("policy_name", "agency_default_v1"))
    values = ([] if selected.get("network_exclusions") == "explicit_with_reason" else
              list(policy.load_snapshot("network_excluded_sites")) + ["AdsNative", "BidSwitch"])
    additional = bundle.get("additional_excluded_sites") or []
    if not isinstance(additional, list):
        raise ValueError("bundle.additional_excluded_sites должен быть массивом")
    values.extend(
        _nonempty_string(value, "bundle.additional_excluded_sites[]")
        for value in additional
    )
    # Bare domains and reverse-DNS application IDs cannot be distinguished
    # reliably. Preserve spelling for both; app IDs are case-sensitive.
    result = list(dict.fromkeys(value.strip() for value in values))
    if len(result) > 1000:
        raise ValueError("Итоговый список ExcludedSites не может превышать 1000 элементов")
    return result


def _responsive_ad(source: dict[str, Any], prefix: str, channel: str) -> CompiledAd:
    _reject_unknown(source, AD_FIELDS, prefix)
    if "hypothesis" in source:
        _nonempty_string(source["hypothesis"], f"{prefix}.hypothesis")
    raw_titles = _required(source, "titles", prefix)
    if not isinstance(raw_titles, list) or not 3 <= len(raw_titles) <= 7:
        raise ValueError(f"{prefix}.titles должен содержать от 3 до 7 заголовков")
    titles = [
        _nonempty_string(value, f"{prefix}.titles[{index}]")
        for index, value in enumerate(raw_titles)
    ]
    if len({title.casefold() for title in titles}) != len(titles):
        raise ValueError(f"{prefix}.titles должны быть уникальными")
    utilization = policy.responsive_title_utilization(titles)
    if not utilization["passes"]:
        raise ValueError(
            f"{prefix}.titles: минимум "
            f"{utilization['required_near_limit_count']} заголовка должны "
            f"использовать 45–56 символов"
        )

    raw_texts = _required(source, "texts", prefix)
    if not isinstance(raw_texts, list) or len(raw_texts) != 3:
        raise ValueError(f"{prefix}.texts должен содержать ровно 3 текста")
    texts = [
        _nonempty_string(value, f"{prefix}.texts[{index}]")
        for index, value in enumerate(raw_texts)
    ]
    if len({text.casefold() for text in texts}) != len(texts):
        raise ValueError(f"{prefix}.texts должны быть уникальными")

    href = None
    if source.get("href") is not None:
        href = _nonempty_string(source["href"], f"{prefix}.href")
        parsed_href = urlsplit(href)
        if (
            parsed_href.scheme not in {"http", "https"}
            or not parsed_href.netloc
            or len(href) > 1024
        ):
            raise ValueError(
                f"{prefix}.href должен быть полным HTTP(S) URL до 1024 знаков"
            )
    if href is None and source.get("business_id") is None:
        raise ValueError(f"{prefix}: требуется href или business_id")
    for index, title in enumerate(titles):
        limits.validate_title(title, f"{prefix}.titles[{index}]")
    for index, text in enumerate(texts):
        limits.validate_text(text, f"{prefix}.texts[{index}]")
    if source.get("display_url_path") is not None:
        display_path = str(source["display_url_path"])
        if href is None:
            raise ValueError(f"{prefix}.display_url_path допустим только с href")
        limits.validate_display_path(display_path, f"{prefix}.display_url_path")
    if source.get("sitelink_set_id") is not None and href is None:
        raise ValueError(f"{prefix}.sitelink_set_id допустим только с href")

    responsive_ad: dict[str, Any] = {
        "Titles": titles,
        "Texts": texts,
    }
    if href is not None:
        responsive_ad["Href"] = href
    mapping = {
        "display_url_path": "DisplayUrlPath",
        "sitelink_set_id": "SitelinkSetId",
        "business_id": "BusinessId",
        "age_label": "AgeLabel",
    }
    for source_name, api_name in mapping.items():
        if source.get(source_name) is not None:
            value = source[source_name]
            if source_name in {"sitelink_set_id", "business_id"}:
                value = parse_id(value, f"{prefix}.{source_name}")
                if value <= 0:
                    raise ValueError(f"{prefix}.{source_name} должен быть положительным")
            responsive_ad[api_name] = value
    if "AgeLabel" in responsive_ad and responsive_ad["AgeLabel"] not in {
        "AGE_0", "AGE_6", "AGE_12", "AGE_16", "AGE_18",
        "MONTHS_1", "MONTHS_2", "MONTHS_3", "MONTHS_4", "MONTHS_5", "MONTHS_6",
        "MONTHS_7", "MONTHS_8", "MONTHS_9", "MONTHS_10", "MONTHS_11", "MONTHS_12",
    }:
        raise ValueError(f"{prefix}.age_label: неизвестная возрастная маркировка")

    if source.get("ad_image_hashes") is not None and source.get("ad_image_hash") is not None:
        raise ValueError(
            f"{prefix}: используйте ad_image_hashes либо ad_image_hash, не вместе"
        )
    raw_image_hashes = source.get("ad_image_hashes")
    if raw_image_hashes is None and source.get("ad_image_hash") is not None:
        raw_image_hashes = [source["ad_image_hash"]]
    if raw_image_hashes is not None:
        if not isinstance(raw_image_hashes, list) or not 1 <= len(raw_image_hashes) <= 5:
            raise ValueError(f"{prefix}.ad_image_hashes: от 1 до 5 хэшей")
        responsive_ad["AdImageHashes"] = [
            _nonempty_string(value, f"{prefix}.ad_image_hashes[]")
            for value in raw_image_hashes
        ]
    if source.get("ad_extension_ids") is not None:
        if not isinstance(source["ad_extension_ids"], list):
            raise ValueError(f"{prefix}.ad_extension_ids должен быть массивом")
        extension_ids = [
            parse_id(value, "ad_extension_ids") for value in source["ad_extension_ids"]
        ]
        if len(extension_ids) > 50 or any(value <= 0 for value in extension_ids):
            raise ValueError(
                f"{prefix}.ad_extension_ids: от 1 до 50 положительных ID"
            )
        if extension_ids:
            responsive_ad["AdExtensionIds"] = extension_ids
    if source.get("video_extension_ids") is not None:
        if not isinstance(source["video_extension_ids"], list):
            raise ValueError(f"{prefix}.video_extension_ids должен быть массивом")
        video_ids = [
            parse_id(value, "video_extension_ids") for value in source["video_extension_ids"]
        ]
        if not 1 <= len(video_ids) <= 6 or any(value <= 0 for value in video_ids):
            raise ValueError(
                f"{prefix}.video_extension_ids: от 1 до 6 положительных ID"
            )
        responsive_ad["VideoExtensionIds"] = video_ids

    if source.get("price_extension") is not None:
        price = source["price_extension"]
        if not isinstance(price, dict):
            raise ValueError(f"{prefix}.price_extension должен быть объектом")
        _reject_unknown(price, {"price", "old_price", "qualifier", "currency"}, prefix)
        amount = _micros(price.get("price"), f"{prefix}.price_extension.price")
        old = (_micros(price["old_price"], f"{prefix}.price_extension.old_price")
               if "old_price" in price else None)
        if (amount > 10**16 or amount % 10000 or (old is not None
                and (old <= amount or old % 10000 or old > 10**16))):
            raise ValueError("price_extension: максимум два знака после запятой; old_price > price")
        qualifier = price.get("qualifier", "NONE")
        currency = price.get("currency")
        if qualifier not in {"FROM", "UP_TO", "NONE"} or currency not in {
            "RUB", "BYN", "CHF", "EUR", "KZT", "TRY", "UAH", "USD", "UZS"
        }:
            raise ValueError("price_extension: укажите допустимые qualifier и currency")
        responsive_ad["PriceExtension"] = {"Price": amount, "PriceQualifier": qualifier,
                                            "PriceCurrency": currency}
        if old is not None:
            responsive_ad["PriceExtension"]["OldPrice"] = old
    if source.get("erir_ad_description") is not None:
        description = _nonempty_string(source["erir_ad_description"], prefix)
        if len(description) > 1000:
            raise ValueError("erir_ad_description: не более 1000 символов")
        responsive_ad["ErirAdDescription"] = description
    findings = []
    button = creative.action_button(source.get("action_button"), href)
    if creative.needs_button(responsive_ad) and button is None:
        findings.append({
            "status": policy.BLOCK,
            "rule": "ads.action_button_selection",
            "message": f"{prefix}: выберите релевантную action_button (text, reason); "
                       "href по умолчанию основной, для контактов задайте destination=contacts "
                       "и проверенный URL страницы. Кнопку затем нужно сохранить в интерфейсе.",
        })
    if not source.get("sitelink_set_id"):
        findings.append({
            "status": policy.BLOCK if href else policy.WARNING,
            "rule": "ads.sitelinks",
            "message": f"{prefix}: задайте общий набор из 4–8 быстрых ссылок (рекомендуется 8).",
        })
    if len(titles) < policy.AGENCY_POLICY_V1["creative"]["responsive_titles_recommended"]:
        findings.append({
            "status": policy.MANUAL,
            "rule": "ads.title_diversity",
            "message": f"{prefix}: рекомендуется до 7 содержательно разных заголовков; "
                       "не добавляйте повторы ради количества.",
        })
    if not source.get("ad_extension_ids"):
        findings.append({
            "status": policy.WARNING,
            "rule": "ads.extensions",
            "message": f"{prefix}: уточнения/дополнения не заданы.",
        })
    if channel == "network" and not creative.image_count_ok(responsive_ad.get("AdImageHashes")):
        findings.append({
            "status": policy.BLOCK,
            "rule": "ads.network_image",
            "message": f"{prefix}: для комбинаторного объявления РСЯ требуется "
                       "от 3 до 5 разных изображений в ad_image_hashes. "
                       "Подготовьте или сгенерируйте минимум 3 самостоятельных креатива.",
            "evidence": {"unique_images": len(creative.image_hashes(
                responsive_ad.get("AdImageHashes")))},
        })
    if channel == "maps" and not source.get("business_id"):
        findings.append({
            "status": policy.BLOCK,
            "rule": "ads.maps_business",
            "message": (
                f"{prefix}: для Карт и списка организаций требуется BusinessId."
            ),
        })
    return {"ResponsiveAd": responsive_ad}, findings


def _autotargeting(
    source: Any, prefix: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compile explicit settings; omission uses the conservative agency default."""
    if source is None:
        source = deepcopy(SAFE_AUTOTARGETING)
    if not isinstance(source, dict):
        raise ValueError(f"{prefix}.autotargeting должен быть объектом")
    _reject_unknown(
        source, {"categories", "brand_options"}, f"{prefix}.autotargeting"
    )

    def compile_flags(
        raw: Any,
        mapping: dict[str, str],
        field: str,
        defaults: dict[str, bool],
    ) -> dict[str, str]:
        if raw is None:
            raw = defaults
        if not isinstance(raw, dict):
            raise ValueError(f"{field} должен быть объектом")
        _reject_unknown(raw, set(mapping), field)
        merged = {**defaults, **raw}
        if not all(isinstance(value, bool) for value in merged.values()):
            raise ValueError(f"{field} принимает только true или false")
        if not any(merged.values()):
            raise ValueError(f"{field}: должен быть включён хотя бы один вариант")
        return {mapping[key]: "YES" if merged[key] else "NO" for key in mapping}

    categories = compile_flags(
        source.get("categories"),
        AUTOTARGETING_CATEGORY_FIELDS,
        f"{prefix}.autotargeting.categories",
        SAFE_AUTOTARGETING["categories"],
    )
    brands = compile_flags(
        source.get("brand_options"),
        AUTOTARGETING_BRAND_FIELDS,
        f"{prefix}.autotargeting.brand_options",
        SAFE_AUTOTARGETING["brand_options"],
    )
    findings: list[dict[str, Any]] = []
    expanded = [
        name
        for name in ("Alternative", "Accessory", "Broader")
        if categories[name] == "YES"
    ]
    if expanded:
        findings.append(
            {
                "status": policy.WARNING,
                "rule": "autotargeting.expanded_categories",
                "message": f"{prefix}: включены расширяющие категории автотаргетинга.",
                "evidence": {"categories": expanded},
            }
        )
    if brands["WithCompetitorsBrand"] == "YES":
        findings.append(
            {
                "status": policy.WARNING,
                "rule": "autotargeting.competitor_brands",
                "message": f"{prefix}: включены запросы с брендами конкурентов.",
            }
        )
    return (
        {
            "Keyword": "---autotargeting",
            "AutotargetingSettings": {
                "Categories": categories,
                "BrandOptions": brands,
            },
        },
        findings,
    )


def _group(
    source: dict[str, Any],
    *,
    channel: str,
    default_region_ids: list[int],
    index: int,
    default_sitelink_set_id: int | None = None,
    product_source: dict | None = None,
) -> CompiledGroup:
    prefix = f"bundle.channels.{channel}.groups[{index}]"
    _reject_unknown(source, GROUP_FIELDS, prefix)
    name = _nonempty_string(_required(source, "name", prefix), f"{prefix}.name")
    if len(name) > 255:
        raise ValueError(f"{prefix}.name длиннее 255 символов")
    region_ids = (
        _ids(source["region_ids"], f"{prefix}.region_ids")
        if source.get("region_ids") is not None
        else list(default_region_ids)
    )
    raw_ads = _required(source, "ads", prefix)
    if not isinstance(raw_ads, list) or not raw_ads:
        raise ValueError(f"{prefix}.ads должен содержать объявления")
    if len(raw_ads) > (2 if channel == "product" else policy.RESPONSIVE_ADS_MAX_NON_ARCHIVED):
        raise ValueError(f"{prefix}.ads: не более 3 неархивных комбинаторных объявлений")
    findings = []

    compiled_ads = []
    for ad_index, source_ad in enumerate(raw_ads):
        if not isinstance(source_ad, dict):
            raise ValueError(f"{prefix}.ads[{ad_index}] должен быть объектом")
        source_ad = {"sitelink_set_id": source.get("sitelink_set_id", default_sitelink_set_id),
                     **source_ad}
        if channel == "product":
            compiled, ad_findings = products.ad(source_ad, product_source), []
        else:
            compiled, ad_findings = _responsive_ad(source_ad, f"{prefix}.ads[{ad_index}]", channel)
        compiled_ads.append(compiled)
        findings.extend(ad_findings)

    raw_keywords = source.get("keywords") or []
    if not isinstance(raw_keywords, list):
        raise ValueError(f"{prefix}.keywords должен быть массивом")
    keywords = []
    for keyword_index, raw in enumerate(raw_keywords):
        item = deepcopy(raw) if isinstance(raw, dict) else {"Keyword": str(raw)}
        _reject_unknown(item, KEYWORD_FIELDS, f"{prefix}.keywords[{keyword_index}]")
        item["Keyword"] = _nonempty_string(
            item.get("Keyword"), f"{prefix}.keywords[{keyword_index}]"
        )
        phrases.validate(item["Keyword"], f"{prefix}.keywords[{keyword_index}]")
        keywords.append(item)
    if channel in {"search", "maps"} and not keywords:
        raise ValueError(f"{prefix}.keywords требует хотя бы одну поисковую фразу")
    if any(item["Keyword"] == "---autotargeting" for item in keywords):
        raise ValueError(
            f"{prefix}.keywords не должен содержать ---autotargeting; "
            "используйте отдельный объект autotargeting"
        )
    semantics.validate_keywords(keywords, f"{prefix}.keywords")
    if channel in {"search", "maps", "product"} or source.get("autotargeting"):
        autotargeting, autotargeting_findings = _autotargeting(
            source.get("autotargeting"), prefix
        )
        keywords.append(autotargeting)
        findings.extend(autotargeting_findings)

    if (
        channel == "network"
        and not keywords
        and not source.get("audience_interest_ids")
        and not source.get("retargeting_rules")
    ):
        findings.append({"status": policy.BLOCK, "rule": "network.targeting",
                         "message": f"{prefix}: нужны ключи, автотаргетинг или аудитория."})
    group: dict[str, Any] = {
        "Name": name,
        "RegionIds": region_ids,
    }
    if "offer_retargeting" in source and not isinstance(
        source["offer_retargeting"], bool
    ):
        raise ValueError(f"{prefix}.offer_retargeting должен быть true или false")
    group["UnifiedAdGroup"] = {
        "OfferRetargeting": "YES" if source.get("offer_retargeting") else "NO"
    }
    shared = source.get("negative_keyword_shared_set_ids")
    if shared is not None:
        from .identifiers import parse_id
        if not isinstance(shared, list) or not 1 <= len(shared) <= 3:
            raise ValueError("negative_keyword_shared_set_ids: от 1 до 3 ID")
        ids = [parse_id(value, "negative_keyword_shared_set_ids") for value in shared]
        if len(set(ids)) != len(ids):
            raise ValueError("negative_keyword_shared_set_ids: повторяющиеся ID")
        group["NegativeKeywordSharedSetIds"] = {"Items": ids}
    group_negatives = source.get("negative_keywords") or []
    if not isinstance(group_negatives, list):
        raise ValueError(f"{prefix}.negative_keywords должен быть массивом")
    if group_negatives:
        group["NegativeKeywords"] = {
            "Items": _negative_phrases(
                group_negatives,
                f"{prefix}.negative_keywords",
                maximum_total_length=4096,
            )
        }
    return group, compiled_ads, keywords, findings, [
        {"id": value, "source": f"{prefix}.region_ids"} for value in region_ids
    ]


def _strategy(
    channel: str,
    weekly_micros: int,
    source: Any,
    priority_goal_ids: set[int],
) -> dict[str, Any]:
    if source is None:
        source = {"type": "maximum_clicks"}
    elif isinstance(source, str):
        source = {"type": source}
    if not isinstance(source, dict):
        raise ValueError(f"bundle.channels.{channel}.strategy должен быть объектом")
    _reject_unknown(source, {"type", "goal_id", "bid_ceiling"},
                    f"bundle.channels.{channel}.strategy")
    strategy_type = str(source.get("type") or "maximum_clicks").strip().lower()
    aliases = {
        "maximum_clicks": "WB_MAXIMUM_CLICKS",
        "wb_maximum_clicks": "WB_MAXIMUM_CLICKS",
        "maximum_conversion_rate": "WB_MAXIMUM_CONVERSION_RATE",
        "wb_maximum_conversion_rate": "WB_MAXIMUM_CONVERSION_RATE",
    }
    try:
        api_type = aliases[strategy_type]
    except KeyError as exc:
        raise ValueError(
            f"bundle.channels.{channel}.strategy.type: поддерживаются "
            "maximum_clicks и maximum_conversion_rate"
        ) from exc
    if api_type == "WB_MAXIMUM_CLICKS":
        if source.get("goal_id") is not None:
            raise ValueError(
                f"bundle.channels.{channel}.strategy.goal_id допустим только "
                "для maximum_conversion_rate"
            )
        strategy_key = "WbMaximumClicks"
        strategy_value = {"WeeklySpendLimit": weekly_micros}
    else:
        try:
            goal_id = parse_id(source.get("goal_id"), "strategy.goal_id")
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"bundle.channels.{channel}.strategy.goal_id обязателен для "
                "maximum_conversion_rate"
            ) from exc
        launch_checks.validate_conversion_goal(goal_id, priority_goal_ids)
        strategy_key = "WbMaximumConversionRate"
        strategy_value = {"WeeklySpendLimit": weekly_micros, "GoalId": goal_id}

    if source.get("bid_ceiling") is not None:
        strategy_value["BidCeiling"] = _micros(
            source["bid_ceiling"], f"bundle.channels.{channel}.strategy.bid_ceiling"
        )
    search_placements = {
        "SearchResults": "YES" if channel == "search" else "NO",
        "ProductGallery": "YES" if channel == "product" else "NO",
        "DynamicPlaces": "YES" if channel == "search" else "NO",
        "Maps": "YES" if channel == "maps" else "NO",
        "SearchOrganizationList": "YES" if channel == "maps" else "NO",
    }
    if channel in {"search", "maps", "product"}:
        search = {
            "BiddingStrategyType": api_type,
            strategy_key: strategy_value,
            "PlacementTypes": search_placements,
        }
        network = {
            "BiddingStrategyType": "SERVING_OFF",
            "PlacementTypes": {"Network": "NO", "Maps": "NO"},
        }
    else:
        search = {
            "BiddingStrategyType": "SERVING_OFF",
            "PlacementTypes": search_placements,
        }
        network = {
            "BiddingStrategyType": api_type,
            strategy_key: strategy_value,
            "PlacementTypes": {"Network": "YES", "Maps": "NO"},
        }
    if channel == "product":
        network = {"BiddingStrategyType": "NETWORK_DEFAULT",
                   "PlacementTypes": {"Network": "YES", "Maps": "NO"}}
    return {"Search": search, "Network": network}


def _time_targeting(source: Any) -> dict[str, Any] | None:
    """Use Direct's native round-the-clock default unless targeting is needed."""
    if source is None or source == "always_on":
        return None
    if not isinstance(source, dict):
        raise ValueError("bundle.schedule должен быть always_on или объектом")
    _reject_unknown(
        source,
        {"days", "hours", "bid_percent", "consider_working_weekends", "holidays"},
        "bundle.schedule",
    )
    days = [int(value) for value in source.get("days", range(1, 8))]
    hours = [int(value) for value in source.get("hours", range(24))]
    if not days or any(value not in range(1, 8) for value in days):
        raise ValueError("bundle.schedule.days принимает непустой список значений 1–7")
    if not hours or any(value not in range(24) for value in hours):
        raise ValueError("bundle.schedule.hours принимает непустой список значений 0–23")
    bid_percent = int(source.get("bid_percent", 100))
    if not 10 <= bid_percent <= 200 or bid_percent % 10:
        raise ValueError("bundle.schedule.bid_percent должен быть от 10 до 200, кратно 10")
    consider = source.get("consider_working_weekends", False)
    if not isinstance(consider, bool):
        raise ValueError(
            "bundle.schedule.consider_working_weekends должен быть true или false"
        )
    holidays = source.get("holidays")
    holiday_schedule = None
    if holidays is not None:
        if not isinstance(holidays, dict):
            raise ValueError("bundle.schedule.holidays должен быть объектом")
        _reject_unknown(holidays, {"suspend", "start_hour", "end_hour", "bid_percent"},
                        "bundle.schedule.holidays")
        if not isinstance(holidays.get("suspend"), bool):
            raise ValueError("bundle.schedule.holidays.suspend должен быть true или false")
        holiday_schedule = {"SuspendOnHolidays": "YES" if holidays["suspend"] else "NO"}
        if holidays["suspend"]:
            if set(holidays) - {"suspend"}:
                raise ValueError("При holidays.suspend=true часы и коэффициент не задаются")
        else:
            start = int(_required(holidays, "start_hour", "bundle.schedule.holidays"))
            end = int(_required(holidays, "end_hour", "bundle.schedule.holidays"))
            percent = int(holidays.get("bid_percent", bid_percent))
            if not 0 <= start < end <= 24 or not 10 <= percent <= 200 or percent % 10:
                raise ValueError("Некорректные часы или коэффициент bundle.schedule.holidays")
            holiday_schedule.update(StartHour=start, EndHour=end, BidPercent=percent)
    if (set(days) == set(range(1, 8)) and set(hours) == set(range(24))
            and bid_percent == 100 and holiday_schedule is None):
        return None
    result = {
        "Schedule": {
            "Items": [
                ",".join(map(str, [day] + [
                    bid_percent if day in days and hour in hours else 0
                    for hour in range(24)
                ]))
                # Omitted days default to 100% in the API, so include disabled days.
                for day in range(1, 8)
            ]
        },
        "ConsiderWorkingWeekends": "YES" if consider else "NO",
    }
    # Without an explicit holiday override, use the ordinary weekday schedule.
    # Taking min/max(hours) would silently enable gaps in a split schedule.
    if holiday_schedule is not None:
        result["HolidaysSchedule"] = holiday_schedule
    return result


def _campaign(
    bundle: dict[str, Any],
    channel: str,
    weekly_micros: int,
    strategy_source: Any,
    campaign_name: str | None = None,
) -> dict[str, Any]:
    name = _nonempty_string(bundle["name"], "bundle.name")
    campaign_name = campaign_name or f"{name} | {CHANNEL_LABELS[channel]}"
    if len(campaign_name) > 255:
        raise ValueError(
            f"bundle.name с суффиксом канала {channel} длиннее 255 символов"
        )
    common: dict[str, Any] = {
        "Name": campaign_name,
        "StartDate": bundle["start_date"],
        "TimeZone": bundle["time_zone"],
    }
    time_targeting = _time_targeting(bundle.get("schedule"))
    if time_targeting is not None:
        common["TimeTargeting"] = time_targeting
    if bundle.get("end_date"):
        common["EndDate"] = bundle["end_date"]
    if bundle.get("blocked_ips") is not None:
        import ipaddress
        ips = bundle["blocked_ips"]
        if not isinstance(ips, list) or len(ips) > 25:
            raise ValueError("blocked_ips: не более 25 IP-адресов")
        common["BlockedIps"] = {"Items": list(dict.fromkeys(str(ipaddress.IPv4Address(v))
                                                          for v in ips))}
    selected_negatives = _negative_keywords(bundle)
    if selected_negatives:
        common["NegativeKeywords"] = {"Items": selected_negatives}
    if channel in {"network", "product"}:
        exclusions = _excluded_sites(bundle)
        if exclusions:
            common["ExcludedSites"] = {"Items": exclusions}

    unified: dict[str, Any] = {
        "BiddingStrategy": _strategy(
            channel,
            weekly_micros,
            strategy_source,
            {int(item["GoalId"]) for item in bundle["priority_goals"]},
        ),
        "Settings": [{"Option": key, "Value": "YES" if value else "NO"}
                     for key, value in bundle["settings"].items()],
        "TrackingParams": _tracking_string(bundle["tracking_profile"]),
        "AttributionModel": bundle["attribution_model"],
    }
    if bundle["counter_ids"]:
        unified["CounterIds"] = {"Items": bundle["counter_ids"]}
    if bundle.get("priority_goals"):
        unified["PriorityGoals"] = {"Items": bundle["priority_goals"]}
    common["UnifiedCampaign"] = unified
    return common


def _normalize_bundle(
    raw: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("bundle должен быть JSON-объектом")
    bundle = deepcopy(raw)
    _reject_unknown(bundle, TOP_LEVEL_FIELDS, "bundle")
    selected = policy.get(bundle.get("policy_name", "agency_default_v1"))
    if "profile" not in selected and "profile_context" in bundle:
        raise ValueError("profile_context требует явный бизнес-профиль policy_name")
    bundle["name"] = _nonempty_string(_required(bundle, "name"), "bundle.name")
    bundle["time_zone"] = _nonempty_string(
        bundle.get("time_zone", "Europe/Moscow"), "bundle.time_zone"
    )
    if not re.fullmatch(r"[A-Za-z_+-]+(?:/[A-Za-z0-9_+.-]+)+", bundle["time_zone"]):
        raise ValueError("bundle.time_zone должен быть именем из Dictionaries.TimeZones")
    bundle["attribution_model"] = bundle.get("attribution_model", "AUTO")
    if bundle["attribution_model"] not in {"AUTO", "FCCD", "LC", "LSCCD"}:
        raise ValueError("bundle.attribution_model: AUTO, FCCD, LC или LSCCD")
    settings = bundle.get("settings", {})
    allowed_settings = {
        "ADD_METRICA_TAG", "ENABLE_SITE_MONITORING", "CAMPAIGN_EXACT_PHRASE_MATCHING_ENABLED",
        "ALTERNATIVE_TEXTS_ENABLED", "ENABLE_COMPANY_INFO", "REQUIRE_SERVICING",
    }
    if not isinstance(settings, dict):
        raise ValueError("bundle.settings должен быть объектом")
    _reject_unknown(settings, allowed_settings, "bundle.settings")
    if any(not isinstance(value, bool) for value in settings.values()):
        raise ValueError("bundle.settings принимает только true/false")
    bundle["settings"] = {
        "ADD_METRICA_TAG": True, "ENABLE_SITE_MONITORING": True,
        "ALTERNATIVE_TEXTS_ENABLED": selected["alternative_texts_default"],
        **settings,
    }
    bundle["client_budget"] = launch_checks.normalize_budget(bundle.get("client_budget"))
    start_raw = _nonempty_string(_required(bundle, "start_date"), "bundle.start_date")
    try:
        start = date.fromisoformat(start_raw)
    except ValueError as exc:
        raise ValueError("bundle.start_date должен иметь формат YYYY-MM-DD") from exc
    if start < (today or campaign_setup.moscow_today()):
        raise ValueError("bundle.start_date не может быть в прошлом")
    bundle["start_date"] = start.isoformat()
    audience_setup.age_modifiers(_required(bundle, "age_min"), bundle.get("age_max"))
    if bundle.get("end_date"):
        try:
            end = date.fromisoformat(str(bundle["end_date"]))
        except ValueError as exc:
            raise ValueError("bundle.end_date должен иметь формат YYYY-MM-DD") from exc
        if end < start:
            raise ValueError("bundle.end_date не может быть раньше start_date")
        bundle["end_date"] = end.isoformat()

    bundle["region_ids"] = (
        _ids(bundle["region_ids"], "bundle.region_ids")
        if bundle.get("region_ids") is not None
        else []
    )
    raw_counters = bundle.get("counter_ids", [])
    if not isinstance(raw_counters, list):
        raise ValueError("bundle.counter_ids должен быть массивом")
    bundle["counter_ids"] = [int(value) for value in raw_counters]
    if any(value <= 0 for value in bundle["counter_ids"]):
        raise ValueError("bundle.counter_ids должен содержать положительные ID")
    bundle["tracking_profile"] = bundle.get(
        "tracking_profile", selected["tracking_default_profile"]
    )
    _tracking_string(bundle["tracking_profile"])

    raw_goals = bundle.get("priority_goals", [])
    if not isinstance(raw_goals, list):
        raise ValueError("bundle.priority_goals должен быть массивом")
    goals = []
    for index, raw_goal in enumerate(raw_goals):
        if not isinstance(raw_goal, dict):
            raise ValueError(f"bundle.priority_goals[{index}] должен быть объектом")
        prefix = f"bundle.priority_goals[{index}]"
        _reject_unknown(raw_goal, {"goal_id", "value", "is_metrika_source_of_value"}, prefix)
        goal_id = parse_id(_required(raw_goal, "goal_id", prefix), f"{prefix}.goal_id")
        if goal_id == 13:
            raise ValueError("priority_goals: 13 — селектор ключевых целей, не отдельная цель")
        value = _micros(
            _required(raw_goal, "value", prefix),
            f"{prefix}.value",
        )
        goals.append({"GoalId": goal_id, "Value": value})
        if "is_metrika_source_of_value" in raw_goal:
            is_source = raw_goal["is_metrika_source_of_value"]
            if not isinstance(is_source, bool):
                raise ValueError(f"{prefix}.is_metrika_source_of_value: true или false")
            if is_source:
                raise ValueError(
                    "is_metrika_source_of_value=true допустим только для стратегий "
                    "AVERAGE_CRR/PAY_FOR_CONVERSION_CRR; компилятор их не поддерживает"
                )
            goals[-1]["IsMetrikaSourceOfValue"] = "YES" if is_source else "NO"
    if len({item["GoalId"] for item in goals}) != len(goals):
        raise ValueError("bundle.priority_goals содержит повторяющиеся goal_id")
    bundle["priority_goals"] = goals
    if goals and not bundle["counter_ids"]:
        raise ValueError("bundle.counter_ids требует счётчик для выбранных бизнес-целей")
    if bundle.get("goal_catalog_campaign_id") is not None:
        catalog_campaign_id = int(bundle["goal_catalog_campaign_id"])
        if catalog_campaign_id <= 0:
            raise ValueError("bundle.goal_catalog_campaign_id должен быть положительным")
        bundle["goal_catalog_campaign_id"] = catalog_campaign_id
    if not isinstance(bundle.get("allow_unverified_goals", False), bool):
        raise ValueError("bundle.allow_unverified_goals должен быть true или false")
    bundle["allow_unverified_goals"] = bool(
        bundle.get("allow_unverified_goals", False)
    )

    if not isinstance(bundle.get("office"), bool):
        raise ValueError("bundle.office должен быть явным true или false")
    # Yandex retired this campaign setting on 2026-08-31. Accept old bundles,
    # but do not let legacy fields affect the plan or its approval.
    bundle.pop("allow_extended_geo", None)
    manual = bundle.get("manual_checks")
    if not isinstance(manual, dict):
        raise ValueError("bundle.manual_checks должен быть объектом")
    _reject_unknown(manual, MANUAL_FIELDS, "bundle.manual_checks")
    manual.pop("extended_geo_verified", None)
    missing_checks = [
        name for name in REQUIRED_MANUAL_CHECKS
        if (name != "goals_reviewed" or goals) and manual.get(name) is not True
    ]
    if missing_checks:
        raise ValueError(
            "Не подтверждены ручные проверки: " + ", ".join(missing_checks)
        )
    acknowledgements = manual.get("acknowledged_warning_rules", [])
    if not isinstance(acknowledgements, list) or not all(
        isinstance(value, str) for value in acknowledgements
    ):
        raise ValueError("manual_checks.acknowledged_warning_rules должен быть массивом строк")

    has_channels = "channels" in bundle
    has_template = (
        "campaign_template" in bundle or "campaign_variants" in bundle
    )
    if has_channels and has_template:
        raise ValueError(
            "bundle.channels нельзя сочетать с campaign_template/campaign_variants"
        )
    if has_template:
        template = _required(bundle, "campaign_template")
        variants = _required(bundle, "campaign_variants")
        if not isinstance(template, dict):
            raise ValueError("bundle.campaign_template должен быть объектом")
        _reject_unknown(template, CHANNEL_FIELDS, "bundle.campaign_template")
        _required(template, "groups", "bundle.campaign_template")
        if not isinstance(variants, list) or len(variants) < 2:
            raise ValueError(
                "bundle.campaign_variants должен содержать минимум два варианта"
            )
        expanded: dict[str, list[dict[str, Any]]] = {}
        for index, variant in enumerate(variants):
            prefix = f"bundle.campaign_variants[{index}]"
            if not isinstance(variant, dict):
                raise ValueError(f"{prefix} должен быть объектом")
            _reject_unknown(variant, CAMPAIGN_VARIANT_FIELDS, prefix)
            channel = _nonempty_string(
                _required(variant, "channel", prefix), f"{prefix}.channel"
            )
            if channel not in CHANNEL_LABELS:
                raise ValueError(
                    f"{prefix}.channel содержит неизвестный канал {channel!r}"
                )
            merged = deepcopy(template)
            merged.update({
                key: deepcopy(value)
                for key, value in variant.items()
                if key != "channel"
            })
            expanded.setdefault(channel, []).append(merged)
        bundle["channels"] = expanded
        bundle.pop("campaign_template", None)
        bundle.pop("campaign_variants", None)

    channels = _required(bundle, "channels")
    if not isinstance(channels, dict):
        raise ValueError("bundle.channels должен быть объектом")
    if not channels:
        raise ValueError("bundle.channels должен содержать хотя бы один канал")
    maps_required = selected.get("profile", {}).get("maps_required", True)
    if bundle["office"] and maps_required and "maps" not in channels:
        raise ValueError("При bundle.office=true требуется отдельный канал maps")
    if not bundle["office"] and "maps" in channels:
        raise ValueError("Канал maps задан при bundle.office=false")
    unknown_channels = channels.keys() - set(CHANNEL_LABELS)
    if unknown_channels:
        raise ValueError("Неизвестные каналы: " + ", ".join(sorted(unknown_channels)))
    if isinstance(bundle.get("weekly_budget"), dict):
        unknown_budgets = bundle["weekly_budget"].keys() - set(CHANNEL_LABELS)
        if unknown_budgets:
            raise ValueError(
                "bundle.weekly_budget содержит неизвестные каналы: "
                + ", ".join(sorted(unknown_budgets))
            )
    return bundle


def _plan_hash(plan_without_hash: dict[str, Any]) -> str:
    canonical = json.dumps(
        plan_without_hash,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compile_bundle(
    raw: dict[str, Any],
    client_login: str,
    *,
    today: date | None = None,
    default_weekly_budget: float | None = None,
) -> dict[str, Any]:
    """Проверить бизнес-план и скомпилировать payload трёх API-сервисов."""
    bundle = _normalize_bundle(raw, today=today)
    selected = policy.get(bundle.get("policy_name", "agency_default_v1"))
    profile_context = None
    profile_decisions = []
    if "profile" in selected:
        if bundle["tracking_profile"] not in selected["tracking_profiles"]:
            raise ValueError("Бизнес-профиль требует tracking_profile=utm_v1")
        profile_context = profiles.context(
            bundle.get("profile_context", {}), selected, login=client_login,
            goals={r["GoalId"] for r in bundle["priority_goals"]},
            counters=set(bundle["counter_ids"]), today=today or campaign_setup.moscow_today())
    compiled_campaigns = []
    findings = []
    region_evidence = []
    weekly_by_channel: dict[str, int] = {}
    for channel in ("search", "network", "maps", "product"):
        if channel not in bundle["channels"]:
            continue
        raw_channel = bundle["channels"][channel]
        channel_sources = raw_channel if isinstance(raw_channel, list) else [raw_channel]
        if not channel_sources:
            raise ValueError(f"bundle.channels.{channel} не может быть пустым")
        for variant_index, channel_source in enumerate(channel_sources):
            variant_prefix = (
                f"bundle.channels.{channel}[{variant_index}]"
                if isinstance(raw_channel, list)
                else f"bundle.channels.{channel}"
            )
            if not isinstance(channel_source, dict):
                raise ValueError(f"{variant_prefix} должен быть объектом")
            _reject_unknown(channel_source, CHANNEL_FIELDS, variant_prefix)
            product_source = None
            if channel == "product":
                if not selected.get("profile", {}).get("product_formats"):
                    raise ValueError("Товарный канал требует ecommerce_new_v2/established_v2")
                product_source = products.source(channel_source.get("product_source"))
            elif "product_source" in channel_source:
                raise ValueError("product_source допустим только в channel=product")
            raw_groups = _required(channel_source, "groups", variant_prefix)
            if not isinstance(raw_groups, list) or not raw_groups:
                raise ValueError(f"{variant_prefix}.groups не может быть пустым")
            if len(raw_groups) > policy.GROUPS_MAX_PER_CAMPAIGN:
                raise ValueError(f"{variant_prefix}: не более 1000 групп в кампании")

            budget_source = (
                channel_source["weekly_budget"]
                if "weekly_budget" in channel_source
                else bundle.get("weekly_budget")
            )
            if len(channel_sources) > 1 and "weekly_budget" not in channel_source:
                raise ValueError(
                    f"{variant_prefix}.weekly_budget обязателен для каждого варианта: "
                    "общий бюджет канала нельзя автоматически копировать в несколько кампаний"
                )
            weekly_micros, weekly_amount, used_default = _weekly_budget(
                budget_source, channel, default_weekly_budget
            )
            weekly_by_channel[channel] = weekly_by_channel.get(channel, 0) + weekly_micros
            budget_policy = selected["budget"]
            in_policy = budget_policy["range_enforcement"] == "client_total" or (
                Decimal(str(budget_policy["minimum"]))
                <= weekly_amount
                <= Decimal(str(budget_policy["maximum"]))
            )
            budget_approved = channel_source.get("budget_approved") is True
            if not in_policy and not budget_approved:
                findings.append({
                    "status": policy.WARNING,
                    "rule": "budget.weekly_range",
                    "message": (
                        f"{variant_prefix}: недельный бюджет вне "
                        "ориентира 2 000–5 000 RUB; применимость проверяется по валюте клиента."
                    ),
                    "evidence": {"amount": str(weekly_amount)},
                })
            elif not in_policy and budget_approved:
                findings.append(
                    {
                        "status": policy.PASS,
                        "rule": "budget.explicit_override",
                        "message": (f"{variant_prefix}: явное значение бюджета сохранено "
                                    "(совместимость budget_approved)."),
                        "evidence": {"amount": str(weekly_amount)},
                    }
                )
            elif used_default:
                findings.append({
                    "status": policy.PASS,
                    "rule": "budget.default_applied",
                    "message": f"Для {channel} применён серверный бюджет по умолчанию.",
                    "evidence": {"amount": str(weekly_amount)},
                })

            default_regions = (
                _ids(channel_source["region_ids"], f"{variant_prefix}.region_ids")
                if channel_source.get("region_ids") is not None
                else bundle["region_ids"]
            )
            if not default_regions:
                raise ValueError(
                    f"{variant_prefix}.region_ids обязателен, "
                    "если bundle.region_ids не задан"
                )
            compiled_groups = []
            for index, source_group in enumerate(raw_groups):
                if not isinstance(source_group, dict):
                    raise ValueError(
                        f"{variant_prefix}.groups[{index}] должен быть объектом"
                    )
                group, ads, keywords, group_findings, regions = _group(
                    source_group,
                    channel=channel,
                    default_region_ids=default_regions,
                    index=index,
                    product_source=product_source,
                    default_sitelink_set_id=channel_source.get(
                        "sitelink_set_id", bundle.get("sitelink_set_id")
                    ),
                )
                compiled_groups.append({
                    "ad_group": group,
                    "ads": ads,
                    "keywords": keywords,
                    "semantic": deepcopy(source_group.get("semantic")),
                    "ad_hypotheses": [ad.get("hypothesis") for ad in source_group["ads"]],
                    "action_buttons": [
                        creative.action_button(ad.get("action_button"), ad.get("href"))
                        for ad in source_group["ads"]
                    ],
                })
                audience = audience_setup.compile_interest(source_group, channel)
                if audience:
                    compiled_groups[-1]["audience"] = audience
                findings.extend(group_findings)
                region_evidence.extend(regions)

            explicit_name = channel_source.get("name")
            if explicit_name is not None:
                campaign_name = _nonempty_string(
                    explicit_name, f"{variant_prefix}.name"
                )
            else:
                campaign_name = f"{bundle['name']} | {CHANNEL_LABELS[channel]}"
                if channel_source.get("name_suffix") is not None:
                    campaign_name += " | " + _nonempty_string(
                        channel_source["name_suffix"],
                        f"{variant_prefix}.name_suffix",
                    )
            strategy_source = channel_source.get("strategy")
            if profile_context is not None:
                strategy_source, decision = profiles.strategy(
                    strategy_source, profile_context, selected, channel=channel,
                    regions=sorted({r for g in compiled_groups
                                    for r in g["ad_group"]["RegionIds"]}),
                    today=today or campaign_setup.moscow_today())
                profile_decisions.append({"campaign": campaign_name, **decision})
            compiled_campaigns.append({
                "channel": channel,
                "channel_variant": variant_index,
                "campaign": _campaign(
                    bundle,
                    channel,
                    weekly_micros,
                    strategy_source,
                    campaign_name,
                ),
                "groups": compiled_groups,
                "group_count_reason": channel_source.get("group_count_reason"),
                "bid_modifiers": audience_setup.age_modifiers(
                    bundle["age_min"], bundle.get("age_max")
                ),
            })

            if product_source is not None:
                compiled_campaigns[-1]["product_source"] = product_source

        if len(channel_sources) > 1:
            total = weekly_by_channel[channel] / 1_000_000
            approved = all(row.get("budget_approved") is True for row in channel_sources)
            in_range = (budget_policy["range_enforcement"] == "client_total" or
                        budget_policy["minimum"] <= total <= budget_policy["maximum"])
            findings.append({
                "status": policy.PASS if in_range or approved else policy.WARNING,
                "rule": "budget.channel_total",
                "message": f"Суммарный недельный бюджет канала {channel}: {total:g}.",
                "evidence": {"channel": channel, "campaigns": len(channel_sources),
                             "weekly_total": total, "explicitly_approved": approved},
            })

    products.validate_campaigns(compiled_campaigns)
    business_profiles = launch_checks.normalize_businesses(
        bundle.get("business_profiles", []),
        {products.payload(ad)["BusinessId"] for item in compiled_campaigns
         for group in item["groups"] for ad in group["ads"]
         if products.payload(ad).get("BusinessId")},
    )
    client_budget_check = launch_checks.check_budget(bundle["client_budget"], compiled_campaigns)

    if profile_context is not None:
        profiles.validate_campaigns(compiled_campaigns, profile_context, selected,
                                    client_budget=bundle["client_budget"], office=bundle["office"],
                                    today=today or campaign_setup.moscow_today(),
                                    allow_unverified_goals=bundle["allow_unverified_goals"])
        findings.append({"status": policy.MANUAL, "rule": "profile.evidence",
                         "message": "Профиль и история проверены по объявленным данным; "
                                    "содержание источников требует проверки специалистом.",
                         "evidence": {"profile": selected["profile"],
                                      "strategy_decisions": profile_decisions}})
    for campaign in compiled_campaigns:
        findings.extend(semantics.cross_group_checks(campaign))

    negative_selection = negatives.select(bundle)
    negative_selection["group_selections"] = []
    for item in compiled_campaigns:
        contexts = negatives.plan_contexts(item)
        contexts.extend(negative_selection["review_context"])
        conflicts = negatives.conflicts(
            [row["phrase"] for row in negative_selection["selected"]], contexts
        )
        for group in item["groups"]:
            group_phrases = group["ad_group"].get("NegativeKeywords", {}).get("Items", [])
            if group_phrases:
                negative_selection["group_selections"].append({
                    "campaign": item["campaign"]["Name"],
                    "group": group["ad_group"]["Name"],
                    "selected": [{
                        "phrase": phrase, "source": "explicit_group_negative",
                        "reason": "Явно задано для этой группы в исходном плане.",
                        "business_reason_requires_review": True,
                    } for phrase in group_phrases],
                })
            conflicts.extend(negatives.conflicts(
                group_phrases,
                negatives.plan_contexts({"groups": [group]})
                + negative_selection["review_context"],
            ))
        if conflicts:
            findings.append({
                "status": policy.WARNING,
                "rule": "search.negative_context_conflict",
                "message": (
                    "Минус-фразы пересекаются с предложением или семантикой; проверьте спрос."
                ),
                "evidence": {"campaign": item["campaign"]["Name"], "conflicts": conflicts},
            })
    findings.append({
        "status": policy.MANUAL,
        "rule": "search.negative_keywords",
        "message": (
            "Проверить минус-фразы по бизнесу и посадочной; общего обязательного списка нет."
        ),
        "evidence": negative_selection,
    })
    findings.append({
        "status": policy.MANUAL,
        "rule": "regions.api_lookup",
        "message": (
            "Регионы подтверждены пользователем; executor должен повторно "
            "проверить ID через API."
        ),
        "evidence": {"regions": region_evidence},
    })
    maximum_goals = selected["goals"]["maximum_recommended"]
    if len(bundle["priority_goals"]) > maximum_goals:
        findings.append({
            "status": policy.WARNING,
            "rule": "goals.too_many",
            "message": (
                f"Выбрано больше {maximum_goals} приоритетных целей; "
                "проверьте, что их ценность сопоставима."
            ),
            "evidence": {"count": len(bundle["priority_goals"])},
        })
    semantic_review, semantic_findings = semantics.review(
        bundle.get("semantic_plan"), compiled_campaigns,
        today=today or campaign_setup.moscow_today(), selected_policy=selected,
    )
    findings.extend(semantic_findings)
    warning_rules = {
        finding["rule"] for finding in findings if finding["status"] == policy.WARNING
    }
    acknowledged = set(bundle["manual_checks"].get("acknowledged_warning_rules", []))
    # Budget ranges are advisory; amounts are clarified at task intake.
    budget_notices = {"budget.weekly_range", "budget.channel_total"}
    unacknowledged = sorted(warning_rules - acknowledged - budget_notices)
    if unacknowledged:
        findings.append({
            "status": policy.BLOCK,
            "rule": "warnings.acknowledgement",
            "message": "Есть не рассмотренные при проверке плана предупреждения.",
            "evidence": {"unacknowledged_rules": unacknowledged},
        })
    plan: dict[str, Any] = {
        "schema": "direct_campaign_bundle_v1",
        "client_login": _nonempty_string(client_login, "client_login"),
        "api_version": "v501",
        "policy": {
            "name": selected["name"],
            "version": selected["version"],
        },
        "manual_checks": bundle["manual_checks"],
        "tracking_profile": bundle["tracking_profile"],
        "client_budget": bundle["client_budget"],
        "business_profiles": business_profiles,
        "goal_catalog_campaign_id": bundle.get("goal_catalog_campaign_id"),
        "allow_unverified_goals": bundle["allow_unverified_goals"],
        "negative_keyword_selection": negative_selection,
        "semantic_plan": deepcopy(bundle.get("semantic_plan")),
        "semantic_review": semantic_review,
        "campaigns": compiled_campaigns,
        "required_manual_actions": creative.planned_actions(compiled_campaigns),
        "findings": findings,
        "summary": {
            "client_budget": client_budget_check,
            "tracking_profile": bundle["tracking_profile"],
            "alternative_texts_enabled": bundle["settings"]["ALTERNATIVE_TEXTS_ENABLED"],
            "business_profiles_declared": len(business_profiles),
            "time_zone": bundle["time_zone"],
            "weekly_budget_by_channel": {
                channel: amount / 1_000_000 for channel, amount in weekly_by_channel.items()
            },
            "weekly_budget_total": sum(weekly_by_channel.values()) / 1_000_000,
            "campaigns": len(compiled_campaigns),
            "ad_groups": sum(len(item["groups"]) for item in compiled_campaigns),
            "ads": sum(
                len(group["ads"])
                for item in compiled_campaigns
                for group in item["groups"]
            ),
            **semantics.counts(compiled_campaigns),
            "bid_modifiers": sum(
                sum(len(row["DemographicsAdjustments"]) for row in item["bid_modifiers"])
                for item in compiled_campaigns
            ),
        },
    }
    names = [
        item["campaign"]["Name"].strip().casefold() for item in compiled_campaigns
    ]
    if len(names) != len(set(names)):
        raise ValueError("Скомпилированные кампании должны иметь уникальные названия")
    audiences = len(audience_setup.planned(plan))
    if audiences:
        plan["summary"].update(audience_targets=audiences, retargeting_lists=audiences)
        plan["summary"]["criteria"] += audiences
    if profile_context is not None:
        plan.update(policy_snapshot=selected, policy_fingerprint=policy.fingerprint(selected),
                    profile_context=profile_context, profile_office=bundle["office"],
                    profile_decisions=profile_decisions)
        plan["summary"] = {"business_profile": selected["name"],
                           "client_stage": selected["profile"]["stage"], **plan["summary"]}
    plan["api_units_estimate"] = api_units.estimate_add(plan["summary"])
    plan["ready"] = not any(item["status"] == policy.BLOCK for item in findings)
    plan["plan_hash"] = _plan_hash(plan)
    return plan


def persist(plan: dict[str, Any], out_dir: Path) -> Path:
    stem = store.safe_stem(plan["campaigns"][0]["campaign"]["Name"])
    path = out_dir / f"plan_{stem}_{plan['plan_hash'][:12]}.json"
    path.write_text(
        json.dumps(wire(plan), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path
