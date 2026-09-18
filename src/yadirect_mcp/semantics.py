"""Structure and source evidence for new plans; never fetches or invents research."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import date
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import policy, products

AUTOTARGETING = "---autotargeting"
BUSINESS_SOURCES = {"website", "catalogue", "client_brief"}
QUERY_SOURCES = {"wordstat", "search_query_report", "metrika", "site_search", "crm"}
SOURCE_TYPES = BUSINESS_SOURCES | QUERY_SOURCES | {"suggestions", "competitor", "llm"}
# Conservative common function words, not a claim to reproduce Yandex morphology.
STOP_WORDS = frozenset(
    ["а", "без", "в", "во", "для", "до", "за", "и", "из", "к", "ко", "на", "над",
     "о", "об", "от", "по", "под", "при", "про", "с", "со", "у", "через", "что",
     "чтобы", "это", "как", "the", "a", "an", "and", "or", "of", "for", "to",
     "in", "on", "with", "at", "from"]
)


def phrase_key(value: str) -> str:
    """Keep meaningful operators; detect only certain textual duplicates."""
    return " ".join(value.casefold().split())


def landing_key(value: str) -> str:
    """Ignore tracking and fragments, preserve product-defining query parameters."""
    parts = urlsplit(value.strip())
    query = sorted(
        (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"yclid", "gclid"}
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path or "/", urlencode(query), ""))


def keyword_word_count(value: str) -> int:
    # Inline negatives do not count toward the seven positive words.
    positive = re.sub(r"(?<!\S)-[^\s]+", "", value)
    stack = [[0, 0]]  # Current branch and largest alternative, per parentheses level.
    for token in re.findall(r"[()|]|[^\W_]+(?:-[^\W_]+)*", positive.casefold()):
        if token == "(":
            stack.append([0, 0])
        elif token == "|":
            stack[-1] = [0, max(stack[-1])]
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("Несогласованные скобки в ключевой фразе")
            branch = max(stack.pop())
            stack[-1][0] += branch
        elif token not in STOP_WORDS:
            stack[-1][0] += 1
    if len(stack) != 1:
        raise ValueError("Несогласованные скобки в ключевой фразе")
    return max(stack[0])


def validate_keywords(rows: list[dict[str, Any]], prefix: str) -> None:
    ordinary = [row for row in rows if row.get("Keyword") != AUTOTARGETING]
    if len(ordinary) > policy.KEYWORDS_MAX_PER_GROUP:
        raise ValueError(f"{prefix}: не более 200 ключевых фраз на группу")
    if len(rows) - len(ordinary) > 1:
        raise ValueError(f"{prefix}: не более одного автотаргетинга на группу")
    seen = set()
    for row in ordinary:
        phrase = row.get("Keyword")
        if not isinstance(phrase, str) or not phrase.strip():
            raise ValueError(f"{prefix}: ключевая фраза должна быть непустой строкой")
        if len(phrase.replace("-!", "-")) > policy.KEYWORD_MAX_LENGTH:
            raise ValueError(f"{prefix}: ключевая фраза длиннее 4096 символов")
        if keyword_word_count(phrase) > policy.KEYWORD_MAX_WORDS:
            raise ValueError(f"{prefix}: больше 7 слов без учёта минус-слов и стоп-слов")
        key = phrase_key(phrase)
        if key in seen:
            raise ValueError(f"{prefix}: дубликат ключевой фразы {phrase!r}")
        seen.add(key)


def validate_plan_limits(campaigns: list[dict[str, Any]]) -> None:
    for campaign in campaigns:
        groups = campaign["groups"]
        if not 1 <= len(groups) <= policy.GROUPS_MAX_PER_CAMPAIGN:
            raise ValueError("Кампания должна содержать от 1 до 1000 групп")
        for group in groups:
            validate_keywords(group["keywords"], group["ad_group"]["Name"])


def counts(campaigns: list[dict[str, Any]]) -> dict[str, int]:
    rows = [row for campaign in campaigns for group in campaign["groups"]
            for row in group["keywords"]]
    auto = sum(row.get("Keyword") == AUTOTARGETING for row in rows)
    return {"keywords": len(rows) - auto, "autotargeting": auto, "criteria": len(rows)}


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} должен быть объектом")
    unknown = value.keys() - fields
    if unknown:
        raise ValueError(f"{label}: неизвестные поля {sorted(unknown)}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} должен быть непустой строкой")
    return value.strip()


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} должен быть массивом")
    return value


def _refs(value: Any, registry: dict[str, Any], label: str) -> list[str]:
    refs = [_text(item, label) for item in _list(value, label)]
    if len(refs) != len(set(refs)) or any(ref not in registry for ref in refs):
        raise ValueError(f"{label}: повторяющиеся или неизвестные ссылки")
    return refs


def review(
    raw: Any, campaigns: list[dict[str, Any]], *, today: date, selected_policy: dict | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Check a caller's research ledger, separately from verification of its contents."""
    findings: list[dict[str, Any]] = []

    def finding(status: str, rule: str, message: str, **evidence: Any) -> None:
        findings.append({"status": status, "rule": rule, "message": message,
                         "evidence": evidence})

    def recommend(count: int, bounds: list[int], reason: Any, rule: str, target: str) -> None:
        if not bounds[0] <= count <= bounds[1]:
            explained = isinstance(reason, str) and bool(reason.strip())
            finding(policy.MANUAL if explained else policy.WARNING, rule,
                    f"{target}: {count}; стартовый ориентир {bounds[0]}–{bounds[1]}. "
                    "Зафиксируйте причину выбранного количества.",
                    target=target, count=count, recommended=bounds, reason=reason)

    if raw is None:
        finding(policy.BLOCK, "semantics.research_required",
                "Нужен semantic_plan: направления бизнеса, источники, история и кандидаты.")
        return {"status": "missing", "groups": []}, findings
    data = deepcopy(_object(raw, {"profile", "profile_reason", "business_directions",
                                  "sources", "history", "candidates",
                                  "wordstat_unavailable_reason"}, "semantic_plan"))
    if "wordstat_unavailable_reason" in data:
        _text(data["wordstat_unavailable_reason"], "semantic_plan.wordstat_unavailable_reason")
    selected = selected_policy or policy.AGENCY_POLICY_V1
    default_profile = selected["campaign_structure"]["default_profile"]
    profile = data.get("profile", default_profile)
    if ("profile" in selected and selected["profile"]["business"] == "ecommerce"
            and "profile" not in data):
        data.setdefault("profile_reason", "Товарные категории и посадочные e-commerce")
    profiles = selected["campaign_structure"]["profiles"]
    if not isinstance(profile, str) or profile not in profiles:
        raise ValueError("semantic_plan.profile: допустимы small_business и catalogue")
    if profile == "catalogue":
        _text(data.get("profile_reason"), "semantic_plan.profile_reason")
    sources = {}
    for raw_source in _list(data.get("sources"), "semantic_plan.sources"):
        source = _object(raw_source, {"id", "type", "reference", "collected_on",
                                     "region_ids", "period", "note"}, "source")
        sid = _text(source.get("id"), "source.id")
        if sid in sources:
            raise ValueError(f"Повтор source.id: {sid}")
        if not isinstance(source.get("type"), str) or source["type"] not in SOURCE_TYPES:
            raise ValueError(f"Неизвестный source.type: {source.get('type')}")
        _text(source.get("reference"), "source.reference")
        for field in ("period", "note"):
            if field in source:
                _text(source[field], f"source.{field}")
        collected = date.fromisoformat(_text(source.get("collected_on"), "source.collected_on"))
        if collected > today:
            raise ValueError("Дата источника не может быть в будущем")
        geo = _list(source.get("region_ids"), "source.region_ids")
        if any(type(item) is not int for item in geo) or len(geo) != len(set(geo)):
            raise ValueError("source.region_ids: нужны уникальные целочисленные ID")
        source["evidence_status"] = "provided_in_bundle"
        sources[sid] = source
    directions = {}
    for row in _list(data.get("business_directions"), "semantic_plan.business_directions"):
        row = _object(row, {"id", "name", "excluded_reason"}, "business_direction")
        did = _text(row.get("id"), "business_direction.id")
        _text(row.get("name"), "business_direction.name")
        if "excluded_reason" in row:
            _text(row["excluded_reason"], "business_direction.excluded_reason")
        if did in directions:
            raise ValueError(f"Повтор business_direction.id: {did}")
        directions[did] = row
    if not directions:
        finding(policy.BLOCK, "structure.coverage", "Не перечислены направления бизнеса.")
    history = _object(data.get("history"), {"status", "source_ids", "reason"}, "history")
    if history.get("status") == "available":
        refs = _refs(history.get("source_ids"), sources, "history.source_ids")
        if not any(sources[ref]["type"] == "search_query_report" for ref in refs):
            finding(policy.BLOCK, "semantics.history",
                    "Доступная история требует источника с фактическими поисковыми запросами.")
        _text(history.get("reason"), "history.reason: результат разбора истории")
    elif history.get("status") in {"new_account", "unavailable"}:
        _text(history.get("reason"), "history.reason")
        if history.get("source_ids"):
            raise ValueError("Для недоступной истории source_ids должны быть пустыми")
    else:
        raise ValueError("history.status: available, new_account или unavailable")
    candidates = {}
    for row in _list(data.get("candidates"), "semantic_plan.candidates"):
        row = _object(row, {"id", "phrase", "source_ids", "decision", "reason",
                           "hypothesis_reason"}, "candidate")
        cid = _text(row.get("id"), "candidate.id")
        _text(row.get("phrase"), "candidate.phrase")
        _text(row.get("reason"), "candidate.reason")
        if cid in candidates:
            raise ValueError(f"Повтор candidate.id: {cid}")
        if row.get("decision") not in {"selected", "excluded", "pending"}:
            raise ValueError("candidate.decision: selected, excluded или pending")
        refs = _refs(row.get("source_ids"), sources, "candidate.source_ids")
        if not refs:
            raise ValueError(f"Кандидат {cid}: укажите источник")
        observed = any(sources[ref]["type"] in QUERY_SOURCES for ref in refs)
        row["evidence_kind"] = "query_source" if observed else "hypothesis"
        if not observed and row["decision"] == "selected":
            _text(row.get("hypothesis_reason"), "candidate.hypothesis_reason")
            finding(policy.MANUAL, "semantics.hypothesis",
                    "Фраза включена как гипотеза; подтверждённый спрос не заявляется.",
                    candidate_id=cid, phrase=row["phrase"], reason=row["hypothesis_reason"])
        candidates[cid] = row
    used_directions: set[str] = set()
    used_candidates: set[str] = set()
    group_reviews = []
    seen_groups: dict[tuple[Any, ...], str] = {}
    for ci, campaign in enumerate(campaigns):
        channel = campaign["channel"]
        name = campaign["campaign"]["Name"]
        ranges = profiles[profile].get(channel)
        if ranges:
            recommend(len(campaign["groups"]), ranges["groups"],
                      campaign.get("group_count_reason"), "structure.group_count", name)
        for gi, group in enumerate(campaign["groups"]):
            target = f"{name} / {group['ad_group']['Name']}"
            raw_group = group.get("semantic")
            if raw_group is None:
                finding(policy.BLOCK, "structure.group_rationale",
                        "Нужны semantic: намерение, предложение, посадочная и источники группы.",
                        target=target)
                continue
            sem = _object(raw_group, {"direction_id", "intent", "offer", "landing_url",
                                      "split_reason", "brand_segment", "business_source_ids",
                                      "candidate_ids", "keyword_count_reason",
                                      "source_geo_reason"}, "group.semantic")
            for field in ("direction_id", "intent", "offer", "landing_url", "split_reason"):
                _text(sem.get(field), f"group.semantic.{field}")
            for field in ("keyword_count_reason", "source_geo_reason"):
                if field in sem:
                    _text(sem[field], f"group.semantic.{field}")
            landing_parts = urlsplit(sem["landing_url"])
            if landing_parts.scheme not in {"https", "http"} or not landing_parts.netloc:
                raise ValueError("group.semantic.landing_url должен быть полным HTTP(S) URL")
            if sem["direction_id"] not in directions:
                raise ValueError(f"Неизвестное направление {sem['direction_id']}")
            used_directions.add(sem["direction_id"])
            if sem.get("brand_segment") not in {"generic", "own_brand", "competitors"}:
                raise ValueError("brand_segment: generic, own_brand или competitors")
            business_refs = _refs(sem.get("business_source_ids"), sources,
                                  "group.semantic.business_source_ids")
            if not any(sources[ref]["type"] in BUSINESS_SOURCES for ref in business_refs):
                finding(policy.BLOCK, "semantics.business_source",
                        "Предложение группы должно подтверждаться сайтом, каталогом или брифом.",
                        target=target)
            hrefs = {landing_key(products.payload(ad)["Href"]) for ad in group["ads"]
                     if products.payload(ad).get("Href")}
            expected_landing = landing_key(sem["landing_url"])
            if hrefs and hrefs != {expected_landing}:
                finding(policy.BLOCK, "structure.landing",
                        "Основные ссылки объявлений группы должны вести на одну посадочную.",
                        target=target, actual=sorted(hrefs), expected=expected_landing)
            ids = _refs(sem.get("candidate_ids"), candidates, "group.semantic.candidate_ids")
            selected = [candidates[cid] for cid in ids]
            phrases = [row["Keyword"] for row in group["keywords"]
                       if row["Keyword"] != AUTOTARGETING]
            if (any(row["decision"] != "selected" for row in selected)
                    or sorted(phrase_key(row["phrase"]) for row in selected)
                    != sorted(phrase_key(phrase) for phrase in phrases)):
                finding(policy.BLOCK, "semantics.keyword_evidence",
                        "Каждому ключу группы должен соответствовать один выбранный кандидат.",
                        target=target)
            used_candidates.update(ids)
            if ranges:
                recommend(len(phrases), ranges["keywords_per_group"],
                          sem.get("keyword_count_reason"), "semantics.keyword_count", target)
            geo = sorted(set(group["ad_group"]["RegionIds"]))
            referenced = {ref for row in selected for ref in row["source_ids"]}
            different_geo = set()
            for candidate in selected:
                measured_refs = [ref for ref in candidate["source_ids"]
                                 if sources[ref]["type"] in {"wordstat", "search_query_report"}]
                if measured_refs and not any(sorted(sources[ref]["region_ids"]) == geo
                                             for ref in measured_refs):
                    different_geo.update(measured_refs)
            if different_geo:
                reason = sem.get("source_geo_reason")
                finding(policy.MANUAL if reason else policy.WARNING,
                        "semantics.source_geography",
                        "География источников отличается от группы; оцените применимость данных.",
                        target=target, source_ids=sorted(different_geo), reason=reason)
            regional_ws = any(sources[ref]["type"] == "wordstat"
                              and sorted(sources[ref]["region_ids"]) == geo for ref in referenced)
            if channel in {"search", "maps"} and not regional_ws:
                reason = (sem.get("source_geo_reason") or data.get("wordstat_unavailable_reason"))
                finding(policy.MANUAL if reason else policy.BLOCK, "semantics.regional_wordstat",
                        "Для группы нужен Wordstat по её географии либо причина отсутствия данных.",
                        target=target, region_ids=geo, reason=reason)
            if channel in {"search", "maps"}:
                auto = next((row for row in group["keywords"]
                             if row["Keyword"] == AUTOTARGETING), {})
                brand = auto.get("AutotargetingSettings", {}).get("BrandOptions", {})
                expected_brand = {"generic": "WithoutBrands", "own_brand": "WithAdvertiserBrand",
                                  "competitors": "WithCompetitorsBrand"}[sem["brand_segment"]]
                if any(value == "YES" and key != expected_brand for key, value in brand.items()):
                    finding(policy.BLOCK, "structure.brand_segment",
                            "Настройки брендов автотаргетинга смешивают разные сегменты спроса.",
                            target=target, expected=expected_brand, actual=brand)
            hypotheses = group.get("ad_hypotheses", [])
            if channel != "product" and len(group["ads"]) > 1 and (
                len(hypotheses) != len(group["ads"])
                or any(not isinstance(item, str) or not item.strip() for item in hypotheses)
                or len({phrase_key(item) for item in hypotheses if isinstance(item, str)})
                != len(hypotheses)
            ):
                finding(policy.BLOCK, "ads.distinct_hypotheses",
                        "Для нескольких объявлений нужны разные содержательные гипотезы.",
                        target=target)
            signature = (ci, sem["direction_id"], phrase_key(sem["intent"]), expected_landing,
                         sem["brand_segment"], tuple(geo))
            if signature in seen_groups:
                finding(policy.MANUAL, "structure.possible_fragmentation",
                        "Проверьте разделение групп с одинаковым намерением и посадочной.",
                        target=target, other=seen_groups[signature], reason=sem["split_reason"])
            seen_groups[signature] = target
            group_reviews.append({"campaign_index": ci, "group_index": gi, "group": target,
                                  **deepcopy(sem), "keywords_count": len(phrases),
                                  "candidates": deepcopy(selected),
                                  "ad_hypotheses": hypotheses,
                                  "review_status": "declared_requires_semantic_review"})
    for did, direction in directions.items():
        if did not in used_directions and not direction.get("excluded_reason"):
            finding(policy.BLOCK, "structure.coverage",
                    "Направление бизнеса нужно представить группой или исключить с причиной.",
                    direction=direction)
        if did in used_directions and direction.get("excluded_reason"):
            finding(policy.BLOCK, "structure.coverage",
                    "Исключённое направление одновременно используется в группе.",
                    direction=direction)
    for cid, candidate in candidates.items():
        if candidate["decision"] == "selected" and cid not in used_candidates:
            finding(policy.BLOCK, "semantics.unassigned_candidate",
                    "Выбранный кандидат не назначен ни одной группе.", candidate_id=cid)
    finding(policy.MANUAL, "semantics.content_review",
            "Проверить смысловую близость фраз, предложение и источники. "
            "Наличие ссылок не доказывает чтение источников или полноту спроса.")
    return {
        "profile": profile, "profile_reason": data.get("profile_reason"),
        "sources": list(sources.values()), "history": history,
        "business_directions": list(directions.values()), "groups": group_reviews,
        "candidates": list(candidates.values()),
        "counts": {
            "candidate_records": len(candidates),
            "found_unique_phrases": len({phrase_key(row["phrase"]) for row in candidates.values()}),
            "selected_unique_phrases": len({phrase_key(row["phrase"]) for row in candidates.values()
                                             if row["decision"] == "selected"}),
            "excluded_candidates": sum(row["decision"] == "excluded"
                                       for row in candidates.values()),
            "pending_candidates": sum(row["decision"] == "pending" for row in candidates.values()),
            "selected_hypotheses": sum(row["decision"] == "selected"
                                       and row["evidence_kind"] == "hypothesis"
                                       for row in candidates.values()),
        },
        "evidence_status": "provided_in_bundle",
    }, findings


def cross_group_checks(campaign: dict) -> list[dict]:
    """Exact duplicates and bounded lexical overlaps; never invent morphology or apply negatives."""
    indexed, simple, postings = {}, [], {}
    for gi, group in enumerate(campaign["groups"]):
        for row in group["keywords"]:
            phrase = phrase_key(row["Keyword"])
            if phrase == AUTOTARGETING:
                continue
            indexed.setdefault(phrase, []).append(gi)
            if re.fullmatch(r"[^\W_]+(?:\s+[^\W_]+)*", phrase):
                tokens = frozenset(phrase.split())
                index = len(simple)
                simple.append((gi, phrase, tokens))
                for word in tokens:
                    postings.setdefault(word, set()).add(index)
    names = [g["ad_group"]["Name"] for g in campaign["groups"]]
    duplicate = [{"phrase": phrase, "groups": [names[i] for i in sorted(set(indices))]}
                 for phrase, indices in indexed.items() if len(set(indices)) > 1]
    findings = []
    if duplicate:
        findings.append({"rule": "semantics.cross_group_duplicates", "status": policy.MANUAL,
                         "message": "Проверьте одинаковые ключи в разных группах.",
                         "evidence": {"campaign": campaign["campaign"]["Name"],
                                      "duplicates": duplicate}})
    if campaign["channel"] not in {"search", "maps"}:
        return findings
    overlaps = []
    for gi, broad, tokens in simple:
        candidates = postings[min(tokens, key=lambda word: len(postings[word]))]
        negatives = [frozenset(phrase_key(v).split()) for v in
                     campaign["groups"][gi]["ad_group"].get("NegativeKeywords", {}).get("Items", [])
                     if re.fullmatch(r"[^\W_]+(?:\s+[^\W_]+)*", v)]
        for index in sorted(candidates):
            other, narrow, other_tokens = simple[index]
            if gi == other or not tokens < other_tokens:
                continue
            if any(negative <= other_tokens for negative in negatives):
                continue
            overlaps.append({"broad_group": names[gi], "broad_phrase": broad,
                             "narrow_group": names[other], "narrow_phrase": narrow,
                             "review_negative_words": sorted(other_tokens - tokens)})
            if len(overlaps) >= 200:
                break
        if len(overlaps) >= 200:
            break
    if overlaps:
        findings.append(
            {
                "rule": "semantics.cross_minusing_review",
                "status": policy.MANUAL,
                "message": "Проверьте пересечение широких и узких ключей. "
                "Минус-слова предложены для смысловой проверки, не для автозаписи.",
                "evidence": {
                    "campaign": campaign["campaign"]["Name"],
                    "overlaps": overlaps,
                    "limit_reached": len(overlaps) == 200,
                    "scope": "literal_words_without_operators_or_morphology",
                },
            }
        )
    return findings
