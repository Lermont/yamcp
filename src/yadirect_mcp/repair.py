"""Guarded updates for existing UnifiedCampaign objects.

The repair plan is intentionally narrow: campaign negatives/priority goals,
ResponsiveAd assets and autotargeting settings. It never resumes campaigns.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    adgroups,
    ads,
    assets,
    bundle,
    campaign_setup,
    campaigns,
    completeness,
    creative,
    goals,
    keywords,
    landing,
    link_checks,
    phrases,
    policy,
    preflight_refs,
    store,
)
from .identifiers import parse_id, wire

TOP_LEVEL_FIELDS = {"campaigns", "ads", "autotargetings"}
CAMPAIGN_FIELDS = {"id", "negative_keywords", "priority_goals"}
AD_FIELDS = {
    "id",
    "titles",
    "texts",
    "href",
    "display_url_path",
    "sitelink_set_id",
    "ad_extension_ids",
    "ad_image_hashes",
}
AUTOTARGETING_FIELDS = {"id", "categories", "brand_options"}


def _unknown(value: dict[str, Any], allowed: set[str], field: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise ValueError(f"{field} содержит неизвестные поля: {', '.join(extra)}")


def _positive_id(value: Any, field: str) -> int:
    number = parse_id(value, field)
    if number <= 0:
        raise ValueError(f"{field} должен быть положительным ID")
    return number


def _goal_values(rows: list[dict[str, Any]]) -> list[tuple]:
    return sorted((row.get("GoalId"), row.get("Value"),
                   row.get("IsMetrikaSourceOfValue", "NO")) for row in rows)


def normalize(raw: dict[str, Any], client_login: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("repair_bundle должен быть объектом")
    source = deepcopy(raw)
    _unknown(source, TOP_LEVEL_FIELDS, "repair_bundle")

    campaign_updates = []
    for index, item in enumerate(source.get("campaigns") or []):
        prefix = f"repair_bundle.campaigns[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} должен быть объектом")
        _unknown(item, CAMPAIGN_FIELDS, prefix)
        update: dict[str, Any] = {"Id": _positive_id(item.get("id"), f"{prefix}.id")}
        if "negative_keywords" in item:
            if not isinstance(item["negative_keywords"], list):
                raise ValueError(f"{prefix}.negative_keywords должен быть массивом")
            update["NegativeKeywords"] = {
                "Items": bundle._negative_phrases(
                    item["negative_keywords"],
                    f"{prefix}.negative_keywords",
                    maximum_total_length=20_000,
                )
            }
        if "priority_goals" in item:
            raw_goals = item["priority_goals"]
            if not isinstance(raw_goals, list):
                raise ValueError(f"{prefix}.priority_goals должен быть массивом")
            goals = []
            for goal_index, goal in enumerate(raw_goals):
                if not isinstance(goal, dict) or set(goal) != {"goal_id", "value"}:
                    raise ValueError(
                        f"{prefix}.priority_goals[{goal_index}] требует goal_id и value"
                    )
                goals.append(
                    {
                        "GoalId": _positive_id(
                            goal["goal_id"],
                            f"{prefix}.priority_goals[{goal_index}].goal_id",
                        ),
                        "Value": bundle._micros(
                            goal["value"],
                            f"{prefix}.priority_goals[{goal_index}].value",
                        ),
                    }
                )
            update["UnifiedCampaign"] = {
                # Documented reset: null deletes PriorityGoals; [] is not the contract.
                # https://yandex.com/dev/direct/doc/en/campaigns/update-unified-campaign
                "PriorityGoals": {"Items": goals} if goals else None
            }
        if len(update) == 1:
            raise ValueError(f"{prefix} не содержит изменений")
        campaign_updates.append(update)

    ad_updates = []
    for index, item in enumerate(source.get("ads") or []):
        prefix = f"repair_bundle.ads[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} должен быть объектом")
        _unknown(item, AD_FIELDS, prefix)
        responsive = {}
        mapping = {
            "titles": "Titles", "texts": "Texts", "href": "Href",
            "display_url_path": "DisplayUrlPath", "sitelink_set_id": "SitelinkSetId",
        }
        for source_name, api_name in mapping.items():
            if source_name in item:
                responsive[api_name] = item[source_name]
        if responsive.get("SitelinkSetId") is not None:
            responsive["SitelinkSetId"] = _positive_id(
                responsive["SitelinkSetId"], f"{prefix}.sitelink_set_id",
            )
        extension_ids = None
        if "ad_extension_ids" in item:
            raw_ids = item["ad_extension_ids"]
            if not isinstance(raw_ids, list):
                raise ValueError(f"{prefix}.ad_extension_ids должен быть массивом; [] очищает")
            extension_ids = [_positive_id(v, f"{prefix}.ad_extension_ids") for v in raw_ids]
            if len(set(extension_ids)) != len(extension_ids):
                raise ValueError(f"{prefix}.ad_extension_ids содержит дубли")
        campaign_setup._validate_responsive_ad(responsive, prefix, partial=True)
        if "ad_image_hashes" in item:
            raw_hashes = item["ad_image_hashes"]
            hashes = creative.image_hashes(raw_hashes)
            if (not isinstance(raw_hashes, list) or len(hashes) != len(raw_hashes)
                    or not 1 <= len(hashes) <= policy.RESPONSIVE_IMAGES_MAXIMUM):
                raise ValueError(f"{prefix}.ad_image_hashes: 1-5 distinct nonempty hashes required")
            # ads.update uses ArrayOfString; ads.add uses a plain array.
            responsive["AdImageHashes"] = {"Items": hashes}
        if extension_ids is not None:
            responsive["CalloutSetting"] = (
                {
                    "AdExtensions": [
                        {"AdExtensionId": int(identifier), "Operation": "SET"}
                        for identifier in extension_ids
                    ]
                }
                if extension_ids
                else None
            )
        if not responsive:
            raise ValueError(f"{prefix} не содержит изменений")
        ad_updates.append(
            {
                "Id": _positive_id(item.get("id"), f"{prefix}.id"),
                "ResponsiveAd": responsive,
            }
        )

    autotargeting_updates = []
    for index, item in enumerate(source.get("autotargetings") or []):
        prefix = f"repair_bundle.autotargetings[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} должен быть объектом")
        _unknown(item, AUTOTARGETING_FIELDS, prefix)
        compiled, _ = bundle._autotargeting(
            {
                "categories": item.get("categories"),
                "brand_options": item.get("brand_options"),
            },
            prefix,
        )
        autotargeting_updates.append(
            {
                "Id": _positive_id(item.get("id"), f"{prefix}.id"),
                "AutotargetingSettings": compiled["AutotargetingSettings"],
            }
        )

    for name, rows in (("campaigns", campaign_updates), ("ads", ad_updates),
                       ("autotargetings", autotargeting_updates)):
        if len({row["Id"] for row in rows}) != len(rows):
            raise ValueError(f"repair_bundle.{name} содержит дубли ID")
    if not campaign_updates and not ad_updates and not autotargeting_updates:
        raise ValueError("repair_bundle не содержит изменений")
    plan: dict[str, Any] = {
        "schema": "direct_campaign_repair_v1",
        "client_login": client_login,
        "api_version": "v501",
        "campaigns": campaign_updates,
        "ads": ad_updates,
        "autotargetings": autotargeting_updates,
        "activated": False,
    }
    canonical = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    plan["plan_hash"] = hashlib.sha256(canonical).hexdigest()
    plan["summary"] = {
        "campaigns": len(campaign_updates),
        "ads": len(ad_updates),
        "autotargetings": len(autotargeting_updates),
    }
    return plan


AD_FIELDS_MAP = {
    "Titles": "titles", "Texts": "texts", "Href": "href",
    "DisplayUrlPath": "display_url_path", "SitelinkSetId": "sitelink_set_id",
    "CalloutSetting": "ad_extension_ids", "AdImageHashes": "ad_image_hashes",
}
PRESERVED_AD_FIELDS = {
    "campaign_id", "ad_group_id", "type", "subtype", "state", "age_label",
    "business_id", "price_extension", "erir_ad_description", "video_extension_ids",
}


def _patched_ad(previous: dict, patch: dict) -> dict:
    result = deepcopy(previous)
    for api_field, field in AD_FIELDS_MAP.items():
        if api_field not in patch:
            continue
        value = patch[api_field]
        if api_field == "CalloutSetting":
            value = [row["AdExtensionId"] for row in (value or {}).get("AdExtensions", [])]
        elif api_field == "AdImageHashes":
            value = (value or {}).get("Items", [])
        result[field] = deepcopy(value)
    return result


def _ad_value(row: dict, field: str):
    value = row.get(field)
    if field in {"ad_extension_ids", "video_extension_ids"}:
        # A missing field is unknown, not proof of an empty set.
        return sorted(parse_id(v, field) for v in value) if value is not None else None
    if field == "ad_image_hashes":
        return sorted(value) if value is not None else None
    if field.endswith("_id") and value is not None:
        return parse_id(value, field)
    return value


def _exact(payload: dict, key: str, wanted: list[int]) -> list[dict]:
    rows = payload.get(key, [])
    ids = [parse_id(row["id"]) for row in rows]
    if (completeness.sources(**{key: payload}) or len(ids) != len(set(ids))
            or set(ids) != set(wanted)):
        raise ValueError(f"Preflight {key}: неполные данные, дубли или отсутствующие объекты")
    return sorted(rows, key=lambda row: int(row["id"]))


async def preflight(api: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Read ownership, final assets and inherited URLs before issuing any grant."""
    login = plan["client_login"]
    ad_ids = [row["Id"] for row in plan["ads"]]
    keyword_ids = [row["Id"] for row in plan["autotargetings"]]
    ad_rows = _exact(await ads.read(
        api, login, ad_ids=ad_ids, limit=10000, preview_limit=None,
    ), "ads", ad_ids) if ad_ids else []
    keyword_rows = _exact(await keywords.read(
        api, login, keyword_ids=keyword_ids,
    ), "keywords", keyword_ids) if keyword_ids else []
    if any("RESPONSIVE_AD" not in {a.get("type"), a.get("subtype")}
           or a.get("state") not in {"ON", "OFF", "SUSPENDED"} for a in ad_rows):
        raise ValueError("Preflight: требуется доступное неархивное ResponsiveAd")
    if any(k.get("keyword") != "---autotargeting" or k.get("state") == "ARCHIVED"
           for k in keyword_rows):
        raise ValueError("Preflight: требуется существующий автотаргетинг")
    campaign_ids = sorted({row["Id"] for row in plan["campaigns"]} | {
        parse_id(row.get("campaign_id"), "campaign_id") for row in ad_rows + keyword_rows
    })
    campaign_rows = _exact(await campaigns.read_settings(
        api, login, campaign_ids=campaign_ids, limit=10000,
    ), "campaigns", campaign_ids) if campaign_ids else []
    if any(c.get("type") != "UNIFIED_CAMPAIGN" or c.get("state") == "ARCHIVED"
           for c in campaign_rows):
        raise ValueError("Preflight: требуется доступная неархивная ЕПК")
    for change in plan["campaigns"]:
        selected = (change.get("UnifiedCampaign", {}).get("PriorityGoals") or {}).get("Items", [])
        if selected:
            catalog = await goals.read(api, change["Id"])
            available = {parse_id(row["id"], "goal_id") for row in catalog["goals"]}
            if completeness.sources(goals=catalog) or not {
                row["GoalId"] for row in selected
            }.issubset(available):
                raise ValueError("Preflight: выбранные цели отсутствуют в каталоге кампании")
    group_ids = sorted({parse_id(row.get("ad_group_id"), "ad_group_id") for row in ad_rows})
    group_rows = _exact(await adgroups.read(
        api, login, ad_group_ids=group_ids, limit=10000,
    ), "groups", group_ids) if group_ids else []
    group_map = {row["id"]: row for row in group_rows}
    if any(group_map[a["ad_group_id"]].get("campaign_id") != a["campaign_id"] for a in ad_rows):
        raise ValueError("Preflight: группа относится к другой кампании")
    patches = {row["Id"]: row["ResponsiveAd"] for row in plan["ads"]}
    effective = [_patched_ad(row, patches[row["id"]]) for row in ad_rows]
    references = []
    for row in effective:
        value = {api_field: row.get(field) for api_field, field in AD_FIELDS_MAP.items()
                 if api_field not in {"CalloutSetting", "AdImageHashes"}}
        if row.get("business_id"):
            value["BusinessId"] = row["business_id"]
        campaign_setup._validate_responsive_ad(value, f"ad {row['id']}")
        if row.get("ad_extension_ids") is None:
            raise ValueError("Preflight: состав уточнений не прочитан")
        references.append({"AdImageHashes": row.get("ad_image_hashes") or [],
                           "AdExtensionIds": row["ad_extension_ids"],
                           "BusinessId": row.get("business_id")})
    sets = await assets.read_sets(
        api, login, [row["sitelink_set_id"] for row in effective if row.get("sitelink_set_id")],
    )
    for row in sets.values():
        if not policy.SITELINKS_MINIMUM <= len(row["Sitelinks"]) <= policy.SITELINKS_MAXIMUM:
            raise ValueError("Preflight: требуется от 4 до 8 быстрых ссылок")
    await preflight_refs.assets(api, login, references)
    token = landing.CACHE_SCOPE.set(login + ":repair:" + plan["plan_hash"])
    try:
        pages = await link_checks.check_live(
            api, login, campaign_rows, group_rows, effective, sitelink_sets=sets,
        ) if effective else []
    finally:
        landing.CACHE_SCOPE.reset(token)
    links = link_checks.summary(pages)
    if not links["all_ok"]:
        raise ValueError("Preflight: конечные URL с метками не проверены: " + str([
            {"url": p.get("url"), "check": p.get("site_check")}
            for p in pages if not (p.get("site_check") or {}).get("ok")
        ]))
    before = {"campaigns": campaign_rows, "ads": ad_rows, "keywords": keyword_rows}
    # Exclude moderation/serving statuses that can change without a settings edit.
    guard = {key: [{k: v for k, v in row.items() if k not in {
        "status", "serving_status", "autotargeting_search_bid_is_auto",
    }} for row in rows] for key, rows in {**before, "groups": group_rows}.items()}
    guard["sitelinks"] = sets
    source_hash = hashlib.sha256(json.dumps(
        guard, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return {"status": "PASS", "client_login": login, "plan_hash": plan["plan_hash"],
            "source_hash": source_hash, "before": before,
            "effective_link_checks": {**links, "pages": pages}}


async def _update(
    api: Any,
    service: str,
    collection: str,
    rows: list[dict[str, Any]],
    client_login: str,
) -> list[dict[str, Any]]:
    actions = []
    maximum = {"campaigns": 10, "ads": 1000, "keywords": 1000}[service]
    for offset in range(0, len(rows), maximum):
        chunk = rows[offset:offset + maximum]
        try:
            result = await api.call_v501(service, "update", {collection: chunk},
                                        client_login=client_login)
            batch = result.get("UpdateResults", [])
            actions.extend(batch)
            if len(batch) != len(chunk):
                raise RuntimeError(f"{service}.update: неполное число результатов")
        except Exception as exc:
            from .executor import BatchFailure
            raise BatchFailure(exc, actions) from exc
        if any(row.get("Errors") or row.get("Id") is None for row in batch):
            break
    return actions


async def apply(
    api: Any, plan: dict[str, Any], *, expected_preflight: dict | None = None,
) -> dict[str, Any]:
    login = plan["client_login"]
    checked = await preflight(api, plan)
    if expected_preflight is not None and (
        expected_preflight.get("plan_hash") != plan["plan_hash"]
        or expected_preflight.get("client_login") != login
        or expected_preflight.get("source_hash") != checked["source_hash"]
    ):
        raise ValueError("Состояние изменилось после preview; требуется новый preview")
    result = {
        "status": "complete",
        "executed": True,
        "client_login": login,
        "plan_hash": plan["plan_hash"],
        "activated": False,
        "updates": {},
    }
    result["preflight"] = checked
    image_ids = {row["Id"] for row in plan["ads"] if "AdImageHashes" in row["ResponsiveAd"]}
    if image_ids:
        result["before_image_updates"] = {
            "ads": [row for row in checked["before"]["ads"] if row["id"] in image_ids],
        }
    for key, service, collection in (("campaigns", "campaigns", "Campaigns"),
                                     ("ads", "ads", "Ads"),
                                     ("autotargetings", "keywords", "Keywords")):
        try:
            actions = await _update(api, service, collection, plan[key], login)
            result["updates"][key] = actions
            if (len(actions) != len(plan[key])
                    or any(row.get("Errors") or row.get("Id") is None for row in actions)):
                result["status"] = "partial"
                break
        except Exception as exc:  # noqa: BLE001 - preserve earlier writes
            result["updates"][key] = getattr(exc, "results", [])
            result["status"] = "partial"
            result["error"] = str(exc)
            break
    return result


async def readback(
    api: Any, plan: dict[str, Any], *, before: dict | None = None,
) -> dict[str, Any]:
    login = plan["client_login"]
    campaign_ids = [row["Id"] for row in plan["campaigns"]]
    ad_ids = [row["Id"] for row in plan["ads"]]
    keyword_ids = [row["Id"] for row in plan["autotargetings"]]
    actual_campaigns = (
        await campaigns.read_settings(api, login, campaign_ids=campaign_ids, limit=10000)
        if campaign_ids
        else {"campaigns": []}
    )
    actual_ads = (
        await ads.read(api, login, ad_ids=ad_ids, preview_limit=None, limit=10000)
        if ad_ids
        else {"ads": []}
    )
    actual_keywords = (
        await keywords.read(api, login, keyword_ids=keyword_ids)
        if keyword_ids
        else {"keywords": []}
    )
    campaign_map = {row["id"]: row for row in actual_campaigns["campaigns"]}
    await assets.enrich_ads(api, login, actual_ads["ads"])
    ad_map = {row["id"]: row for row in actual_ads["ads"]}
    keyword_map = {row["id"]: row for row in actual_keywords["keywords"]}
    incomplete = completeness.sources(
        campaigns=actual_campaigns, ads=actual_ads, keywords=actual_keywords,
    )
    mismatches = [{"reason": "incomplete_data", "sources": incomplete}] if incomplete else []
    for key, payload in (("campaigns", actual_campaigns), ("ads", actual_ads),
                         ("keywords", actual_keywords)):
        rows = payload[key]
        if len({row["id"] for row in rows}) != len(rows):
            mismatches.append({"service": key, "reason": "duplicate_readback_ids"})
    before_campaigns = {row["id"]: row for row in (before or {}).get("campaigns", [])}
    for expected in plan["campaigns"]:
        actual = campaign_map.get(expected["Id"])
        if actual is None:
            mismatches.append({"service": "campaigns", "id": expected["Id"], "reason": "missing"})
            continue
        changed = set()
        if "NegativeKeywords" in expected:
            changed.add("negative_keywords")
        if "PriorityGoals" in expected.get("UnifiedCampaign", {}):
            changed.add("priority_goals")
        previous = before_campaigns.get(expected["Id"], {})
        for field in previous.keys() - changed - {"status"}:
            if actual.get(field) != previous[field]:
                mismatches.append({"service": "campaigns", "id": expected["Id"],
                                   "reason": "preserved_" + field})
        if "PriorityGoals" in expected.get("UnifiedCampaign", {}):
            goals = expected["UnifiedCampaign"]["PriorityGoals"]
            expected_goals = (goals or {}).get("Items", [])
            if _goal_values(actual.get("priority_goals") or []) != _goal_values(expected_goals):
                mismatches.append({"service": "campaigns", "id": expected["Id"],
                                   "reason": "priority_goals"})
        if "NegativeKeywords" in expected and not phrases.equivalent(
            actual.get("negative_keywords") or [],
            expected["NegativeKeywords"]["Items"],
        ):
            mismatches.append({
                "service": "campaigns",
                "id": expected["Id"],
                "reason": "negative_keywords",
            })
    before_ads = {row["id"]: row for row in (before or {}).get("ads", [])}
    for expected in plan["ads"]:
        identifier = expected["Id"]
        actual = ad_map.get(identifier)
        if actual is None:
            mismatches.append({"service": "ads", "id": identifier, "reason": "missing"})
            continue
        previous = before_ads.get(identifier)
        wanted = _patched_ad(previous or {}, expected["ResponsiveAd"])
        fields = set(AD_FIELDS_MAP.values()) if previous is not None else set(wanted)
        if previous is not None:
            fields.update(PRESERVED_AD_FIELDS)
        for field in sorted(fields):
            if _ad_value(actual, field) != _ad_value(wanted, field):
                mismatches.append({"service": "ads", "id": identifier, "reason": field,
                                   "expected": wanted.get(field), "actual": actual.get(field)})
        if wanted.get("sitelink_set_id") is not None and not (
            policy.SITELINKS_MINIMUM <= (actual.get("sitelink_count") or 0)
            <= policy.SITELINKS_MAXIMUM
        ):
            mismatches.append({"service": "ads", "id": identifier, "reason": "sitelink_count"})
    for expected in plan["autotargetings"]:
        actual = keyword_map.get(expected["Id"])
        settings = expected["AutotargetingSettings"]
        if actual is None or actual.get("autotargeting") != {
            "categories": settings["Categories"],
            "brand_options": settings["BrandOptions"],
        }:
            mismatches.append({
                "service": "keywords",
                "id": expected["Id"],
                "reason": "autotargeting",
            })
    return {
        "verified": not mismatches,
        "mismatches": mismatches,
        "counts": {
            "campaigns": len(campaign_map),
            "ads": len(ad_map),
            "autotargetings": len(keyword_map),
        },
    }


def persist(payload: dict[str, Any], out_dir: Path) -> Path:
    payload = {key: value for key, value in payload.items() if key != "confirmation_required"}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    login = store.safe_stem(str(payload.get("client_login") or "client"))
    plan_hash = str(payload.get("plan_hash") or "")[:12]
    path = out_dir / f"repair_{login}_{stamp}_{plan_hash}.json"
    path.write_text(
        json.dumps(wire(payload), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path
