"""Append one draft audience group without changing its existing campaign.

Only a cloned owned ResponsiveAd and one catalog-verified audience segment are
supported. No moderation/resume calls; all mutations use the repair JournalAPI.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from typing import Any

from . import (
    ads,
    assets,
    audience_link,
    bundle,
    campaigns,
    goals,
    group_append,
    link_checks,
    preflight_refs,
)
from .identifiers import parse_id

GOAL_OPERATORS = {"ALL", "ANY", "NONE"}
MAX_LIFESPAN_DAYS = 540


def goal_rules(raw: Any) -> list[dict]:
    """Direct RetargetingList rules from [{operator, goals: [{goal_id, days}]}]."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
        raise ValueError("goal_rules: 1..10 rules required")
    rules, seen = [], set()
    for index, rule in enumerate(raw):
        prefix = f"goal_rules[{index}]"
        if not isinstance(rule, dict) or set(rule) != {"operator", "goals"}:
            raise ValueError(f"{prefix}: operator and goals required")
        if rule["operator"] not in GOAL_OPERATORS:
            raise ValueError(f"{prefix}.operator: ALL, ANY or NONE")
        if not isinstance(rule["goals"], list) or not 1 <= len(rule["goals"]) <= 50:
            raise ValueError(f"{prefix}.goals: 1..50 goals required")
        arguments = []
        for goal in rule["goals"]:
            if not isinstance(goal, dict) or set(goal) != {"goal_id", "days"}:
                raise ValueError(f"{prefix}.goals: goal_id and days required")
            identifier = parse_id(goal["goal_id"], f"{prefix}.goal_id")
            days = goal["days"]
            if type(days) is not int or not 1 <= days <= MAX_LIFESPAN_DAYS:
                raise ValueError(f"{prefix}.days: 1..{MAX_LIFESPAN_DAYS}")
            if identifier in seen:
                raise ValueError(f"{prefix}: duplicate goal {identifier}")
            seen.add(identifier)
            arguments.append({"ExternalId": identifier, "MembershipLifeSpan": days})
        rules.append({"Operator": rule["operator"], "Arguments": arguments})
    if all(rule["Operator"] == "NONE" for rule in rules):
        raise ValueError("goal_rules: at least one ALL or ANY rule required")
    return rules


def _canonical(rules: list) -> list:
    """Order-insensitive comparison of goal rules, lifespan included."""
    return sorted(
        (row.get("Operator"), sorted((parse_id(a.get("ExternalId")), a.get("MembershipLifeSpan"))
                                     for a in row.get("Arguments", [])))
        for row in rules
    )


def normalize(raw: Any, login: str) -> dict:
    base = {"campaign_id", "source_ad_id", "name"}
    if not isinstance(raw, dict) or set(raw) not in (base | {"segment_id"}, base | {"goal_rules"}):
        raise ValueError("audience_group requires campaign_id, source_ad_id, name and "
                         "exactly one of segment_id or goal_rules")
    keys = ("campaign_id", "source_ad_id") + (("segment_id",) if "segment_id" in raw else ())
    item = {key: parse_id(raw[key], key) for key in keys}
    if any(value <= 0 for value in item.values()):
        raise ValueError("audience_group IDs must be positive")
    if "goal_rules" in raw:
        item["goal_rules"] = goal_rules(raw["goal_rules"])
    name = raw["name"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 250:
        raise ValueError("audience_group name must contain 1..250 characters")
    item["name"] = name.strip()
    plan = {
        "schema": "direct_campaign_repair_v1",
        "client_login": login,
        "api_version": "v501",
        "campaigns": [],
        "ads": [],
        "autotargetings": [],
        "audience_group": item,
        "activated": False,
    }
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan["plan_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    plan["summary"] = {"campaigns": 0, "new_audience_groups": 1, "new_ads": 1}
    return plan


async def rows(api, login, service, collection, selection, fields):
    value = await api.call_v501(
        service,
        "get",
        {
            "SelectionCriteria": selection,
            "FieldNames": fields,
            "Page": {"Limit": 10000},
        },
        client_login=login,
    )
    if value == {}:
        return []
    result = value.get(collection)
    if (
        value.get("LimitedBy") is not None
        or not isinstance(result, list)
        or len({row.get("Id") for row in result}) != len(result)
        or any(not row.get("Id") for row in result)
    ):
        raise ValueError("Incomplete or duplicate audience inventory: " + service)
    return sorted(result, key=lambda row: row["Id"])


def stable(value):
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in {"status", "serving_status"}}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def clone_ad(source, rich=False):
    """Copy a ResponsiveAd; rich=True also keeps BusinessId and video extensions."""
    names = (
        "titles",
        "texts",
        "href",
        "display_url_path",
        "sitelink_set_id",
        "ad_extension_ids",
        "ad_image_hashes",
        "age_label",
        "erir_ad_description",
    )
    value = {k: deepcopy(source[k]) for k in names if source.get(k) is not None}
    if rich:
        for key in ("video_extension_ids", "business_id"):
            if source.get(key):
                value[key] = deepcopy(source[key])
    elif source.get("video_extension_ids") or source.get("business_id"):
        raise ValueError("Audience clone supports plain ResponsiveAd only")
    price = source.get("price_extension")
    if price:
        value["price_extension"] = {
            "price": str(Decimal(price["Price"]) / 1_000_000),
            "qualifier": price["PriceQualifier"],
            "currency": price["PriceCurrency"],
        }
        if price.get("OldPrice") is not None:
            value["price_extension"]["old_price"] = str(Decimal(price["OldPrice"]) / 1_000_000)
    value["action_button"] = {
        "text": "Узнать больше",
        "href": value.get("href"),
        "reason": "Изучить предложение на исходном лендинге",
    }
    compiled, findings = bundle._responsive_ad(value, "audience_group.ad", "network")
    if any(f["status"] == "BLOCK" for f in findings):
        raise ValueError("Source ad does not satisfy creative requirements")
    return compiled["ResponsiveAd"]


async def snapshot(api, plan):
    login, item = plan["client_login"], plan["audience_group"]
    cid = item["campaign_id"]
    found = group_append.complete(
        await campaigns.read_settings(api, login, campaign_ids=[cid], limit=10000), "campaigns"
    )
    if len(found) != 1 or found[0]["id"] != cid:
        raise ValueError("Campaign unavailable or foreign")
    campaign = found[0]
    strategy = campaign.get("bidding_strategy", {})
    if (
        campaign["type"] != "UNIFIED_CAMPAIGN"
        or campaign["state"] not in {"ON", "OFF", "SUSPENDED"}
        or campaign.get("package_bidding_strategy")
        or strategy.get("Search", {}).get("BiddingStrategyType") != "SERVING_OFF"
        or strategy.get("Network", {}).get("BiddingStrategyType")
        not in {"WB_MAXIMUM_CLICKS", "WB_MAXIMUM_CONVERSION_RATE"}
    ):
        raise ValueError("Only network-only UnifiedCampaign is supported")
    inventory = await group_append.inventory(api, login, [cid])
    owners = {g["id"]: g["campaign_id"] for g in inventory["groups"]}
    if any(r["campaign_id"] != cid for values in inventory.values() for r in values) or any(
        owners.get(r["ad_group_id"]) != cid for key in ("ads", "keywords") for r in inventory[key]
    ):
        raise ValueError("Foreign objects in campaign inventory")
    source = [a for a in inventory["ads"] if a["id"] == item["source_ad_id"]]
    if (
        len(source) != 1
        or source[0]["type"] != "RESPONSIVE_AD"
        or source[0]["state"] == "ARCHIVED"
        or source[0]["status"] in {"REJECTED", "UNKNOWN"}
    ):
        raise ValueError("Source ResponsiveAd unavailable")
    group = next(g for g in inventory["groups"] if g["id"] == source[0]["ad_group_id"])
    if group.get("tracking_params") or group.get("offer_retargeting") != "NO":
        raise ValueError("Source group must have no URL overrides or offer retargeting")
    catalog = await goals.read_retargeting(api, login)
    if "goal_rules" in item:
        wanted = {a["ExternalId"] for rule in item["goal_rules"] for a in rule["Arguments"]}
        by_id = {r["id"]: r for r in catalog["goals"]}
        if not catalog.get("complete") or any(
            by_id.get(goal, {}).get("type") != "goal" for goal in wanted
        ):
            raise ValueError("Retargeting goals are unavailable in the client's catalog")
        selected = [by_id[goal] for goal in sorted(wanted)]
    else:
        selected = [r for r in catalog["goals"] if r["id"] == item["segment_id"]]
        if len(selected) != 1 or selected[0]["type"] not in {"segment", "audience_segment"}:
            raise ValueError("Audience segment is unavailable in the client's catalog")
    lists = await rows(
        api,
        login,
        "retargetinglists",
        "RetargetingLists",
        {},
        ["Id", "Name", "Type", "Rules", "IsAvailable", "Scope"],
    )
    targets = await rows(
        api,
        login,
        "audiencetargets",
        "AudienceTargets",
        {"CampaignIds": [cid]},
        ["Id", "AdGroupId", "RetargetingListId", "State", "StrategyPriority"],
    )
    if any(t["AdGroupId"] not in owners for t in targets):
        raise ValueError("Foreign audience target")
    return {
        "campaign": stable(campaign),
        "inventory": stable(inventory),
        "source": stable(source[0]),
        "source_group": stable(group),
        "segment": selected if "goal_rules" in item else selected[0],
        "lists": lists,
        "targets": targets,
    }


def list_rules(item):
    if "goal_rules" in item:
        return deepcopy(item["goal_rules"])
    return [{"Operator": "ANY", "Arguments": [{"ExternalId": item["segment_id"]}]}]


def matching_lists(before, item):
    if "goal_rules" in item:
        wanted = _canonical(item["goal_rules"])
        same = lambda rules: _canonical(rules) == wanted  # noqa: E731
    else:
        wanted = list_rules(item)
        same = lambda rules: audience_link._rules(rules) == wanted  # noqa: E731
    matches = [r for r in before["lists"] if r["Type"] == "RETARGETING" and same(r["Rules"])]
    if len(matches) > 1:
        raise ValueError("Ambiguous existing audience list")
    if matches and (
        matches[0]["IsAvailable"] != "YES"
        or matches[0]["Scope"] not in {"FOR_TARGETS_AND_ADJUSTMENTS", "FOR_TARGETS_ONLY"}
    ):
        raise ValueError("Matching audience list unavailable")
    return matches


async def preflight(api, plan):
    before, item = await snapshot(api, plan), plan["audience_group"]
    inventory = before["inventory"]
    if len(inventory["groups"]) >= 1000 or any(
        g["name"].strip().casefold() == item["name"].casefold() for g in inventory["groups"]
    ):
        raise ValueError("Duplicate group name or group limit")
    matches = matching_lists(before, item)
    if matches and any(t["RetargetingListId"] == matches[0]["Id"] for t in before["targets"]):
        raise ValueError("Segment is already targeted in this campaign")
    ad = clone_ad(before["source"], rich="goal_rules" in item)
    group = {
        "CampaignId": item["campaign_id"],
        "Name": item["name"],
        "RegionIds": before["source_group"]["region_ids"],
        "UnifiedAdGroup": {"OfferRetargeting": "NO"},
    }
    refs = await assets.read_sets(api, plan["client_login"], [ad["SitelinkSetId"]])
    if any(not 4 <= len(s["Sitelinks"]) <= 8 for s in refs.values()):
        raise ValueError("Expected 4..8 sitelinks")
    await preflight_refs.assets(api, plan["client_login"], [ad])
    pages = await link_checks.check_live(
        api,
        plan["client_login"],
        [before["campaign"]],
        [before["source_group"]],
        [before["source"]],
        sitelink_sets=refs,
    )
    if not pages or not link_checks.summary(pages)["all_ok"]:
        raise ValueError("Effective landing URLs did not pass verification")
    return {
        "status": "PASS",
        "before": before,
        "ad": ad,
        "group": group,
        "existing_list_id": matches[0]["Id"] if matches else None,
        "sitelinks": refs,
        "effective_link_checks": pages,
        "new_ad_state": "draft",
        "launch": "not_performed",
    }


async def apply(api, plan, expected_preflight):
    checked = await preflight(api, plan)
    compared = ("before", "ad", "group", "existing_list_id", "sitelinks")
    if expected_preflight is None or any(checked[k] != expected_preflight[k] for k in compared):
        raise ValueError("Snapshot changed after preview")
    login, item = plan["client_login"], plan["audience_group"]
    added = {"responses": {}, "list_id": checked["existing_list_id"]}
    result = {
        **plan,
        "executed": True,
        "activated": False,
        "status": "partial",
        "preflight": checked,
        "added": added,
    }

    async def add(service, collection, row):
        response = await api.call_v501(service, "add", {collection: [row]}, client_login=login)
        added["responses"][service] = response
        values = response.get("AddResults", [])
        if len(values) != 1 or values[0].get("Errors") or not values[0].get("Id"):
            raise ValueError("Partial audience-group add: " + service)
        return parse_id(values[0]["Id"])

    try:
        if added["list_id"] is None:
            added["list_id"] = await add(
                "retargetinglists",
                "RetargetingLists",
                {
                    "Name": item["name"],
                    "Type": "RETARGETING",
                    "Rules": list_rules(item),
                },
            )
        added["group_id"] = await add("adgroups", "AdGroups", checked["group"])
        keys = await rows(
            api,
            login,
            "keywords",
            "Keywords",
            {"AdGroupIds": [added["group_id"]]},
            ["Id", "Keyword", "State"],
        )
        if any(k["Keyword"] != "---autotargeting" for k in keys):
            raise ValueError("Unexpected keywords in new group")
        active = [k["Id"] for k in keys if k["State"] != "SUSPENDED"]
        if active:
            response = await api.call_v501(
                "keywords", "suspend", {"SelectionCriteria": {"Ids": active}}, client_login=login
            )
            added["responses"]["keywords_suspend"] = response
            values = response.get("SuspendResults", [])
            if (
                len(values) != len(active)
                or any(v.get("Errors") for v in values)
                or {v.get("Id") for v in values} != set(active)
            ):
                raise ValueError("Could not disable new-group autotargeting")
        added["target_id"] = await add(
            "audiencetargets",
            "AudienceTargets",
            {
                "AdGroupId": added["group_id"],
                "RetargetingListId": added["list_id"],
                "StrategyPriority": "NORMAL",
            },
        )
        added["ad_id"] = await add(
            "ads", "Ads", {"AdGroupId": added["group_id"], "ResponsiveAd": checked["ad"]}
        )
        result["status"] = "complete"
    except Exception as exc:  # noqa: BLE001 - retain IDs; never repeat a write
        result["error"] = str(exc)
    return result


async def readback(api, plan, before, added):
    if not added or not all(added.get(k) for k in ("group_id", "ad_id", "target_id", "list_id")):
        return {"verified": False, "reason": "partial_add"}
    current = await snapshot(api, plan)
    gid, aid, tid = (added[k] for k in ("group_id", "ad_id", "target_id"))
    rich = "goal_rules" in plan["audience_group"]
    errors = []
    if current["campaign"] != before["campaign"]:
        errors.append("campaign_changed")
    for key in ("groups", "ads", "keywords"):
        old, now = before["inventory"][key], current["inventory"][key]
        mapping = {r["id"]: r for r in now}
        if any(mapping.get(r["id"]) != r for r in old):
            errors.append("existing_" + key + "_changed")
        expected = {r["id"] for r in old}
        if key == "groups":
            expected.add(gid)
        elif key == "ads":
            expected.add(aid)
        else:
            expected.update(
                r["id"]
                for r in now
                if r["ad_group_id"] == gid and r["autotargeting"] and r["state"] == "SUSPENDED"
            )
        if set(mapping) != expected:
            errors.append("unexpected_" + key)
    group = next((g for g in current["inventory"]["groups"] if g["id"] == gid), {})
    actual_ads = group_append.complete(
        await ads.read(api, plan["client_login"], ad_ids=[aid], include_archived=True), "ads"
    )
    ad = actual_ads[0] if len(actual_ads) == 1 and actual_ads[0]["id"] == aid else {}
    if (
        group.get("name") != plan["audience_group"]["name"]
        or group.get("region_ids") != before["source_group"]["region_ids"]
        or group.get("offer_retargeting") != "NO"
        or group.get("tracking_params")
    ):
        errors.append("group_fields")
    if (
        ad.get("ad_group_id") != gid
        or ad.get("state") != "OFF"
        or ad.get("status") != "DRAFT"
        or clone_ad(ad, rich) != clone_ad(before["source"], rich)
    ):
        errors.append("ad_fields_or_state")
    target = {
        "Id": tid,
        "AdGroupId": gid,
        "RetargetingListId": added["list_id"],
        "State": "ON",
        "StrategyPriority": "NORMAL",
    }
    if current["targets"] != sorted(before["targets"] + [target], key=lambda r: r["Id"]):
        errors.append("audience_targets")
    matches = matching_lists(current, plan["audience_group"])
    if not matches or matches[0]["Id"] != added["list_id"]:
        errors.append("segment_rule")
    return {
        "verified": not errors,
        "mismatches": errors,
        "group_id": gid,
        "ad_id": aid,
        "target_id": tid,
        "list_id": added["list_id"],
        "campaign_state": current["campaign"]["state"],
        "new_ad_state": ad.get("state"),
    }
