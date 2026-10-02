"""Contextual negative-keyword candidates, never a universal required blacklist.

Profiles are agency starting hypotheses, not platform requirements. Matching
below finds review candidates; it does not emulate Direct's morphology/auction.
"""

from __future__ import annotations

import re
from typing import Any

from . import policy

PROFILES: dict[str, dict[str, str]] = {
    "custom": {},
    "rental": {
        "вакансия": "Поиск сотрудников не является заказом аренды.",
        "резюме": "Поиск резюме не является заказом аренды.",
    },
    "education": {
        "вакансия преподавателя": "Поиск работы отделён от записи на обучение.",
        "торрент": "Скачивание копии курса отделено от покупки обучения.",
    },
    "toys": {
        "вакансия": "Поиск работы отделён от покупки игрушек.",
        "резюме": "Поиск резюме отделён от покупки игрушек.",
    },
    "legacy_reviewed": {},
}


def _key(value: str) -> str:
    # Keep operators: !форма and форма are not interchangeable exclusions.
    return " ".join(value.casefold().replace("ё", "е").split())


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(row, str) or not row.strip() for row in value
    ):
        raise ValueError(f"{field} должен быть массивом непустых строк")
    return [row.strip() for row in value]


def _reasoned(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{field} должен быть массивом объектов phrase/reason")
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"phrase", "reason"}:
            raise ValueError(f"{field}: обязательны только phrase и reason")
        phrase, reason = _strings([row["phrase"], row["reason"]], field)
        result.append({"phrase": phrase, "reason": reason})
    return result


def select(bundle: dict[str, Any]) -> dict[str, Any]:
    raw = bundle.get("negative_keyword_policy", {})
    allowed = {
        "profile",
        "business_terms",
        "additions",
        "exclusions",
        "search_queries",
        "landing_texts",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError(
            "negative_keyword_policy: допустимы profile, business_terms, additions, "
            "exclusions, search_queries, landing_texts"
        )
    profile = raw.get("profile", "custom")
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("negative_keyword_policy.profile: " + ", ".join(PROFILES))
    business_terms = _strings(raw.get("business_terms", []), "business_terms")
    review_context = [
        {"kind": kind, "text": text, "source": "provided_in_bundle"}
        for field, kind in (
            ("business_terms", "business_term"),
            ("search_queries", "search_query"),
            ("landing_texts", "landing"),
        )
        for text in _strings(raw.get(field, []), field)
    ]
    selected = {
        _key(phrase): {"phrase": phrase, "reason": reason, "source": f"profile:{profile}"}
        for phrase, reason in PROFILES[profile].items()
    }
    if profile == "legacy_reviewed":
        selected.update(
            {
                _key(phrase): {
                    "phrase": phrase,
                    "reason": "Явно выбран проверяемый старый снимок.",
                    "source": "snapshot:search_negative_keywords_v1",
                }
                for phrase in policy.load_snapshot("search_negative_keywords")
                if _key(phrase) not in policy.RISKY_NEGATIVES
            }
        )
    approved = _strings(bundle.get("approved_risky_negatives", []), "approved_risky_negatives")
    if any(_key(phrase) not in policy.RISKY_NEGATIVES for phrase in approved):
        raise ValueError("approved_risky_negatives содержит слова вне списка рискованных")
    for phrase in approved + _strings(
        bundle.get("additional_negative_keywords", []), "additional_negative_keywords"
    ):
        selected[_key(phrase)] = {
            "phrase": phrase,
            "reason": "Явно передано в исходном плане.",
            "source": "explicit_legacy_field",
        }
    for row in _reasoned(raw.get("additions", []), "additions"):
        selected[_key(row["phrase"])] = {**row, "source": "client_addition"}
    excluded = []
    for row in _reasoned(raw.get("exclusions", []), "exclusions"):
        removed = selected.pop(_key(row["phrase"]), None)
        excluded.append({**row, "source": "client_exclusion", "was_selected": removed is not None})
    # Protect explicit product terms only from profile defaults. An explicit
    # addition conflicting with these terms remains visible for human review.
    contexts = [{"kind": "business_term", "text": term} for term in business_terms]
    for key, row in list(selected.items()):
        if row["source"].startswith(("profile:", "snapshot:")) and conflicts(
            [row["phrase"]], contexts
        ):
            excluded.append(
                {
                    "phrase": row["phrase"],
                    "source": "business_term_protection",
                    "reason": (
                        "Совпадение с явно указанным товаром или услугой; "
                        "не добавлено автоматически."
                    ),
                    "was_selected": True,
                }
            )
            del selected[key]
    return {
        "profile": profile,
        "business_terms": business_terms,
        "selected": list(selected.values()),
        "excluded": excluded,
        "review_required": True,
        "review_context": review_context,
    }


def _tokens(value: str) -> list[str]:
    return re.findall(r"!?[^\W_]+", value.casefold().replace("ё", "е"))


def _stem(word: str) -> str:
    # Conservative Russian suffix matching, with a minimum root of 4 letters.
    for suffix in (
        "иями",
        "ами",
        "ями",
        "ого",
        "ему",
        "ому",
        "ая",
        "яя",
        "ое",
        "ее",
        "ые",
        "ие",
        "ой",
        "ий",
        "ый",
        "ам",
        "ям",
        "ом",
        "ем",
        "ов",
        "ев",
        "ах",
        "ях",
        "а",
        "я",
        "ы",
        "и",
        "у",
        "ю",
        "е",
    ):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def conflicts(phrases: list[str], contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for phrase in phrases:
        words = _tokens(phrase)
        if not words:
            continue
        for context in contexts:
            tokens = [token.lstrip("!") for token in _tokens(str(context.get("text", "")))]
            if all(
                any(
                    word[1:] == token
                    if word.startswith("!")
                    else word == token or _stem(word) == _stem(token)
                    for token in tokens
                )
                for word in words
            ):
                result.append({"phrase": phrase, "context": context, "match": "review_candidate"})
                break
    return result


def plan_contexts(campaign: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for group in campaign.get("groups", []):
        group_name = group.get("ad_group", {}).get("Name")
        if group_name:
            result.append({"kind": "group_name", "text": group_name})
        for row in group.get("keywords", []):
            text = row.get("Keyword", "")
            if text != "---autotargeting":
                # Inline negative words do not describe the advertised product.
                result.append({"kind": "keyword", "text": re.split(r"\s+-", text)[0]})
        for ad in group.get("ads", []):
            body = ad.get("ResponsiveAd", {})
            for key in ("Titles", "Texts"):
                for text in body.get(key, []):
                    result.append({"kind": key.lower(), "text": text})
    return result


def audit_contexts(
    ad_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
    landing_pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for row in ad_rows:
        for key in ("titles", "texts"):
            for text in row.get(key) or []:
                result.append({"kind": key, "text": text, "ad_id": row.get("id")})
    for row in keyword_rows:
        text = row.get("keyword") or row.get("text") or ""
        if text and text != "---autotargeting":
            result.append({"kind": "keyword", "text": re.split(r"\s+-", text)[0]})
    for row in landing_pages:
        checked = row.get("site_check") or row
        for key in ("title", "meta_description", "description", "h1", "text_preview"):
            value = checked.get(key)
            for text in value if isinstance(value, list) else [value]:
                if isinstance(text, str) and text:
                    result.append({"kind": "landing", "text": text, "url": row.get("url")})
    return result
