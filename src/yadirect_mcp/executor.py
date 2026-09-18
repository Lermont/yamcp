"""Guarded executor для скомпилированного UnifiedCampaign-плана v501."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    account,
    adgroups,
    ads,
    api_units,
    assets,
    audience_setup,
    audit,
    campaign_setup,
    campaigns,
    completeness,
    creative,
    keywords,
    launch_checks,
    link_checks,
    phrases,
    policy,
    preflight_refs,
    products,
    profiles,
    regions,
    schedule,
    semantics,
    store,
    tracking,
    wordstat,
    workflow,
)
from .api_units import ADD_BATCH_LIMITS as UNIT_ADD_BATCH_LIMITS
from .identifiers import wire

ADD_BATCH_LIMITS = {
    "campaigns": UNIT_ADD_BATCH_LIMITS["campaigns"],
    "adgroups": UNIT_ADD_BATCH_LIMITS["ad_groups"],
    "ads": UNIT_ADD_BATCH_LIMITS["ads"],
    "keywords": UNIT_ADD_BATCH_LIMITS["keywords"],
    "bidmodifiers": UNIT_ADD_BATCH_LIMITS["bid_modifiers"],
    "retargetinglists": UNIT_ADD_BATCH_LIMITS["retargeting_lists"],
    "audiencetargets": UNIT_ADD_BATCH_LIMITS["audience_targets"],
}


PREFLIGHT_TTL = 15 * 60
_PREFLIGHT_CACHE: dict[tuple, tuple[float, dict]] = {}


class BatchFailure(RuntimeError):
    def __init__(self, original: Exception, results: list[dict[str, Any]]) -> None:
        super().__init__(str(original))
        self.original = original
        self.results = results


def _action_id(action: Any) -> int | None:
    if not isinstance(action, dict):
        return None
    value = action.get("Id")
    return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else None


def _fatal(stage: str, exc: Exception) -> dict[str, Any]:
    source = getattr(exc, "original", exc)
    result: dict[str, Any] = {"stage": stage, "message": str(source)}
    if getattr(source, "code", None) is not None:
        result["error_code"] = source.code
    if getattr(source, "request_id", None):
        result["request_id"] = source.request_id
    return result


async def _add_v501(
    api: Any,
    service: str,
    collection: str,
    objects: list[dict[str, Any]],
    client_login: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        batch_limit = ADD_BATCH_LIMITS[service]
    except KeyError as exc:
        raise ValueError(f"Неизвестный лимит add для сервиса {service}") from exc
    for offset in range(0, len(objects), batch_limit):
        chunk = objects[offset : offset + batch_limit]
        try:
            response = await api.call_v501(
                service,
                "add",
                {collection: chunk},
                client_login=client_login,
            )
            actions = response.get("AddResults", [])
            if not isinstance(actions, list) or len(actions) != len(chunk):
                raise RuntimeError(
                    f"{service}.add вернул "
                    f"{len(actions) if isinstance(actions, list) else 0} "
                    f"результатов для {len(chunk)} объектов"
                )
        except Exception as exc:
            raise BatchFailure(exc, results) from exc
        results.extend(actions)
    return results


def _planned_region_ids(plan: dict[str, Any]) -> list[int]:
    return sorted({
        abs(int(region_id))
        for campaign in plan["campaigns"]
        for group in campaign["groups"]
        for region_id in group["ad_group"]["RegionIds"]
        if int(region_id) != 0
    })


async def preflight(
    api: Any,
    plan: dict[str, Any],
    *,
    wordstat_out_dir: Path | None = None,
    wordstat_deadline_seconds: float = 60,
    reuse_expensive: bool = False,
) -> dict[str, Any]:
    """Повторно проверить внешнее состояние непосредственно перед preview/apply."""
    if not plan.get("ready"):
        raise ValueError("План содержит BLOCK-проверки и не может быть применён")
    validate_plan(plan)
    client_login = plan["client_login"]
    time_zones = {item["campaign"].get("TimeZone", "Europe/Moscow")
                  for item in plan["campaigns"]}
    if time_zones - {"Europe/Moscow"}:
        dictionary = await api.call(
            "dictionaries", "get", {"DictionaryNames": ["TimeZones"]},
            client_login=client_login,
        )
        available_zones = {row.get("TimeZone") for row in dictionary.get("TimeZones", [])}
        if time_zones - available_zones:
            raise ValueError(
                "Неизвестный TimeZone: " + ", ".join(sorted(time_zones - available_zones))
            )
    region_ids = _planned_region_ids(plan)
    region_result: dict[str, Any] = {"resolved": [], "dictionary_size": None}
    if region_ids:
        region_result = await regions.lookup(
            api,
            ids=region_ids,
            client_login=client_login,
        )
        if region_result.get("unknown_ids"):
            raise ValueError(
                "В справочнике Директа отсутствуют регионы: "
                + ", ".join(str(value) for value in region_result["unknown_ids"])
            )

    existing = await api.call_v501(
        "campaigns",
        "get",
        {
            "SelectionCriteria": {
                "States": ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED", "ARCHIVED"]
            },
            "FieldNames": ["Id", "Name", "State"],
            "TextCampaignFieldNames": ["CounterIds"],
            "UnifiedCampaignFieldNames": ["CounterIds"],
            "Page": {"Limit": 10000},
        },
        client_login=client_login,
    )
    if existing.get("LimitedBy") is not None:
        raise ValueError(
            "Preflight получил усечённый список кампаний и не может безопасно "
            "исключить дубликаты имён"
        )
    planned_names = {
        item["campaign"]["Name"].strip().casefold() for item in plan["campaigns"]
    }
    duplicates = [
        {"id": row.get("Id"), "name": row.get("Name"), "state": row.get("State")}
        for row in existing.get("Campaigns", [])
        if str(row.get("Name") or "").strip().casefold() in planned_names
    ]
    if duplicates:
        raise ValueError(
            "Кампании с такими именами уже существуют: "
            + ", ".join(str(row["name"]) for row in duplicates)
        )
    goal_ids = {
        int(goal["GoalId"])
        for item in plan["campaigns"]
        for goal in item["campaign"]["UnifiedCampaign"].get(
            "PriorityGoals", {}
        ).get("Items", [])
        if int(goal["GoalId"]) != 12
    }
    catalog_campaign_id = plan.get("goal_catalog_campaign_id")
    planned_counters = {
        int(counter) for item in plan["campaigns"]
        for counter in item["campaign"]["UnifiedCampaign"].get(
            "CounterIds", {}
        ).get("Items", [])
    }
    if not catalog_campaign_id and existing.get("Campaigns"):
        catalog_campaign_id = next(
            (
                int(row["Id"])
                for row in existing["Campaigns"]
                if row.get("Id") is not None and row.get("State") != "ARCHIVED"
                and planned_counters.intersection(
                    ((row.get("UnifiedCampaign") or row.get("TextCampaign") or {})
                     .get("CounterIds") or {}).get("Items", [])
                )
            ),
            None,
        )
    goal_check: dict[str, Any]
    if goal_ids and catalog_campaign_id:
        owns_catalog_campaign = any(
            int(row.get("Id") or 0) == catalog_campaign_id
            for row in existing.get("Campaigns", [])
        )
        if not owns_catalog_campaign:
            ownership = await api.call_v501(
                "campaigns",
                "get",
                {
                    "SelectionCriteria": {"Ids": [catalog_campaign_id]},
                    "FieldNames": ["Id"],
                    "Page": {"Limit": 1},
                },
                client_login=client_login,
            )
            owns_catalog_campaign = any(
                int(row.get("Id") or 0) == catalog_campaign_id
                for row in ownership.get("Campaigns", [])
            )
        if not owns_catalog_campaign:
            raise ValueError(
                "goal_catalog_campaign_id не принадлежит клиенту или недоступен"
            )
        from . import goals

        catalog = await goals.read(api, catalog_campaign_id)
        available = {
            int(row["id"])
            for row in catalog["goals"]
            if row.get("id") is not None
        }
        missing_goals = sorted(goal_ids - available)
        if missing_goals:
            raise ValueError(
                "В каталоге GetStatGoals отсутствуют выбранные цели: "
                + ", ".join(str(value) for value in missing_goals)
            )
        goal_check = {
            "status": policy.PASS,
            "catalog_campaign_id": catalog_campaign_id,
            "goal_ids": sorted(goal_ids),
        }
    elif goal_ids and plan.get("allow_unverified_goals"):
        goal_check = {
            "status": policy.WARNING,
            "goal_ids": sorted(goal_ids),
            "note": (
                "Нет кампании с подходящим счётчиком для GetStatGoals; применено явное "
                "allow_unverified_goals=true."
            ),
        }
    elif goal_ids:
        raise ValueError(
            "Нельзя проверить приоритетные цели: нет кампании с подходящим счётчиком "
            "для GetStatGoals. Задайте goal_catalog_campaign_id или явно "
            "allow_unverified_goals=true."
        )
    else:
        goal_check = {
            "status": policy.PASS,
            "goal_ids": [],
            "note": "Для максимума кликов без выбранных целей каталог Метрики не требуется.",
        }

    shared_ids = {
        identifier
        for c in plan["campaigns"]
        for g in c["groups"]
        for identifier in g["ad_group"].get("NegativeKeywordSharedSetIds", {}).get("Items", [])
    }
    await preflight_refs._references(api, client_login, "negativekeywordsharedsets",
                                      "NegativeKeywordSharedSets", shared_ids,
                                      fields=["Id", "Name", "NegativeKeywords"])
    product_check = await products.check_sources(api, plan)
    currency_check = await preflight_refs.currency(api, plan)
    asset_check = await preflight_refs.assets(
        api, client_login, [products.payload(ad) for c in plan["campaigns"]
                            for g in c["groups"] for ad in g["ads"]],
        business_profiles=plan.get("business_profiles"))
    counter_check = {"status": policy.MANUAL if planned_counters else policy.PASS,
                     "counter_ids": sorted(planned_counters),
                     "note": "Проверка установки через браузер/Метрику обязательна; "
                             "статический HTML не доказывает отсутствие счётчика."}
    if planned_counters and getattr(api, "metrika_available", False) is True:
        checked_counters = [
            await api.metrika_counter(counter) for counter in sorted(planned_counters)
        ]
        counter_check = {"status": policy.PASS, "counters": checked_counters,
                         "scope": "metrika_read_access_and_existence"}
    interests_check = await audience_setup.preflight(api, plan)
    cache_key = (client_login, plan["plan_hash"])
    cached = _PREFLIGHT_CACHE.get(cache_key)
    reused = bool(reuse_expensive and cached and cached[0] > time.monotonic())
    if reused:
        effective_links = deepcopy(cached[1]["links"])
        wordstat_checks = deepcopy(cached[1]["wordstat"])
    else:
        effective_links = await link_checks.check_plan(api, plan)
        wordstat_checks = (
            await regional_wordstat(api, plan, wordstat_out_dir, wordstat_deadline_seconds)
            if wordstat_out_dir is not None and effective_links["all_ok"] else []
        )
        if effective_links["all_ok"] and not any(row.get("error") for row in wordstat_checks):
            if len(_PREFLIGHT_CACHE) >= 100:
                _PREFLIGHT_CACHE.pop(next(iter(_PREFLIGHT_CACHE)))
            _PREFLIGHT_CACHE[cache_key] = (time.monotonic() + PREFLIGHT_TTL,
                                          {"links": deepcopy(effective_links),
                                           "wordstat": deepcopy(wordstat_checks)})

    estimate = int(
        (plan.get("api_units_estimate") or {}).get("estimated_units", 0)
    )
    units_for = getattr(api, "units_for", None)
    last_units = units_for(client_login) if units_for else None
    available = getattr(last_units, "rest", None)
    units_check = {
        "estimated_write_units": estimate,
        "available_after_preflight": available,
        "enough_for_estimated_write": (
            available >= estimate if available is not None else None
        ),
    }
    if not api_units.rates_current():
        units_check["warning"] = "Тарифный снимок устарел; оценка баллов носит справочный характер."
    if available is not None and available < estimate and api_units.rates_current():
        raise ValueError(
            "Недостаточно API-баллов для безопасной write-фазы: "
            f"доступно {available}, прогноз {estimate}. Дождитесь пополнения, "
            "используйте баллы агентства либо выполните подтверждённое ручное "
            "копирование через Директ Коммандер с последующим API-аудитом."
        )

    return {
        "status": (
            policy.BLOCK if not effective_links["all_ok"] else
            policy.MANUAL if any(row["status"] == policy.MANUAL for row in wordstat_checks)
            else policy.WARNING if any(row["status"] == policy.WARNING for row in wordstat_checks)
            else policy.PASS
        ),
        "api_units": units_check,
        "regions_checked": region_ids,
        "regions_resolved": len(region_result.get("resolved", [])),
        "regions": region_result.get("resolved", []),
        "duplicate_campaigns": [],
        "sitelinks": effective_links["sitelinks"],
        "effective_link_checks": effective_links,
        "goals": goal_check,
        "currency": currency_check,
        "client_budget": currency_check["client_budget"],
        "assets": asset_check,
        "product_sources": product_check,
        "counters": counter_check,
        "audiences": interests_check,
        "wordstat": wordstat_checks,
        "wordstat_requested": wordstat_out_dir is not None,
        "expensive_checks_reused": reused,
        "cache_ttl_seconds": PREFLIGHT_TTL,
    }


async def regional_wordstat(
    api: Any, plan: dict[str, Any], out_dir: Path, deadline_seconds: float,
) -> list[dict[str, Any]]:
    """One result per group; reuse identical phrase/region lookups within this preflight."""
    checks = []
    cache: dict[tuple[tuple[int, ...], str], tuple[dict[str, Any], str]] = {}
    for campaign in plan["campaigns"]:
        if campaign["channel"] not in {"search", "maps"}:
            continue
        for group in campaign["groups"]:
            geo = tuple(sorted(set(group["ad_group"]["RegionIds"])))
            check = {"campaign": campaign["campaign"]["Name"],
                     "group": group["ad_group"]["Name"], "geo_ids": list(geo)}
            checks.append(check)
            if any(item < 0 for item in geo):
                check.update(status=policy.MANUAL, note=(
                    "География содержит исключения. Точный охват Wordstat требует отдельной "
                    "проверки; положительные регионы не подменяют географию группы."
                ))
                continue
            phrases = list(dict.fromkeys(row["Keyword"] for row in group["keywords"]
                                        if row["Keyword"] != semantics.AUTOTARGETING))
            missing = [phrase for phrase in phrases if (geo, phrase) not in cache]
            try:
                for offset in range(0, len(missing), wordstat.MAX_PHRASES):
                    batch = missing[offset:offset + wordstat.MAX_PHRASES]
                    result = await wordstat.lookup(
                        api, phrases=batch, geo_ids=list(geo) if geo != (0,) else None,
                        out_dir=out_dir, deadline_seconds=deadline_seconds, min_shows=0, top=1,
                    )
                    by_phrase = {row["phrase"]: row for row in result["phrases"]}
                    for phrase in batch:
                        cache[(geo, phrase)] = (by_phrase.get(phrase, {
                            "phrase": phrase, "shows": None, "data_status": "missing_response",
                        }), result["path"])
                rows = [cache[(geo, phrase)][0] for phrase in phrases]
                zero = [row["phrase"] for row in rows if row.get("shows") == 0]
                no_results = [row["phrase"] for row in rows
                              if row.get("data_status") == "no_results"]
                absent = [row["phrase"] for row in rows if row.get("shows") is None
                          and row.get("data_status") != "no_results"]
                check.update(
                    status=(policy.MANUAL if absent else policy.WARNING
                            if zero or no_results else policy.PASS),
                    phrases_count=len(rows), zero_count=len(zero), missing_count=len(absent),
                    no_results_count=len(no_results), no_results_preview=no_results[:30],
                    zero_preview=zero[:30], missing_preview=absent[:30],
                    data_status_counts={
                        status: sum(row.get("data_status", "unknown") == status for row in rows)
                        for status in sorted({row.get("data_status", "unknown") for row in rows})
                    },
                    artifact_paths=sorted({cache[(geo, phrase)][1] for phrase in phrases}),
                    note="Частотность не суммируется и не является прогнозом показов. "
                         "Ноль не означает автоматического исключения фразы.",
                )
            except Exception as exc:  # noqa: BLE001 - unavailable demand remains explicit
                check.update(status=policy.MANUAL, error=str(exc))
    return checks


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


def compare_readback(
    plan: dict[str, Any],
    execution: dict[str, Any],
    settings_payload: dict[str, Any],
    groups_payload: dict[str, Any],
    ads_payload: dict[str, Any],
    keyword_ids: set[int],
    ad_ids: set[int] | None = None,
    keyword_rows: list[dict[str, Any]] | None = None,
    modifiers_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Сопоставить фактическое API-состояние с конкретным скомпилированным планом."""
    findings: list[dict[str, Any]] = []
    incomplete = completeness.sources(
        campaigns=settings_payload, adgroups=groups_payload, ads=ads_payload,
        bid_modifiers=(modifiers_payload or {}).get("bid_modifiers", {}),
    )
    if incomplete:
        return {"verified": False, "source_truncated": True, "incomplete_sources": incomplete,
                "summary": {policy.BLOCK: 1}, "findings": [{
                    "rule": "readback.incomplete_data", "status": policy.BLOCK,
                    "message": "API вернул неполные данные; сравнение с планом недостоверно.",
                    "evidence": {"sources": incomplete},
                }]}
    actual_campaigns = {
        int(item["id"]): item
        for item in settings_payload.get("campaigns", [])
        if item.get("id") is not None
    }
    groups = groups_payload.get("groups", [])
    actual_ads = ads_payload.get("ads", [])
    actual_ads_by_id = {
        int(item["id"]): item for item in actual_ads if item.get("id") is not None
    }
    actual_keywords_by_id = {
        int(item["id"]): item
        for item in keyword_rows or []
        if item.get("id") is not None
    }
    expected_keyword_ids = {
        int(action["Id"])
        for item in execution.get("campaigns", [])
        for group in item.get("groups", [])
        for action in group.get("keywords", [])
        if isinstance(action, dict) and action.get("Id") is not None
    }
    expected_ad_ids = {
        int(action["Id"])
        for item in execution.get("campaigns", [])
        for group in item.get("groups", [])
        for action in group.get("ads", [])
        if isinstance(action, dict) and action.get("Id") is not None
    }
    if ad_ids is None:
        ad_ids = {
            int(item["id"])
            for item in actual_ads
            if item.get("id") is not None
        }

    def check(rule: str, matches: bool, message: str, **evidence: Any) -> None:
        finding: dict[str, Any] = {
            "rule": rule,
            "status": policy.PASS if matches else policy.BLOCK,
            "message": message,
        }
        if evidence:
            finding["evidence"] = evidence
        findings.append(finding)

    for executed in execution.get("campaigns", []):
        campaign_id = executed.get("id")
        if campaign_id is None:
            continue
        planned = plan["campaigns"][executed["plan_index"]]
        actual = actual_campaigns.get(int(campaign_id))
        check(
            "readback.campaign_exists",
            actual is not None,
            f"Кампания {campaign_id} найдена повторным чтением.",
            campaign_id=campaign_id,
        )
        if actual is None:
            continue
        expected_campaign = planned["campaign"]
        expected_unified = expected_campaign["UnifiedCampaign"]
        check(
            "readback.name",
            actual.get("name") == expected_campaign["Name"],
            f"Имя кампании {campaign_id} совпадает с планом.",
            expected=expected_campaign["Name"],
            actual=actual.get("name"),
        )
        check(
            "readback.channel",
            audit.classify_channel(actual)["channel"] == planned["channel"],
            f"Канал кампании {campaign_id} совпадает с планом.",
            expected=planned["channel"],
            actual=audit.classify_channel(actual)["channel"],
        )
        check(
            "readback.not_activated",
            actual.get("state") != "ON",
            f"Кампания {campaign_id} не запущена после создания.",
            actual_state=actual.get("state"),
        )
        expected_ages = {row["Age"] for item in planned.get("bid_modifiers", [])
                         for row in item.get("DemographicsAdjustments", [])}
        if modifiers_payload is not None:
            actual_modifiers = (
                modifiers_payload.get("bid_modifiers", {}).get("items", [])
            )
            actual_ages = {
                (row.get("details") or {}).get("Age") for row in actual_modifiers
                if row.get("campaign_id") == campaign_id
                and row.get("ad_group_id") is None
                and row.get("type") == "DEMOGRAPHICS_ADJUSTMENT"
                and row.get("bid_modifier") == 0
                and (row.get("details") or {}).get("Gender") in {None, "GENDER_ALL"}
                and row.get("enabled", "YES") == "YES"
            }
            demographic_rows = [row for row in actual_modifiers
                                if row.get("campaign_id") == campaign_id
                                and row.get("type") == "DEMOGRAPHICS_ADJUSTMENT"]
            exact_exclusions = len(demographic_rows) == len(expected_ages) and all(
                row.get("ad_group_id") is None and row.get("bid_modifier") == 0
                and row.get("enabled", "YES") == "YES"
                and (row.get("details") or {}).get("Gender") in {None, "GENDER_ALL"}
                for row in demographic_rows
            )
            check(
                "readback.age_min",
                actual_ages == expected_ages and exact_exclusions,
                f"Возрастное ограничение кампании {campaign_id} совпадает с планом.",
                expected_excluded_ages=sorted(expected_ages),
                actual_excluded_ages=sorted(actual_ages, key=str),
            )
        expected_tracking = expected_unified["TrackingParams"]
        check(
            "readback.tracking",
            tracking.parse_params(actual.get("tracking_params"))
            == tracking.parse_params(expected_tracking),
            f"TrackingParams кампании {campaign_id} совпадают с планом.",
        )
        check(
            "readback.attribution_model",
            actual.get("attribution_model") == expected_unified.get("AttributionModel"),
            f"Атрибуция кампании {campaign_id} совпадает с планом.",
        )
        check(
            "readback.settings",
            all((actual.get("settings") or {}).get(row["Option"]) == row["Value"]
                for row in expected_unified.get("Settings", [])),
            f"Настройки кампании {campaign_id} совпадают с планом.",
        )
        check(
            "readback.weekly_budget",
            sorted(_weekly_budgets(actual.get("bidding_strategy")))
            == sorted(_weekly_budgets(expected_unified["BiddingStrategy"])),
            f"Недельный бюджет кампании {campaign_id} совпадает с планом.",
        )
        for placement in ("Search", "Network"):
            expected_strategy = expected_unified["BiddingStrategy"].get(placement, {})
            actual_strategy = (actual.get("bidding_strategy") or {}).get(placement, {})
            check(
                "readback.strategy_type",
                actual_strategy.get("BiddingStrategyType")
                == expected_strategy.get("BiddingStrategyType"),
                f"Стратегия {placement} кампании {campaign_id} совпадает с планом.",
            )
            if planned["channel"] == "product":
                check("readback.product_placements", actual_strategy.get("PlacementTypes")
                      == expected_strategy.get("PlacementTypes"),
                      f"Места показа товарной кампании {campaign_id} совпадают с планом.")
            for key in ("WbMaximumClicks", "WbMaximumConversionRate"):
                if key in expected_strategy:
                    if "GoalId" in expected_strategy[key]:
                        check(
                            "readback.strategy_goal",
                            (actual_strategy.get(key) or {}).get("GoalId")
                            == expected_strategy[key]["GoalId"],
                            f"Цель {placement} кампании {campaign_id} совпадает с планом.",
                        )
                    check(
                        "readback.bid_ceiling",
                        (actual_strategy.get(key) or {}).get("BidCeiling")
                        == expected_strategy[key].get("BidCeiling"),
                        f"Ограничение ставки кампании {campaign_id} совпадает с планом.",
                    )
        check(
            "readback.time_zone",
            actual.get("time_zone") == expected_campaign.get("TimeZone", "Europe/Moscow"),
            f"Часовой пояс кампании {campaign_id} совпадает с планом.",
        )
        check(
            "readback.time_targeting",
            schedule.matches(
                actual.get("time_targeting"), expected_campaign.get("TimeTargeting")
            ),
            f"Расписание кампании {campaign_id} совпадает с планом.",
        )
        check(
            "readback.counters",
            set(actual.get("counter_ids") or [])
            == set(expected_unified.get("CounterIds", {}).get("Items", [])),
            f"Счётчики кампании {campaign_id} совпадают с планом.",
        )
        expected_goal_ids = {
            int(item["GoalId"])
            for item in expected_unified.get("PriorityGoals", {}).get("Items", [])
        }
        actual_goal_ids = {
            int(item["GoalId"])
            for item in actual.get("priority_goals") or []
            if item.get("GoalId") is not None
        }
        check(
            "readback.priority_goals",
            actual_goal_ids == expected_goal_ids and {
                row["GoalId"]: (row.get("Value"), row.get("IsMetrikaSourceOfValue", "NO"))
                for row in actual.get("priority_goals") or []
            } == {
                row["GoalId"]: (row.get("Value"), row.get("IsMetrikaSourceOfValue", "NO"))
                for row in expected_unified.get("PriorityGoals", {}).get("Items", [])
            },
            f"Приоритетные цели кампании {campaign_id} совпадают с планом.",
        )
        expected_negatives = {
            str(value).strip().lower()
            for value in expected_campaign.get("NegativeKeywords", {}).get(
                "Items", []
            )
        }
        actual_negatives = {
            str(value).strip().lower()
            for value in actual.get("negative_keywords") or []
        }
        check(
            "readback.negative_keywords",
            phrases.equivalent(list(actual_negatives), list(expected_negatives)),
            f"Минус-фразы кампании {campaign_id} совпадают с планом.",
        )
        check("readback.blocked_ips", set(actual.get("blocked_ips") or []) ==
              set(expected_campaign.get("BlockedIps", {}).get("Items", [])),
              f"Исключённые IP кампании {campaign_id} совпадают с планом.")
        # Direct lowercases ExcludedSites identifiers on readback.
        expected_sites = {
            str(value).strip().lower()
            for value in expected_campaign.get("ExcludedSites", {}).get(
                "Items", []
            )
        }
        actual_sites = {
            str(value).strip().lower()
            for value in actual.get("excluded_sites") or []
        }
        check(
            "readback.excluded_sites",
            actual_sites == expected_sites,
            f"Исключённые площадки кампании {campaign_id} совпадают с планом.",
        )
        expected_groups = len(planned["groups"])
        expected_ads = sum(len(item["ads"]) for item in planned["groups"])
        actual_groups = [
            row for row in groups if row.get("campaign_id") == campaign_id
        ]
        campaign_ads_count = int(
            ads_payload.get("counts_by_campaign", {}).get(
                str(campaign_id),
                sum(
                    1
                    for row in actual_ads
                    if row.get("campaign_id") == campaign_id
                ),
            )
        )
        check(
            "readback.group_count",
            len(actual_groups) == expected_groups,
            f"Число групп кампании {campaign_id} совпадает с планом.",
            expected=expected_groups,
            actual=len(actual_groups),
        )
        actual_groups_by_id = {
            int(row["id"]): row
            for row in actual_groups
            if row.get("id") is not None
        }
        for executed_group in executed.get("groups", []):
            group_id = executed_group.get("id")
            if group_id is None:
                continue
            expected_group = planned["groups"][executed_group["plan_index"]][
                "ad_group"
            ]
            actual_group = actual_groups_by_id.get(int(group_id))
            check(
                "readback.group_exists",
                actual_group is not None,
                f"Группа {group_id} найдена повторным чтением.",
            )
            if actual_group is None:
                continue
            check(
                "readback.group_name",
                actual_group.get("name") == expected_group["Name"],
                f"Имя группы {group_id} совпадает с планом.",
            )
            check(
                "readback.group_regions",
                set(actual_group.get("region_ids") or [])
                == set(expected_group["RegionIds"]),
                f"Регионы группы {group_id} совпадают с планом.",
            )
            check("readback.group_negatives", phrases.equivalent(
                actual_group.get("negative_keywords") or [],
                expected_group.get("NegativeKeywords", {}).get("Items", [])),
                f"Минус-фразы группы {group_id} совпадают с планом.")
            check("readback.group_shared_negatives",
                  set(actual_group.get("negative_keyword_shared_set_ids") or []) ==
                  set(expected_group.get("NegativeKeywordSharedSetIds", {}).get("Items", [])),
                  f"Общие наборы минус-фраз группы {group_id} совпадают с планом.")
            planned_group = planned["groups"][executed_group["plan_index"]]
            for expected_ad, action in zip(
                planned_group["ads"], executed_group.get("ads", []), strict=False
            ):
                ad_id = _action_id(action)
                if ad_id is None:
                    continue
                actual_ad = actual_ads_by_id.get(ad_id)
                expected_responsive = products.payload(expected_ad)
                is_product = products.kind(expected_ad) is not None
                check(
                    "readback.ad_content",
                    products.compare(expected_ad, actual_ad) if is_product else (
                        actual_ad is not None)
                    and actual_ad.get("type") == "RESPONSIVE_AD"
                    and actual_ad.get("titles") == expected_responsive["Titles"]
                    and actual_ad.get("texts") == expected_responsive["Texts"]
                    and actual_ad.get("href") == expected_responsive.get("Href")
                    and actual_ad.get("display_url_path")
                    == expected_responsive.get("DisplayUrlPath"),
                    f"Содержимое объявления {ad_id} совпадает с планом.",
                    ad_id=ad_id,
                )
                if is_product:
                    check("readback.product_processing", (actual_ad or {}).get(
                        "feed_processing_status") == "PROCESSED",
                        f"Товары объявления {ad_id} обработаны, фильтр не пуст.")
                for api_field, read_field in (("PriceExtension", "price_extension"),
                                               ("ErirAdDescription", "erir_ad_description")):
                    if api_field in expected_responsive:
                        check("readback.ad_" + read_field,
                              (actual_ad or {}).get(read_field) == expected_responsive[api_field],
                              f"{api_field} объявления {ad_id} совпадает с планом.")
                if expected_responsive.get("AdImageHashes"):
                    check(
                        "readback.ad_images",
                        actual_ad is not None
                        and set(actual_ad.get("ad_image_hashes") or [])
                        == set(expected_responsive["AdImageHashes"]),
                        f"Все изображения объявления {ad_id} совпадают с планом.",
                        ad_id=ad_id,
                        expected=expected_responsive["AdImageHashes"],
                        actual=(actual_ad or {}).get("ad_image_hashes"),
                    )
                expected_set = expected_responsive.get("SitelinkSetId")
                if "AgeLabel" in expected_responsive:
                    check(
                        "readback.ad_age_label",
                        actual_ad is not None
                        and actual_ad.get("age_label") == expected_responsive["AgeLabel"],
                        f"Возрастная маркировка объявления {ad_id} совпадает с планом.",
                    )
                check(
                    "readback.ad_sitelinks",
                    actual_ad is not None
                    and actual_ad.get("sitelink_set_id") == expected_set
                    and (expected_set is None or policy.SITELINKS_MINIMUM
                         <= (actual_ad.get("sitelink_count") or 0) <= policy.SITELINKS_MAXIMUM),
                    f"Набор и количество быстрых ссылок объявления {ad_id} проверены.",
                    ad_id=ad_id,
                )
            if keyword_rows is not None:
                for expected_keyword, action in zip(
                    planned_group["keywords"],
                    executed_group.get("keywords", []),
                    strict=False,
                ):
                    keyword_id = _action_id(action)
                    if keyword_id is None:
                        continue
                    actual_keyword = actual_keywords_by_id.get(keyword_id)
                    expected_settings = expected_keyword.get(
                        "AutotargetingSettings"
                    )
                    matches = actual_keyword is not None and (
                        actual_keyword.get("keyword")
                        == expected_keyword.get("Keyword")
                    )
                    if expected_settings is not None:
                        matches = matches and (
                            actual_keyword.get("autotargeting") == {
                                "categories": expected_settings["Categories"],
                                "brand_options": expected_settings["BrandOptions"],
                            }
                        )
                    check(
                        "readback.keyword_content",
                        matches,
                        f"Ключ/автотаргетинг {keyword_id} совпадает с планом.",
                        keyword_id=keyword_id,
                    )
        check(
            "readback.ad_count",
            campaign_ads_count == expected_ads,
            f"Число объявлений кампании {campaign_id} совпадает с планом.",
            expected=expected_ads,
            actual=campaign_ads_count,
        )

    check(
        "readback.ad_ids",
        ad_ids == expected_ad_ids,
        "Все созданные объявления найдены повторным чтением.",
        expected=sorted(expected_ad_ids),
        actual=sorted(ad_ids),
    )
    check(
        "readback.keyword_ids",
        keyword_ids == expected_keyword_ids,
        "Все созданные ключевые фразы найдены повторным чтением.",
        expected=sorted(expected_keyword_ids),
        actual=sorted(keyword_ids),
    )
    counts = {
        status: sum(item["status"] == status for item in findings)
        for status in (policy.PASS, policy.BLOCK)
    }
    return {
        "verified": counts[policy.BLOCK] == 0,
        "summary": counts,
        "findings": findings,
    }


async def _read_created_ids(
    api: Any,
    service: str,
    collection: str,
    ids: list[int],
    client_login: str,
) -> set[int]:
    found: set[int] = set()
    for offset in range(0, len(ids), 10000):
        chunk = ids[offset : offset + 10000]
        response = await api.call_v501(
            service,
            "get",
            {
                "SelectionCriteria": {"Ids": chunk},
                "FieldNames": ["Id"],
                "Page": {"Limit": 10000},
            },
            client_login=client_login,
        )
        found.update(
            int(item["Id"])
            for item in response.get(collection, [])
            if item.get("Id") is not None
        )
        if response.get("LimitedBy") is not None:
            raise RuntimeError(
                f"{service}.get усёк readback созданных объектов"
            )
    return found


async def _read_created_keywords(
    api: Any,
    ids: list[int],
    client_login: str,
) -> list[dict[str, Any]]:
    """Read all created keywords and current autotargeting settings."""
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(ids), 10000):
        chunk = ids[offset : offset + 10000]
        response = await api.call_v501(
            "keywords",
            "get",
            {
                "SelectionCriteria": {"Ids": chunk},
                "FieldNames": [
                    "Id",
                    "Keyword",
                    "AdGroupId",
                    "CampaignId",
                    "State",
                    "Status",
                    "ServingStatus",
                    "AutotargetingSearchBidIsAuto",
                    "StrategyPriority",
                    "UserParam1",
                    "UserParam2",
                ],
                "AutotargetingSettingsCategoriesFieldNames": [
                    "Exact",
                    "Narrow",
                    "Alternative",
                    "Accessory",
                    "Broader",
                ],
                "AutotargetingSettingsBrandOptionsFieldNames": [
                    "WithoutBrands",
                    "WithAdvertiserBrand",
                    "WithCompetitorsBrand",
                ],
                "Page": {"Limit": 10000},
            },
            client_login=client_login,
        )
        rows.extend(response.get("Keywords", []))
        if response.get("LimitedBy") is not None:
            raise RuntimeError("keywords.get усёк readback созданных объектов")
    return rows


async def readback(
    api: Any,
    plan: dict[str, Any],
    execution: dict[str, Any],
) -> dict[str, Any]:
    """Независимо перечитать созданные объекты и применить аудит политики."""
    campaign_ids = [
        int(item["id"])
        for item in execution.get("campaigns", [])
        if item.get("id") is not None
    ]
    if not campaign_ids:
        raise ValueError("В результате нет созданных campaign ID для readback")
    settings_payload = await campaigns.read_settings(
        api, plan["client_login"], campaign_ids=campaign_ids, limit=len(campaign_ids)
    )
    groups_payload = await adgroups.read(
        api, plan["client_login"], campaign_ids=campaign_ids, limit=10000
    )
    ads_payload = await ads.read(
        api,
        plan["client_login"],
        campaign_ids=campaign_ids,
        include_archived=True,
        limit=10000,
        preview_limit=None,
    )
    sitelink_sets = await assets.enrich_ads(api, plan["client_login"], ads_payload.get("ads", []))
    created_keyword_ids = [
        int(action["Id"])
        for item in execution.get("campaigns", [])
        for group in item.get("groups", [])
        for action in group.get("keywords", [])
        if isinstance(action, dict) and action.get("Id") is not None
    ]
    created_ad_ids = [
        int(action["Id"])
        for item in execution.get("campaigns", [])
        for group in item.get("groups", [])
        for action in group.get("ads", [])
        if isinstance(action, dict) and action.get("Id") is not None
    ]
    ad_ids = await _read_created_ids(
        api,
        "ads",
        "Ads",
        created_ad_ids,
        plan["client_login"],
    )
    keyword_rows: list[dict[str, Any]] = []
    keyword_ids: set[int] = set()
    incomplete = completeness.sources(
        campaigns=settings_payload, adgroups=groups_payload, ads=ads_payload,
    )
    for offset in range(0, len(created_keyword_ids), 10000):
        keyword_payload = await keywords.read(
            api,
            plan["client_login"],
            keyword_ids=created_keyword_ids[offset : offset + 10000],
            limit=10000,
        )
        incomplete.extend(completeness.sources(keywords=keyword_payload))
        keyword_rows.extend(keyword_payload["keywords"])
        keyword_ids.update(
            int(item["id"])
            for item in keyword_payload["keywords"]
            if item.get("id") is not None
        )
    if incomplete:
        return {
            "verified": False, "source_truncated": True,
            "incomplete_sources": incomplete,
            "comparison": {"verified": False, "findings": [{
                "rule": "readback.incomplete_data", "status": policy.BLOCK,
                "message": "API вернул неполные данные; совпадение с планом не проверено.",
                "evidence": {"sources": incomplete},
            }]},
        }
    for created in execution.get("campaigns", []):
        planned = plan["campaigns"][created["plan_index"]]
        if planned["channel"] == "product":
            for row in ads_payload.get("ads", []):
                if row.get("campaign_id") == created.get("id"):
                    row["product_urls"] = planned["product_source"]["sample_urls"]
    checked_pages = await link_checks.check_live(
        api, plan["client_login"], settings_payload.get("campaigns", []),
        groups_payload.get("groups", []), ads_payload.get("ads", []), sitelink_sets=sitelink_sets,
    )
    selected_policy = policy.for_plan(plan)
    policy_audit = audit.audit_payload(
        settings_payload,
        policy_name=selected_policy["name"],
        landing_pages=checked_pages,
        groups=groups_payload.get("groups", []),
        ad_rows=ads_payload.get("ads", []),
        keyword_rows=keyword_rows,
    )
    comparison = compare_readback(
        plan,
        execution,
        settings_payload,
        groups_payload,
        ads_payload,
        keyword_ids,
        ad_ids,
        keyword_rows,
        await account.read(
            api,
            plan["client_login"],
            sections=["bid_modifiers"],
            campaign_ids=campaign_ids,
        ),
    )
    audience_readback = await audience_setup.readback(api, plan, execution)
    launch_readback = await readback_launch_checks(api, plan, settings_payload.get("campaigns", []))
    return {
        "verified": (comparison["verified"] and policy_audit["ready"]
                     and audience_readback["verified"] and launch_readback["verified"]),
        "launch_checks": launch_readback,
        "audiences": audience_readback,
        "source_truncated": bool(comparison.get("source_truncated")),
        "incomplete_sources": comparison.get("incomplete_sources", []),
        "comparison": comparison,
        "policy_audit": policy_audit,
        "counts": {
            "campaigns": settings_payload.get("count", 0),
            "ad_groups": groups_payload.get("count", 0),
            "ads": ads_payload.get("count", 0),
            "keywords": sum(row.get("keyword") != semantics.AUTOTARGETING
                            for row in keyword_rows),
            "autotargeting": sum(row.get("keyword") == semantics.AUTOTARGETING
                                 for row in keyword_rows),
            "criteria": len(keyword_ids) + len(audience_readback["rows"]),
            "bid_modifiers": sum(
                len(action.get("Ids", [action["Id"]] if action.get("Id") else []))
                for item in execution.get("campaigns", [])
                for action in item.get("bid_modifiers", [])
            ),
        },
        "objects": {
            "campaigns": settings_payload.get("campaigns", []),
            "ad_groups": groups_payload.get("groups", []),
            "ads": ads_payload.get("ads", []),
            "keywords": keyword_rows,
        },
    }


def validate_ad_counts(plan: dict[str, Any]) -> None:
    for campaign in plan.get("campaigns", []):
        for group in campaign.get("groups", []):
            if campaign["channel"] == "product":
                continue  # products.validate_campaigns checks per-format limits.
            if not 1 <= len(group.get("ads", [])) <= policy.RESPONSIVE_ADS_MAX_NON_ARCHIVED:
                raise ValueError("Новая группа должна содержать от 1 до 3 комбинаторных объявлений")


async def readback_launch_checks(api, plan: dict, actual_campaigns: list[dict]) -> dict:
    """Independently re-read contacts and currency; check the actual created budgets."""
    result = {"verified": False}
    try:
        if any(c["channel"] == "product" for c in plan["campaigns"]):
            result["product_sources"] = await products.check_sources(api, plan)
        if plan.get("client_budget") is not None:
            currency = await preflight_refs.currency(api, plan)
            result["client_budget"] = launch_checks.check_budget(
                plan["client_budget"], actual_campaigns, currency["currency"])
        if plan.get("business_profiles"):
            rows = await preflight_refs._references(
                api, plan["client_login"], "businesses", "Businesses",
                {row["business_id"] for row in plan["business_profiles"]},
                fields=["Id", "IsPublished", "Phone", "Address", "HasOffice"],
                batch_size=1000, page_limit=1000,
            )
            result["business_contacts"] = launch_checks.check_businesses(
                plan["business_profiles"], rows)
    except (ValueError, KeyError, TypeError) as exc:
        result["error"] = str(exc)
        return result
    result["verified"] = True
    return result


def validate_plan(plan: dict[str, Any]) -> None:
    selected_policy = policy.for_plan(plan)
    products.validate_campaigns(plan["campaigns"])
    if any(c["channel"] == "product" for c in plan["campaigns"]) and not selected_policy.get(
            "profile", {}).get("product_formats"):
        raise ValueError("Товарный канал требует товарного профиля v2")
    if "profile" in selected_policy:
        unified = plan["campaigns"][0]["campaign"]["UnifiedCampaign"]
        context = profiles.context(
            plan.get("profile_context"), selected_policy, login=plan["client_login"],
            goals={r["GoalId"] for r in unified.get("PriorityGoals", {}).get("Items", [])},
            counters=set(unified.get("CounterIds", {}).get("Items", [])),
            today=campaign_setup.moscow_today())
        profiles.validate_campaigns(
            plan["campaigns"], context, selected_policy, client_budget=plan.get("client_budget"),
            office=plan.get("profile_office") is True, today=campaign_setup.moscow_today(),
            allow_unverified_goals=plan.get("allow_unverified_goals", False),
        )
    launch_checks.check_budget(plan.get("client_budget"), plan["campaigns"])
    if "business_profiles" in plan:
        launch_checks.normalize_businesses(
            plan["business_profiles"],
            {products.payload(ad)["BusinessId"] for c in plan["campaigns"]
             for g in c["groups"] for ad in g["ads"] if products.payload(ad).get("BusinessId")},
        )
    for item in plan["campaigns"]:
        unified = item["campaign"]["UnifiedCampaign"]
        priority_ids = {int(row["GoalId"])
                        for row in unified.get("PriorityGoals", {}).get("Items", [])}
        for branch in unified["BiddingStrategy"].values():
            if branch.get("BiddingStrategyType") == "WB_MAXIMUM_CONVERSION_RATE":
                launch_checks.validate_conversion_goal(
                    branch.get("WbMaximumConversionRate", {}).get("GoalId", 0), priority_ids)
    validate_ad_counts(plan)
    for item in plan["campaigns"]:
        for group in item["groups"]:
            buttons = group.get("action_buttons", [])
            if len(buttons) != len(group["ads"]):
                raise ValueError("План кнопок устарел: повторите direct_campaign_plan")
            for ad, button in zip(group["ads"], buttons, strict=True):
                payload = products.payload(ad)
                if (
                    creative.needs_button(payload)
                    and creative.action_button(button, payload["Href"]) is None
                ):
                    raise ValueError("Не выбрана action_button для объявления")
                if item["channel"] == "network" and not creative.image_count_ok(
                    payload.get("AdImageHashes")
                ):
                    raise ValueError("РСЯ: требуется от 3 до 5 разных изображений")
    semantics.validate_plan_limits(plan["campaigns"])
    _, findings = semantics.review(
        plan.get("semantic_plan"), plan["campaigns"], today=campaign_setup.moscow_today(),
        selected_policy=selected_policy,
    )
    acknowledged = set(plan.get("manual_checks", {}).get("acknowledged_warning_rules", []))
    blocked = [row for row in findings if row["status"] == policy.BLOCK
               or row["status"] == policy.WARNING and row["rule"] not in acknowledged]
    if blocked:
        raise ValueError("План структуры и семантики не готов: "
                         + "; ".join(row["message"] for row in blocked[:5]))
    if not plan.get("ready"):
        raise ValueError("План содержит BLOCK-проверки и не может быть применён")


async def apply(api: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Создать кампании и дочерние объекты, никогда не активируя показы."""
    validate_plan(plan)
    mark = api.units_mark() if callable(getattr(api, "units_mark", None)) else None
    client_login = plan["client_login"]
    requested_campaigns = plan["campaigns"]
    fatal_error = None
    try:
        campaign_actions = await _add_v501(
            api,
            "campaigns",
            "Campaigns",
            [item["campaign"] for item in requested_campaigns],
            client_login,
        )
    except Exception as exc:  # noqa: BLE001
        campaign_actions = getattr(exc, "results", [])
        fatal_error = _fatal("campaigns.add", exc)

    campaign_results: list[dict[str, Any]] = []
    flat_groups: list[tuple[int, int, dict[str, Any]]] = []
    for campaign_index, (planned, action) in enumerate(
        zip(requested_campaigns, campaign_actions, strict=False)
    ):
        campaign_id = _action_id(action)
        result = {
            "plan_index": campaign_index,
            "channel": planned["channel"],
            "name": planned["campaign"]["Name"],
            "id": campaign_id,
            "warnings": action.get("Warnings", []),
            "errors": action.get("Errors", []),
            "groups": [],
            "bid_modifiers": [],
        }
        campaign_results.append(result)
        if campaign_id is None:
            continue
        for group_index, planned_group in enumerate(planned["groups"]):
            payload = dict(planned_group["ad_group"])
            payload["CampaignId"] = campaign_id
            flat_groups.append((campaign_index, group_index, payload))

    flat_modifiers: list[tuple[int, dict[str, Any]]] = []
    for campaign_index, item in enumerate(campaign_results):
        campaign_id = item.get("id")
        if campaign_id is None:
            continue
        for payload in requested_campaigns[campaign_index].get("bid_modifiers", []):
            flat_modifiers.append(
                (campaign_index, {**payload, "CampaignId": campaign_id})
            )
    modifier_actions: list[dict[str, Any]] = []
    if flat_modifiers and fatal_error is None:
        try:
            modifier_actions = await _add_v501(
                api,
                "bidmodifiers",
                "BidModifiers",
                [payload for _, payload in flat_modifiers],
                client_login,
            )
        except Exception as exc:  # noqa: BLE001
            modifier_actions = getattr(exc, "results", [])
            fatal_error = _fatal("bidmodifiers.add", exc)
    for (campaign_index, _), action in zip(
        flat_modifiers, modifier_actions, strict=False
    ):
        campaign_results[campaign_index]["bid_modifiers"].append(action)

    group_actions: list[dict[str, Any]] = []
    if flat_groups and fatal_error is None:
        try:
            group_actions = await _add_v501(
                api,
                "adgroups",
                "AdGroups",
                [payload for _, _, payload in flat_groups],
                client_login,
            )
        except Exception as exc:  # noqa: BLE001
            group_actions = getattr(exc, "results", [])
            fatal_error = _fatal("adgroups.add", exc)

    flat_ads: list[tuple[int, int, dict[str, Any]]] = []
    flat_keywords: list[tuple[int, int, dict[str, Any]]] = []
    for (campaign_index, group_index, _), action in zip(
        flat_groups, group_actions, strict=False
    ):
        group_id = _action_id(action)
        planned_group = requested_campaigns[campaign_index]["groups"][group_index]
        group_result = {
            "plan_index": group_index,
            "name": planned_group["ad_group"]["Name"],
            "id": group_id,
            "warnings": action.get("Warnings", []),
            "errors": action.get("Errors", []),
            "ads": [],
            "keywords": [],
        }
        campaign_results[campaign_index]["groups"].append(group_result)
        if group_id is None:
            continue
        flat_ads.extend(
            (campaign_index, group_index, {**payload, "AdGroupId": group_id})
            for payload in planned_group["ads"]
        )
        flat_keywords.extend(
            (campaign_index, group_index, {**payload, "AdGroupId": group_id})
            for payload in planned_group["keywords"]
        )

    ad_actions: list[dict[str, Any]] = []
    if flat_ads and fatal_error is None:
        try:
            ad_actions = await _add_v501(
                api,
                "ads",
                "Ads",
                [payload for _, _, payload in flat_ads],
                client_login,
            )
        except Exception as exc:  # noqa: BLE001
            ad_actions = getattr(exc, "results", [])
            fatal_error = _fatal("ads.add", exc)
    for (campaign_index, group_index, _), action in zip(
        flat_ads, ad_actions, strict=False
    ):
        campaign_results[campaign_index]["groups"][group_index]["ads"].append(action)

    keyword_actions: list[dict[str, Any]] = []
    if flat_keywords and fatal_error is None:
        try:
            keyword_actions = await _add_v501(
                api,
                "keywords",
                "Keywords",
                [payload for _, _, payload in flat_keywords],
                client_login,
            )
        except Exception as exc:  # noqa: BLE001
            keyword_actions = getattr(exc, "results", [])
            fatal_error = _fatal("keywords.add", exc)
    for (campaign_index, group_index, _), action in zip(
        flat_keywords, keyword_actions, strict=False
    ):
        campaign_results[campaign_index]["groups"][group_index]["keywords"].append(
            action
        )

    audience_result = None
    if audience_setup.planned(plan):
        if fatal_error is None:
            audience_result = await audience_setup.apply(api, plan, campaign_results, _add_v501)
        else:
            audience_result = {
                "complete": False, "rows": [], "error": "Предыдущий этап не завершён",
            }
    created_audience_targets = sum(
        _action_id(row["audience_target"]) is not None
        for row in (audience_result or {}).get("rows", [])
    )
    created_lists = sum(
        _action_id(row["retargeting_list"]) is not None
        for row in (audience_result or {}).get("rows", [])
    )
    created_campaigns = sum(item["id"] is not None for item in campaign_results)
    created_groups = sum(
        group["id"] is not None
        for item in campaign_results
        for group in item["groups"]
    )
    created_ads = sum(_action_id(action) is not None for action in ad_actions)
    created_criteria = sum(_action_id(action) is not None for action in keyword_actions)
    created_autotargeting = sum(
        _action_id(action) is not None and payload["Keyword"] == semantics.AUTOTARGETING
        for (_, _, payload), action in zip(flat_keywords, keyword_actions, strict=False)
    )
    created_keywords = created_criteria - created_autotargeting
    created_modifiers = sum(
        len(action.get("Ids", [action["Id"]] if action.get("Id") else []))
        for action in modifier_actions if not action.get("Errors")
    )
    created_criteria += created_audience_targets
    expected = plan["summary"]
    created_counts = {
        "campaigns": created_campaigns,
        "ad_groups": created_groups,
        "ads": created_ads,
        "keywords": created_keywords,
        "autotargeting": created_autotargeting,
        "criteria": created_criteria,
        "bid_modifiers": created_modifiers,
    }
    if audience_result is not None:
        created_counts.update(
            audience_targets=created_audience_targets, retargeting_lists=created_lists,
        )
    complete = (fatal_error is None
                and (audience_result is None or audience_result["complete"])
                and all(expected[name] == count for name, count in created_counts.items()))
    result: dict[str, Any] = {
        "status": "complete" if complete else "partial",
        "executed": True,
        "client_login": client_login,
        "plan_hash": plan["plan_hash"],
        "api_units_estimate": plan.get("api_units_estimate"),
        "campaigns": campaign_results,
        "summary": {
            name: {"requested": expected[name], "created": created}
            for name, created in {
                "campaigns": created_campaigns,
                "ad_groups": created_groups,
                "ads": created_ads,
                "keywords": created_keywords,
                "autotargeting": created_autotargeting,
                "criteria": created_criteria,
                "bid_modifiers": created_modifiers,
            }.items()
        },
        "activated": False,
        "message": (
            "Объекты созданы, показы не запущены."
            if complete
            else "Создание завершено частично; повторять весь план нельзя."
        ),
    }
    if audience_result is not None:
        result["audiences"] = audience_result
        result["summary"].update({
            name: {"requested": expected[name], "created": created_counts[name]}
            for name in ("audience_targets", "retargeting_lists")
        })
    from collections import Counter
    warning_counts = Counter()
    warning_messages = {}
    audience_rows = (audience_result or {}).get("rows", [])
    list_actions = [row["retargeting_list"] for row in audience_rows]
    target_actions = [row["audience_target"] for row in audience_rows]
    for service, actions in (
        ("campaigns", campaign_actions),
        ("adgroups", group_actions),
        ("ads", ad_actions),
        ("keywords", keyword_actions),
        ("bidmodifiers", modifier_actions),
        ("retargetinglists", list_actions),
        ("audiencetargets", target_actions),
    ):
        for action in actions:
            for warning in action.get("Warnings", []):
                key = (service, str(warning.get("Code", "unknown")))
                warning_counts[key] += 1
                warning_messages[key] = warning.get("Message") or warning.get("Details")
    result["api_warnings"] = [{"service": key[0], "code": key[1], "count": count,
                               "message": warning_messages[key]}
                              for key, count in warning_counts.items()]
    result["required_manual_actions"] = creative.executed_actions(plan, campaign_results)
    if created_campaigns:
        result["required_manual_actions"].append({
            "rule": "launch.moderation", "required": True, "status": "pending_explicit_launch",
            "message": "Перед модерацией проверьте паузу кампаний. Принятая модерация и StartDate "
                       "могут запустить показы; запуск требует отдельной явной команды.",
        })
    # status=complete describes API creation, not completion of UI-only additions.
    result["api_creation_complete"] = complete
    workflow.update(result)
    if result["workflow"]["ui_verification"] == "pending":
        result["message"] += (
            " Настройка не завершена: выполните required_manual_actions "
            "и проверьте сохранённый результат в интерфейсе."
        )
    if fatal_error:
        result["fatal_error"] = fatal_error
    if mark is not None:
        usage = api.units_since(mark)
        result["write_units_usage"] = usage
        comparable = complete and not usage.get("truncated") and bool(usage.get("requests"))
        estimate = (plan.get("api_units_estimate") or {}).get("estimated_units")
        result["api_units_comparison"] = {
            "comparable": comparable, "estimated": estimate, "actual": usage.get("spent"),
            "difference": (
                usage["spent"] - estimate if comparable and estimate is not None else None
            ),
        }
    return result


def persist(result: dict[str, Any], out_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    login = store.safe_stem(str(result.get("client_login") or "client"))
    path = out_dir / f"apply_{login}_{stamp}_{result['plan_hash'][:12]}.json"
    path.write_text(
        json.dumps(wire(result), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path
