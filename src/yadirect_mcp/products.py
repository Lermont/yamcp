"""Typed product ads and source checks. A website is never a feed URL."""

from __future__ import annotations

import ipaddress
import json
from copy import deepcopy
from urllib.parse import urlsplit

from . import landing
from .identifiers import parse_id

AD_TYPES = {"shopping": "ShoppingAd", "listing": "ListingAd"}
READ_TYPES = {"ShoppingAd": "SHOPPING_AD", "ListingAd": "LISTING_AD"}
READ_FIELDS = [
    "FeedId",
    "FeedFilterConditions",
    "FeedProcessingStatus",
    "TitleSources",
    "TextSources",
    "DefaultTexts",
    "SitelinkSetId",
    "AdExtensions",
    "BusinessId",
]


def payload(ad: dict) -> dict:
    return next((ad[key] for key in ("ResponsiveAd", "ShoppingAd", "ListingAd") if key in ad), {})


def kind(ad: dict) -> str | None:
    return next((key for key in READ_TYPES if key in ad), None)


def url(value, label="url") -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError(f"{label}: нужен HTTP(S) URL до 1024 символов")
    parts = urlsplit(value)
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError(f"{label}: неверный публичный URL")
    host = parts.hostname.casefold()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError(f"{label}: нужен публичный адрес")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError(f"{label}: нужен публичный адрес")
    return value


def source(raw: dict, *, require_id=True) -> dict:
    fields = {"type", "url", "feed_id", "sample_urls", "site_feed_verified", "review_reason"}
    if (
        not isinstance(raw, dict)
        or raw.keys() - fields
        or raw.get("type") not in {"feed", "website"}
    ):
        raise ValueError("product_source: type=feed/website, url, feed_id, sample_urls")
    result = {"type": raw["type"], "url": url(raw.get("url"), "product_source.url")}
    if raw.get("feed_id") is not None:
        result["feed_id"] = parse_id(raw["feed_id"], "product_source.feed_id")
    elif require_id:
        raise ValueError(
            "product_source.feed_id: сначала подготовьте источник через "
            "direct_product_source; для сайта — источник в интерфейсе Директа"
        )
    samples = raw.get("sample_urls")
    if not isinstance(samples, list) or not 1 <= len(samples) <= 10:
        raise ValueError("product_source.sample_urls: 1–10 проверяемых товарных/каталожных URL")
    result["sample_urls"] = list(dict.fromkeys(url(v, "sample_urls") for v in samples))
    if raw["type"] == "website":
        host = urlsplit(result["url"]).hostname
        if any(urlsplit(v).hostname != host for v in result["sample_urls"]):
            raise ValueError("sample_urls: для сайта нужен тот же hostname")
        if require_id and (
            raw.get("site_feed_verified") is not True
            or not isinstance(raw.get("review_reason"), str)
            or not raw["review_reason"].strip()
        ):
            raise ValueError(
                "Источник сайта: подтвердите привязку FeedId в интерфейсе "
                "через site_feed_verified=true и review_reason"
            )
        result.update(
            site_feed_verified=raw.get("site_feed_verified") is True,
            review_reason=raw.get("review_reason", ""),
        )
    elif any(k in raw for k in ("site_feed_verified", "review_reason")):
        raise ValueError("site_feed_verified/review_reason относятся только к источнику website")
    return result


def filters(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError("feed_filter_conditions: не более 30 фильтров")
    allowed = {
        "CONTAINS_ANY",
        "EQUALS_ANY",
        "EXISTS",
        "GREATER_THAN",
        "IN_RANGE",
        "LESS_THAN",
        "NOT_CONTAINS_ALL",
    }
    for row in value:
        if (
            not isinstance(row, dict)
            or set(row) != {"Operand", "Operator", "Arguments"}
            or not isinstance(row["Operand"], str)
            or not row["Operand"].strip()
            or row["Operator"] not in allowed
            or not isinstance(row["Arguments"], list)
            or len(row["Arguments"]) > 10
            or any(not isinstance(v, str) or not v for v in row["Arguments"])
        ):
            raise ValueError("feed_filter_conditions: неверное условие")
        if row["Operator"] != "EXISTS" and not row["Arguments"]:
            raise ValueError("feed_filter_conditions: нужны Arguments")
    if len(json.dumps(value, ensure_ascii=False).encode()) > 65 * 1024:
        raise ValueError("feed_filter_conditions: не более 65 КБ")
    return deepcopy(value)


def ad(raw: dict, product_source: dict) -> dict:
    allowed = {
        "type",
        "default_text",
        "feed_filter_conditions",
        "title_sources",
        "text_sources",
        "sitelink_set_id",
        "ad_extension_ids",
        "business_id",
        "hypothesis",
    }
    if raw.keys() - allowed or raw.get("type") not in AD_TYPES:
        raise ValueError("Товарное объявление: type=shopping/listing и поля товарного формата")
    text = raw.get("default_text")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > 81
        or any(len(word) > 23 for word in text.split())
    ):
        raise ValueError("default_text: непустой текст до 81 символа, слово до 23")
    result = {"FeedId": product_source["feed_id"], "DefaultTexts": [text.strip()]}
    if raw.get("feed_filter_conditions"):
        result["FeedFilterConditions"] = filters(raw["feed_filter_conditions"])
    elif "feed_filter_conditions" in raw:
        filters(raw["feed_filter_conditions"])
    for snake, api in (("title_sources", "TitleSources"), ("text_sources", "TextSources")):
        if snake in raw:
            values = raw[snake]
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(v, str) or not v.strip() for v in values)
                or len(set(values)) != len(values)
            ):
                raise ValueError(f"{snake}: непустой массив уникальных полей фида")
            result[api] = list(values)
    for snake, api in (("sitelink_set_id", "SitelinkSetId"), ("business_id", "BusinessId")):
        if raw.get(snake) is not None:
            result[api] = parse_id(raw[snake], snake)
    if "ad_extension_ids" in raw:
        values = raw["ad_extension_ids"]
        if not isinstance(values, list) or not 1 <= len(values) <= 50:
            raise ValueError("ad_extension_ids: 1–50 ID")
        result["AdExtensionIds"] = [parse_id(v, "ad_extension_ids") for v in values]
        if len(set(result["AdExtensionIds"])) != len(values):
            raise ValueError("ad_extension_ids: повтор ID")
    return {AD_TYPES[raw["type"]]: result}


def validate_campaigns(campaigns: list[dict]) -> None:
    for campaign in campaigns:
        if campaign["channel"] != "product":
            if any(kind(ad) for g in campaign["groups"] for ad in g["ads"]):
                raise ValueError("Товарные объявления требуют channel=product")
            continue
        src = source(campaign.get("product_source"))
        strategy = campaign["campaign"]["UnifiedCampaign"]["BiddingStrategy"]
        if strategy["Search"].get("PlacementTypes") != {
            "SearchResults": "NO",
            "ProductGallery": "YES",
            "DynamicPlaces": "NO",
            "Maps": "NO",
            "SearchOrganizationList": "NO",
        } or strategy.get("Network") != {
            "BiddingStrategyType": "NETWORK_DEFAULT",
            "PlacementTypes": {"Network": "YES", "Maps": "NO"},
        }:
            raise ValueError("Товарный канал: галерея и РСЯ с общей стратегией")
        for group in campaign["groups"]:
            kinds = [kind(a) for a in group["ads"]]
            if not kinds or None in kinds or len(set(kinds)) != len(kinds):
                raise ValueError("В товарной группе не более одного ShoppingAd и одного ListingAd")
            for item in group["ads"]:
                value = payload(item)
                if value.get("FeedId") != src["feed_id"]:
                    raise ValueError("FeedId объявления не совпадает с product_source")
                raw = {
                    "type": "shopping" if kind(item) == "ShoppingAd" else "listing",
                    "default_text": (value.get("DefaultTexts") or [None])[0],
                }
                for snake, api in (
                    ("feed_filter_conditions", "FeedFilterConditions"),
                    ("title_sources", "TitleSources"),
                    ("text_sources", "TextSources"),
                    ("sitelink_set_id", "SitelinkSetId"),
                    ("ad_extension_ids", "AdExtensionIds"),
                    ("business_id", "BusinessId"),
                ):
                    if api in value:
                        raw[snake] = value[api]
                if ad(raw, src) != item:
                    raise ValueError("Некорректный payload товарного объявления")


async def check_sources(api, plan: dict) -> dict:
    from . import feeds

    campaigns = [c for c in plan["campaigns"] if c["channel"] == "product"]
    if not campaigns:
        return {"verified": True, "sources": []}
    result = await feeds.read(
        api, plan["client_login"], [c["product_source"]["feed_id"] for c in campaigns]
    )
    rows = {r["Id"]: r for r in result["feeds"]}
    checks = []
    for c in campaigns:
        src = c["product_source"]
        row = rows[src["feed_id"]]
        if (
            row.get("Status") != "DONE"
            or type(row.get("NumberOfItems")) is not int
            or row["NumberOfItems"] <= 0
        ):
            raise ValueError("Фид ещё не обработан, пуст или содержит ошибку")
        if row.get("BusinessType") != "RETAIL":
            raise ValueError("Товарный источник должен иметь BusinessType=RETAIL")
        if src["type"] == "feed" and (
            row.get("SourceType") != "URL" or (row.get("UrlFeed") or {}).get("Url") != src["url"]
        ):
            raise ValueError("URL фида не совпадает с согласованным product_source")
        available = set((row.get("TitleAndTextSources") or {}).get("Items", []))
        for g in c["groups"]:
            for item in g["ads"]:
                value = payload(item)
                if set(value.get("TitleSources", []) + value.get("TextSources", [])) - available:
                    raise ValueError("Поля заголовков/текстов отсутствуют в TitleAndTextSources")
        if src["type"] == "website":
            pages = await landing.inspect_pages([{"url": u} for u in src["sample_urls"]])
            if len(pages) != len(src["sample_urls"]) or any(
                not p.get("ok")
                or p.get("html_truncated")
                or not p.get("product_markup")
                or urlsplit(p.get("final_url") or p["url"]).hostname
                != urlsplit(src["url"]).hostname
                for p in pages
            ):
                raise ValueError("Не подтверждена товарная разметка на sample_urls сайта")
        checks.append(
            {
                "feed_id": src["feed_id"],
                "status": row["Status"],
                "number_of_items": row["NumberOfItems"],
                "source_type": src["type"],
                "site_binding": "caller_reviewed" if src["type"] == "website" else "api_url",
            }
        )
    return {"verified": True, "sources": checks}


def compare(expected: dict, actual: dict | None) -> bool:
    if not actual or actual.get("type") != READ_TYPES[kind(expected)]:
        return False
    value = payload(expected)
    fields = (
        ("FeedId", "feed_id"),
        ("DefaultTexts", "texts"),
        ("FeedFilterConditions", "feed_filter_conditions"),
        ("TitleSources", "title_sources"),
        ("TextSources", "text_sources"),
        ("BusinessId", "business_id"),
    )
    return all(
        actual.get(b, [] if a in {"FeedFilterConditions", "TitleSources", "TextSources"} else None)
        == value.get(
            a, [] if a in {"FeedFilterConditions", "TitleSources", "TextSources"} else None
        )
        for a, b in fields
    ) and set(actual.get("ad_extension_ids", [])) == set(value.get("AdExtensionIds", []))
