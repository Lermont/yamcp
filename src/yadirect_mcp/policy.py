"""Версионируемые агентские правила настройки Яндекс Директа.

Документ человека остаётся первичным источником требований, но код не должен
угадывать их по тексту при каждом запуске. Здесь лежит стабильное представление
политики, пригодное для preview, аудита и компилятора кампаний.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import profiles

PASS = "PASS"
WARNING = "WARNING"
BLOCK = "BLOCK"
MANUAL = "MANUAL"
STATUSES = (PASS, WARNING, BLOCK, MANUAL)

RESPONSIVE_TITLE_MAX_LENGTH = 56
RESPONSIVE_TITLE_NEAR_LIMIT_MIN_LENGTH = 45
RESPONSIVE_TITLE_NEAR_LIMIT_REQUIRED = 3
RESPONSIVE_ADS_MAX_NON_ARCHIVED = 3
RESPONSIVE_ADS_MAX_TOTAL = 10
NETWORK_IMAGES_MINIMUM = 3
RESPONSIVE_IMAGES_MAXIMUM = 5
GROUPS_MAX_PER_CAMPAIGN = 1000
KEYWORDS_MAX_PER_GROUP = 200
KEYWORD_MAX_WORDS = 7
KEYWORD_MAX_LENGTH = 4096

SITELINKS_MINIMUM = 4
SITELINKS_RECOMMENDED = 8
SITELINKS_MAXIMUM = 8


def responsive_title_utilization(titles: list[Any]) -> dict[str, Any]:
    """Measure whether a responsive ad meaningfully uses the title allowance."""
    lengths = [len(str(value).strip()) for value in titles]
    required = min(RESPONSIVE_TITLE_NEAR_LIMIT_REQUIRED, len(lengths))
    near_limit_count = sum(
        RESPONSIVE_TITLE_NEAR_LIMIT_MIN_LENGTH <= length <= RESPONSIVE_TITLE_MAX_LENGTH
        for length in lengths
    )
    return {
        "lengths": lengths,
        "minimum_length": min(lengths, default=0),
        "maximum_length": max(lengths, default=0),
        "average_length": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
        "near_limit_count": near_limit_count,
        "required_near_limit_count": required,
        "passes": len(lengths) >= 3 and near_limit_count >= required,
    }


TRACKING_PARAMS_V1 = {
    "type": "{source_type}",
    "source": "{source}",
    "block": "{position_type}",
    "pos": "{position}",
    "key": "{keyword}",
    "campaign": "{campaign_id}",
    "yd_campaign_name": "{campaign_name}",
    "name_lat": "{campaign_name_lat}",
    "retargeting": "{retargeting_id}",
    "ad": "{ad_id}",
    "phrase": "{phrase_id}",
    "gbid": "{gbid}",
    "device": "{device_type}",
    "region": "{region_id}",
    "region_name": "{region_name}",
}

TRACKING_PARAMS_UTM_V1 = {
    **TRACKING_PARAMS_V1,
    "utm_source": "yandex",
    "utm_medium": "cpc",
    "utm_campaign": "{campaign_id}",
    "utm_content": "{ad_id}",
    "utm_term": "{phrase_id}",
}

# Эти слова присутствуют в базовом списке регламента, но могут отсечь
# коммерчески полезный информационный спрос. Их наличие не является ошибкой:
# требуется решение специалиста для конкретной семантики.
RISKY_NEGATIVES = frozenset({
    "бесплатно",
    "видео",
    "выбрать",
    "где",
    "зачем",
    "инструкция",
    "как",
    "когда",
    "маркетинг",
    "обзор",
    "подобрать",
    "почему",
    "пример",
    "рекомендации",
    "руководство",
    "советы",
    "тест",
    "фото",
})

AGENCY_POLICY_V1: dict[str, Any] = {
    "name": "agency_default_v1",
    "version": "1.12.0",
    "execution_workflow": {
        "questions": "missing_information_at_start",
        "budget_approval_required": False,
        "campaign_plan_approval_required": False,
        "creation_approval_required": False,
        "report_publication_approval_required": False,
        "known_report_destination_required": True,
        "launch_requires_explicit_user_instruction": True,
        "preview_token": "internal_plan_integrity_not_human_consent",
        "source": "explicit_user_instruction",
        "approved_on": "2026-09-11",
    },
    "browser_workflow": {
        "engine": "playwright",
        "browser_channel": "chrome",
        "scope": "all_browser_actions",
        "separate_authenticated_profile": True,
        "fallback_requires_explicit_user_request": True,
        "verification": "save_reopen_and_visual_review",
        "source": "explicit_user_instruction",
        "approved_on": "2026-09-11",
    },
    "status_model": {
        PASS: "требование подтверждено данными API",
        WARNING: "допустимо только после осознанного решения специалиста",
        BLOCK: "план или настройка противоречат обязательному правилу",
        MANUAL: "API не даёт достаточно данных, нужна ручная проверка",
    },
    "sources": {
        "action_button": {
            "url": "https://yandex.ru/support/direct/ru/efficiency/button",
            "api_reference": "https://yandex.ru/dev/direct/doc/ru/ads/add",
            "verified_on": "2026-09-10",
        },
        "responsive_group_limits": {
            "url": "https://yandex.ru/support/direct/ru/unified-performance-campaign/create-comb-ad",
            "verified_on": "2026-09-05",
        },
        "report_conversion_contract": {
            "url": "https://yandex.ru/dev/direct/doc/ru/spec",
            "verified_on": "2026-09-05",
        },
        "network_carousel": {
            "url": "https://yandex.ru/support/direct/ru/efficiency/carousel",
            "verified_on": "2026-09-04",
            "api_reference": "https://yandex.ru/dev/direct/doc/ru/ads/add",
        },
        "geotargeting_update": {
            "url": "https://b2b.yandex.ru/adv/news/obnovlenie-geotargetinga-v-direkte",
            "published_on": "2026-08-31",
            "verified_on": "2026-09-04",
            "api_reference": "https://yandex.ru/dev/direct/doc/ru/annex/campaign-options",
        },
        "regulation": {
            "url": "https://docs.google.com/document/d/1eegjaMrZmx9C_a1EXd72zL5_SHJ-bQUON2qoC4JVjZM/edit?tab=t.0",
            "observed_modified_on": "2025-09-29",
        },
        "search_negative_keywords": {
            "url": "https://docs.google.com/spreadsheets/d/1emyOx8EGWt2BgSdi9JUSMWfRYj8qOzGs97gT_vjmIVE/edit?gid=0",
            "observed_modified_on": "2025-09-17",
            "rows": 250,
            "unique_items": 249,
            "snapshot_state": "embedded",
            "snapshot_file": "search_negative_keywords_v1.json",
            "sha256": "0066111357f9ba2dfb1367b99679d19cb5d15e9280fbdcba0ff1ee6a07beb263",
        },
        "network_excluded_sites": {
            "url": "https://docs.google.com/spreadsheets/d/1TSal-cuTprcY8yin5UznwbmMOLpm107kYTkYUYthCLE/edit",
            "observed_modified_on": "2025-12-15",
            "unique_items": 248,
            "snapshot_state": "embedded",
            "snapshot_file": "network_excluded_sites_v1.json",
            "sha256": "febd80318a8bbeaf62c7351aea6db07219bf9bbe5bb8b76c7bc0a26e87f2cc5a",
        },
    },
    "campaign_structure": {
        "required_channels": ["search"],
        "optional_channels": ["network"],
        "conditional_channel": "maps_if_office",
        "separate_campaigns": True,
        "groups_maximum": GROUPS_MAX_PER_CAMPAIGN,
        "group_principle": "one_intent_offer_landing",
        "split_by": ["product", "purchase_scenario", "landing", "offer", "brand_segment"],
        "geo_split": "only_when_offer_economics_or_management_differ",
        "separate_campaign_when": ["budget", "strategy", "goals"],
        "low_frequency": "merge_only_semantically_related",
        "default_profile": "small_business",
        "profiles": {
            "small_business": {
                "search": {"groups": [3, 10], "keywords_per_group": [10, 30]},
                "network": {"groups": [2, 5], "keywords_per_group": [5, 15]},
            },
            "catalogue": {"requires_reason": True, "fixed_recommended_counts": False},
        },
        "range_enforcement": "recommendation_with_explanation",
        "coverage": "each_business_direction_grouped_or_excluded_with_reason",
    },
    "semantics": {
        "required_in_new_bundle": True,
        "keywords_maximum": KEYWORDS_MAX_PER_GROUP,
        "keyword_max_words": KEYWORD_MAX_WORDS,
        "keyword_max_length": KEYWORD_MAX_LENGTH,
        "campaign_keyword_minimum": None,
        "source_fields": ["id", "type", "reference", "collected_on", "region_ids"],
        "business_evidence_required": True,
        "wordstat": "regional_research_or_documented_unavailability",
        "search_history": "review_if_available_otherwise_record_reason",
        "generated_phrases": "hypotheses_until_supported_by_observed_queries",
        "frequency": "zero_missing_and_no_results_are_distinct",
        "zero_frequency_action": "review_not_automatic_exclusion",
        "sum_nested_frequencies": False,
        "autotargeting_counted_separately": True,
        "evidence_verification": "references_provided_by_caller_not_independently_verified",
    },
    "geotargeting": {
        "region_level": "ad_group",
        "note": "Проверяются регионы показа на уровне групп.",
    },
    "budget": {
        "client_limit_scope": "planned_campaigns",
        "client_limit_field": "client_budget",
        "monthly_conversion": "amount * 12 / 52; not a calendar month spend cap",
        "period": "weekly",
        "minimum": 2000,
        "maximum": 5000,
        "range_enforcement": "warning",
        "units": "account_currency",
        "spend_constraint": "budget",
        "forbidden_default_constraints": ["target_cpa"],
    },
    "strategy": {
        "recommended": ["WB_MAXIMUM_CLICKS", "WB_MAXIMUM_CONVERSION_RATE"],
        "allowed_with_warning": ["HIGHEST_POSITION", "NETWORK_DEFAULT"],
        "blocked": [
            "AVERAGE_CPA",
            "AVERAGE_CRR",
            "PAY_FOR_CONVERSION",
            "PAY_FOR_CONVERSION_CRR",
        ],
    },
    "creative": {
        "responsive_ads_max_non_archived": RESPONSIVE_ADS_MAX_NON_ARCHIVED,
        "responsive_ads_max_total": RESPONSIVE_ADS_MAX_TOTAL,
        "responsive_ads_recommended_start": 1,
        "responsive_titles_recommended": 7,
        "additional_ads_require_distinct_hypotheses": True,
        "network_images": {
            "minimum": NETWORK_IMAGES_MINIMUM,
            "maximum": RESPONSIVE_IMAGES_MAXIMUM,
            "unique_required": True,
            "generation_minimum": NETWORK_IMAGES_MINIMUM,
            "generation": "agent_image_tool_or_direct_ui",
            "server_generates_images": False,
            "verification": "api_readback_hashes_and_ui_visual_review",
        },
        "action_button": {
            "required_with_href": True,
            "selection": "most_relevant_available_ui_label_for_offer",
            "reason_required": True,
            "destinations": ["main", "contacts"],
            "default_href": "ad_href",
            "contacts_url": "explicit_verified_same_site_page",
            "api_supported": False,
            "verification": "saved_ui_per_ad",
        },
        "network_carousel": {
            "required": True,
            "minimum_images": 2,
            "maximum_images": 10,
            "main_image_required": True,
            "minimum_side_px": 450,
            "maximum_file_mb": 10,
            "aspect_ratio_difference_exclusive_max": 0.15,
            "verification": "saved_ui_per_ad",
            "api_supported": False,
            "ordinary_ad_image_hashes_satisfy_requirement": False,
        },
        "sitelinks_minimum": SITELINKS_MINIMUM,
        "sitelinks_recommended": SITELINKS_RECOMMENDED,
        "sitelinks_maximum": SITELINKS_MAXIMUM,
        "responsive_title_max_length": RESPONSIVE_TITLE_MAX_LENGTH,
        "responsive_title_near_limit_min_length": (
            RESPONSIVE_TITLE_NEAR_LIMIT_MIN_LENGTH
        ),
        "responsive_title_near_limit_required": (
            RESPONSIVE_TITLE_NEAR_LIMIT_REQUIRED
        ),
    },
    "goals": {
        "selection": "explicit_business_goals",
        "optional_for_strategies": ["WB_MAXIMUM_CLICKS"],
        "manual_review_when": "priority_goals_selected",
        "maximum_recommended": 5,
        "requires_manual_review": True,
    },
    "negative_keywords": {
        "default_profile": "custom",
        "universal_required_list": False,
        "profiles": ["custom", "rental", "education", "toys", "legacy_reviewed"],
        "client_exclusions_require_reason": True,
        "context_conflicts_require_review": True,
    },
    "reporting": {
        "conversion_scope_default": "campaign",
        "goals_default": "resolve_strategy_then_priority_goals",
        "attribution_default": "resolve_campaign_settings",
        "mixed_settings": "split_campaigns_or_explicit_comparison",
        "all_goals_requires_explicit_scope": True,
        "include_vat_default": True,
        "metadata_sidecar": True,
        "client_report": {
            "required": True,
            "format": "single_report_setup_then_statistics",
            "create_at": "campaign_setup",
            "update_at": "statistics_reporting",
            "canonical_url": "https://bi-data.ru/elama/{client_login}/",
            "preserve_setup": True,
            "preserve_previous_periods": True,
            "read_existing_before_update": True,
            "backup_before_update": True,
            "navigation": ["Настройка", "Статистика"],
            "navigation_location": "left_sidebar",
            "template": "templates/client-report",
            "knowledge_resource": "direct://kb/client-report",
            "enforcement": "agent_workflow",
            "refresh_tool": "direct_client_report",
            "statistics_and_html": "deterministic_code",
            "ai_input": "bounded_brief_only",
            "ai_output": "title_and_text_bound_to_data_revision",
            "daily_runner": "python -m yadirect_mcp.report_runner refresh-all",
            "runner_installs_schedule": False,
            "legacy_create_report_satisfies_requirement": False,
            "contacts": {
                "telegram": "https://t.me/SergeyMushtuk",
                "max_phone": "+79099994402",
                "email": "hello@mediatargeting.agency",
                "phone": "+74951961160",
            },
            "approved_on": "2026-09-06",
            "source": "explicit_user_instruction",
        },
    },
    "tracking_default_profile": "utm_v1",
    "alternative_texts_default": False,
    "business_profile_verification": ["IsPublished", "Phone", "Address", "HasOffice"],
    "tracking_profiles": {
        "utm_v1": {"level": "campaign", "params": TRACKING_PARAMS_UTM_V1},
        "regulation_v1": {
            "level": "campaign",
            "params": TRACKING_PARAMS_V1,
        }
    },
    "landing_url_policy": {
        "required": True,
        "scope": ["main", "sitelink", "button"],
        "button_url_readback": "saved_ui_per_ad",
        "check_effective_tracking": True,
        "devices": ["desktop", "mobile"],
        "unknown_or_truncated": BLOCK,
        "soft_404": BLOCK,
        "manual_acknowledgement_is_not_http_evidence": True,
        "campaign_name_parameter": "yd_campaign_name",
        "approved_on": "2026-09-07",
    },
    "rules": [
        {"id": "landing.effective_urls", "failure_status": BLOCK},
        {"id": "ads.sitelink_count", "failure_status": BLOCK},
        {"id": "structure.separate_channels", "failure_status": BLOCK},
        {"id": "budget.weekly_range", "failure_status": WARNING},
        {"id": "strategy.no_target_cpa", "failure_status": BLOCK},
        {"id": "tracking.campaign_profile", "failure_status": BLOCK},
        {"id": "groups.region_ids", "failure_status": BLOCK},
        {"id": "groups.tracking_override", "failure_status": WARNING},
        {"id": "structure.coverage", "failure_status": BLOCK},
        {"id": "structure.group_rationale", "failure_status": BLOCK},
        {"id": "structure.landing", "failure_status": BLOCK},
        {"id": "structure.brand_segment", "failure_status": BLOCK},
        {"id": "structure.group_count", "failure_status": WARNING},
        {"id": "semantics.keyword_count", "failure_status": WARNING},
        {"id": "semantics.research_required", "failure_status": BLOCK},
        {"id": "semantics.keyword_evidence", "failure_status": BLOCK},
        {"id": "semantics.history", "failure_status": BLOCK},
        {"id": "semantics.regional_wordstat", "failure_status": BLOCK},
        {"id": "semantics.source_geography", "failure_status": WARNING},
        {"id": "semantics.content_review", "failure_status": MANUAL},
        {"id": "semantics.evidence_unavailable", "failure_status": MANUAL},
        {"id": "ads.distinct_hypotheses", "failure_status": BLOCK},
        {"id": "search.negative_keywords", "failure_status": MANUAL},
        {"id": "network.excluded_sites", "failure_status": BLOCK},
        {"id": "network.carousel", "failure_status": MANUAL},
        {"id": "ads.network_image", "failure_status": BLOCK},
        {"id": "ads.action_button", "failure_status": MANUAL},
        {"id": "ads.action_button_selection", "failure_status": BLOCK},
        {"id": "goals.explicit_business_selection", "failure_status": MANUAL},
        {"id": "maps.office_required", "failure_status": MANUAL},
    ],
}

def is_maximum_clicks_strategy(strategy: dict[str, Any] | None) -> bool:
    """Allow missing Metrika only when every active API branch is maximum clicks."""
    if not isinstance(strategy, dict):
        return False
    types = []
    for channel in ("Search", "Network"):
        branch = strategy.get(channel)
        if not isinstance(branch, dict):
            return False
        types.append(branch.get("BiddingStrategyType"))
    return "WB_MAXIMUM_CLICKS" in types and all(
        value in {"WB_MAXIMUM_CLICKS", "SERVING_OFF"} for value in types
    )


def network_carousel_requirement(**target: Any) -> dict[str, Any]:
    """Track a real UI carousel separately from ordinary API image variants."""
    return {
        "rule": "network.carousel",
        "required": True,
        "status": "pending_ui",
        "verification_method": "saved_ui_per_ad",
        "requirements": copy.deepcopy(AGENCY_POLICY_V1["creative"]["network_carousel"]),
        "message": (
            "Добавить и сохранить карусель из 2–10 релевантных изображений, "
            "затем повторно открыть объявление и проверить число и порядок слайдов. "
            "AdImageHashes и обычное изображение не подтверждают карусель."
        ),
        **target,
    }


DATA_DIR = Path(__file__).parent / "policy_data"
_LEGACY_BYTES = (DATA_DIR / "agency_default_v1_1_12_0.json").read_bytes()
_LEGACY_SHA256 = "ad4a95cafff5d0e5a109376b7ac92bcb9fcee1f88df771d3d8a7583b84fa0817"
if hashlib.sha256(_LEGACY_BYTES).hexdigest() != _LEGACY_SHA256:
    raise RuntimeError("Нарушена целостность зафиксированной политики 1.12.0")
_LEGACY_POLICY = json.loads(_LEGACY_BYTES)
POLICIES = {_LEGACY_POLICY["name"]: _LEGACY_POLICY, **profiles.build_policies(_LEGACY_POLICY)}


def _negative_words(value: str) -> list[str]:
    """Canonical words visible after Direct stores a negative phrase."""
    normalized = str(value).casefold().replace("ё", "е").replace("-", " ")
    return [
        token.lstrip("!+")
        for token in re.findall(r"[!+]?[^\W_]+", normalized, flags=re.UNICODE)
    ]


def _negative_word_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 3 and longer.startswith(shorter):
        return True
    prefix = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        prefix += 1
    return prefix >= 3 and prefix >= min(len(left), len(right)) - 2


def negative_phrase_equivalent(left: str, right: str) -> bool:
    """Account for Direct's visible punctuation and morphology normalization."""
    left_words = _negative_words(left)
    right_words = _negative_words(right)
    return len(left_words) == len(right_words) and all(
        _negative_word_equivalent(a, b)
        for a, b in zip(left_words, right_words, strict=True)
    )


def missing_negative_phrases(
    actual: list[str] | tuple[str, ...],
    required: list[str] | tuple[str, ...] | set[str],
) -> list[str]:
    """Required phrases not covered by the normalized API readback."""
    actual_values = list(actual)
    return sorted(
        value
        for value in required
        if not any(negative_phrase_equivalent(value, row) for row in actual_values)
    )


def get(name: str = "agency_default_v1") -> dict[str, Any]:
    """Вернуть независимую копию политики, которую вызывающий может менять."""
    try:
        return copy.deepcopy(POLICIES[name])
    except KeyError as exc:
        known = ", ".join(sorted(POLICIES))
        raise ValueError(f"Неизвестная политика {name!r}. Доступны: {known}") from exc


@lru_cache(maxsize=2)
def load_snapshot(source_name: str) -> tuple[str, ...]:
    """Загрузить снимок и проверить его контрольную сумму перед применением."""
    source = AGENCY_POLICY_V1["sources"].get(source_name)
    if not source or not source.get("snapshot_file"):
        raise ValueError(f"Для источника {source_name!r} нет встроенного снимка")
    path = DATA_DIR / source["snapshot_file"]
    raw = path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != source["sha256"]:
        raise RuntimeError(
            f"Контрольная сумма снимка {path.name} не совпадает с политикой"
        )
    values = json.loads(raw.decode("utf-8"))
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise RuntimeError(f"Некорректный формат снимка {path.name}")
    if len(values) != source["unique_items"]:
        raise RuntimeError(f"Число элементов снимка {path.name} не совпадает с политикой")
    return tuple(values)


def fingerprint(selected: dict) -> str:
    return hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def for_plan(plan: dict) -> dict:
    selected = get(plan.get("policy", {}).get("name", "agency_default_v1"))
    if "profile" in selected and (
        plan["policy"].get("version") != selected["version"]
        or plan.get("policy_snapshot") != selected
        or plan.get("policy_fingerprint") != fingerprint(selected)
    ):
        raise ValueError("Профиль/версия/снимок политики не соответствует плану")
    return selected
