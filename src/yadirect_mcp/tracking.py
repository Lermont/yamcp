"""Разбор и проверка URL-параметров без подмены UTM кастомной разметкой."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote, unquote_plus, urlsplit, urlunsplit


def parse_params(value: str | None) -> dict[str, str | list[str]]:
    """Разобрать полный URL либо строку TrackingParams вида ``?a=1&b=2``."""
    if not value:
        return {}
    raw = value.strip()
    query = urlsplit(raw).query if "://" in raw else raw.lstrip("?&")
    result = {}
    for key, val in parse_qsl(query, keep_blank_values=True):
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(val)
        else:
            result[key] = val
    return result


def effective_params(
    href: str | None,
    campaign_tracking: str | None,
    group_tracking: str | None = None,
    ad_tracking: str | None = None,
) -> dict[str, Any]:
    """Объявление > группа > кампания; выбранный блок переопределяет Href.

    Блоки разных уровней не объединяются. Правила Direct:
    https://yandex.ru/support/direct/ru/statistics/url-tags-for-camping
    """
    href_params = parse_params(href)
    group_params = parse_params(group_tracking)
    campaign_params = parse_params(campaign_tracking)
    ad_params = parse_params(ad_tracking)
    sources = {
        "href": href_params,
        "group": group_params,
        "campaign": campaign_params,
        "ad": ad_params,
    }
    collisions: dict[str, dict[str, str | list[str]]] = {}
    all_keys = set().union(*(params.keys() for params in sources.values()))
    for key in sorted(all_keys):
        values = {
            source: params[key] for source, params in sources.items() if key in params
        }
        if len({tuple(v) if isinstance(v, list) else v for v in values.values()}) > 1:
            collisions[key] = values
    selected_level = next((level for level in ("ad", "group", "campaign")
                           if sources[level]), None)
    merged = dict(href_params)
    if selected_level:
        merged.update(sources[selected_level])
    return {
        "href_params": href_params,
        "group_params": group_params,
        "campaign_params": campaign_params,
        "ad_params": ad_params,
        "selected_level": selected_level,
        "effective_params": merged,
        "conflicts": collisions,
    }


def render_url(
    href: str, campaign_tracking: str | None, group_tracking: str | None = None,
    *, ad_tracking: str | None = None, values: dict[str, Any],
) -> dict[str, Any]:
    """Build a real encoded test URL; unknown macros must never silently pass."""
    effective = effective_params(href, campaign_tracking, group_tracking, ad_tracking)
    unknown: set[str] = set()

    def replace(value: str, *, path: bool = False) -> str:
        def substitute(match: re.Match) -> str:
            key = unquote_plus(match.group(1))
            if key not in values:
                unknown.add(key)
                return match.group(0)
            text = str(values[key])
            return quote(text, safe="") if path else text
        return re.sub(r"(?:\{|%7[bB])((?:(?!%7[dD])[^{}])+)(?:\}|%7[dD])",
                      substitute, value)

    parts = urlsplit(href)
    if "{" in parts.netloc or "}" in parts.netloc:
        unknown.add("dynamic_host")
    selected = effective["selected_level"]
    selected_raw = {"campaign": campaign_tracking, "group": group_tracking,
                    "ad": ad_tracking}.get(selected) or ""
    selected_query = (urlsplit(selected_raw).query if "://" in selected_raw
                      else selected_raw.lstrip("?&"))
    overrides = {unquote_plus(part.split("=", 1)[0])
                 for part in selected_query.split("&") if part}
    pieces = [part for part in parts.query.split("&")
              if part and unquote_plus(part.split("=", 1)[0]) not in overrides]
    pieces.extend(part for part in selected_query.split("&") if part)
    if not any(unquote_plus(part.split("=", 1)[0]) == "yclid" for part in pieces):
        pieces.append("yclid=" + quote(str(values.get("yclid", "1234567890123456789")), safe=""))
    # Replace only macros, preserving all original escaping, duplicate keys and order.
    query = "&".join(replace(part, path=True) for part in pieces)
    url = urlunsplit(parts._replace(path=replace(parts.path, path=True),
                                   query=query, fragment=replace(parts.fragment, path=True)))
    if len(url.encode("utf-8")) > 4096:
        unknown.add("url_over_4096_bytes")
    return {"url": url, "unknown_macros": sorted(unknown),
            "selected_level": effective["selected_level"]}


def compare_profile(
    actual: str | None,
    expected: dict[str, str],
) -> dict[str, Any]:
    """Сравнить TrackingParams с именами и шаблонными значениями профиля."""
    parsed = parse_params(actual)
    missing = [key for key in expected if key not in parsed]
    mismatched = {
        key: {"expected": expected[key], "actual": parsed[key]}
        for key in expected.keys() & parsed.keys()
        if parsed[key] != expected[key]
    }
    return {
        "params": parsed,
        "missing": missing,
        "mismatched": mismatched,
        "extra": sorted(parsed.keys() - expected.keys()),
        "matches": not missing and not mismatched,
    }
