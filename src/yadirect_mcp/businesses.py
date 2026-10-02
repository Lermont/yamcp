"""Профили организаций Яндекс Бизнеса: чтение по ID и сверка телефона с сайтом.

Businesses.get принимает только Ids — поиска организации по телефону в Direct API
нет. Поэтому ID берётся из объявления или интерфейса, а соответствие клиенту
проверяется по телефону профиля: он должен встречаться на страницах сайта.
Источник: https://yandex.ru/dev/direct/doc/ru/businesses/get
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlsplit

from . import launch_checks, policy
from .identifiers import parse_id

FIELDS = ["Id", "Name", "Address", "Phone", "ProfileUrl", "IsPublished", "HasOffice",
          "Urls", "Rubric", "MergedIds"]
MAX_IDS = 1000
MAX_PHONES = 50
_SEP = r"[\s\- ‑‒–()]*"
_PHONE_TEXT = re.compile(
    r"(?<![\d+])(?:\+\s*7|8)" + _SEP + r"\d{3}" + _SEP + r"\d{3}" + _SEP
    + r"\d{2}" + _SEP + r"\d{2}(?!\d)"
)
_TEL_HREF = re.compile(r"""href\s*=\s*["']\s*tel:([^"']+)["']""", re.I)


def phones_in(html: str) -> list[str]:
    """Российские номера из ссылок tel: и текста страницы, только цифры с 7."""
    found: set[str] = set()
    for match in _TEL_HREF.finditer(html):
        raw = unquote(match.group(1)).split(",", 1)[0].split(";", 1)[0].strip()
        try:
            found.add(launch_checks.phone(raw))
        except ValueError:
            continue
    for match in _PHONE_TEXT.finditer(html):
        digits = re.sub(r"\D", "", match.group(0))
        found.add("7" + digits[1:] if digits.startswith("8") else digits)
    return sorted(found)[:MAX_PHONES]


def normalize_ids(values: Any) -> list[int]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_IDS:
        raise ValueError(f"business_ids: требуется от 1 до {MAX_IDS} ID профилей")
    ids = [parse_id(value, "business_ids") for value in values]
    if any(value <= 0 for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("business_ids: нужны уникальные положительные ID")
    return ids


async def read(api: Any, login: str, ids: list[int]) -> list[dict]:
    """Все доступные профили по ID; отсутствующий профиль не считается ошибкой чтения."""
    rows: list[dict] = []
    for start in range(0, len(ids), MAX_IDS):
        chunk = ids[start:start + MAX_IDS]
        result = await api.call_v501(
            "businesses", "get",
            {"SelectionCriteria": {"Ids": chunk}, "FieldNames": FIELDS,
             "Page": {"Limit": MAX_IDS}},
            client_login=login,
        )
        if result.get("LimitedBy") is not None:
            raise ValueError("Businesses.get: неполный список профилей")
        rows.extend(result.get("Businesses", []))
    returned = [row.get("Id") for row in rows]
    if len(returned) != len(set(returned)) or not set(returned) <= set(ids):
        raise ValueError("Businesses.get: дубли или профили вне запроса")
    return rows


def _host(value: str | None) -> str | None:
    if not value:
        return None
    host = urlsplit(value if "//" in value else "//" + value).hostname or ""
    return host.casefold().removeprefix("www.") or None


def compare(
    ids: list[int], rows: list[dict], *, site_phones: list[str] | None = None,
    expected_phones: list[str] | None = None, site_urls: list[str] | None = None,
) -> dict[str, Any]:
    """PASS только для опубликованного профиля с телефоном с сайта или ожидаемым."""
    by_id = {row["Id"]: row for row in rows}
    expected = sorted({launch_checks.phone(value) for value in expected_phones or []})
    site = sorted(set(site_phones or []))
    hosts = {_host(url) for url in site_urls or []} - {None}
    profiles = []
    for identifier in ids:
        row = by_id.get(identifier)
        if row is None:
            profiles.append({
                "business_id": identifier, "status": policy.BLOCK, "accessible": False,
                "message": ("Businesses.get не вернул профиль: у логина нет прав "
                            "представителя и профиль не привязан к его объявлениям. "
                            "ID и телефон сверить в интерфейсе."),
            })
            continue
        try:
            profile_phone = launch_checks.phone(row.get("Phone"))
        except ValueError:
            profile_phone = None
        urls = [url for url in row.get("Urls") or [] if isinstance(url, str)]
        url_hosts = {_host(url) for url in urls} - {None}
        checks = {
            "published": row.get("IsPublished") == "YES",
            "phone_on_site": (profile_phone in site) if site and profile_phone else None,
            "phone_expected": (profile_phone in expected) if expected and profile_phone else None,
            "site_in_profile_urls": bool(hosts & url_hosts) if hosts and urls else None,
        }
        phone_checks = [checks["phone_on_site"], checks["phone_expected"]]
        if not checks["published"] or profile_phone is None or False in phone_checks:
            status = policy.BLOCK
        elif True in phone_checks:
            status = policy.PASS
        else:
            status = policy.MANUAL
        profiles.append({
            "business_id": identifier, "status": status, "accessible": True,
            "name": row.get("Name"), "address": row.get("Address"), "phone": row.get("Phone"),
            "phone_normalized": profile_phone, "is_published": row.get("IsPublished"),
            "has_office": row.get("HasOffice"), "profile_url": row.get("ProfileUrl"),
            "rubric": row.get("Rubric"), "urls": urls, "merged_ids": row.get("MergedIds"),
            "checks": checks,
        })
    statuses = {row["status"] for row in profiles}
    overall = (policy.BLOCK if policy.BLOCK in statuses
               else policy.MANUAL if policy.MANUAL in statuses else policy.PASS)
    return {
        "status": overall,
        "profiles": profiles,
        "site_phones": site,
        "expected_phones": expected,
        "method": ("Businesses.get по ID; телефон профиля сравнивается с номерами "
                   "tel: и текста страниц сайта и/или с ожидаемыми номерами."),
        "phone_search_supported_by_api": False,
    }
