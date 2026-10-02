"""Explicit removal of manual keywords/regional modifiers in stopped draft UPCs."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from . import account, adgroups, ads, campaigns, keywords
from .identifiers import parse_id


def normalize(raw, login):
    fields = {"campaign_ids", "keyword_ids", "regional_modifier_ids"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise ValueError(
            "remove_criteria: campaign_ids, keyword_ids, regional_modifier_ids required"
        )
    value = {}
    for field in fields:
        rows = raw[field]
        maximum = 10 if field == "campaign_ids" else 1000
        if not isinstance(rows, list) or len(rows) > maximum:
            raise ValueError("Invalid ID list: " + field)
        value[field] = [parse_id(x, field) for x in rows]
        if any(x <= 0 for x in value[field]) or len(set(value[field])) != len(rows):
            raise ValueError("Unique positive IDs required: " + field)
        value[field].sort()
    if not value["campaign_ids"] or not (value["keyword_ids"] or value["regional_modifier_ids"]):
        raise ValueError("Campaign scope and explicit removals required")
    plan = {"schema": "direct_campaign_repair_v1", "client_login": login,
            "api_version": "v501", "campaigns": [], "ads": [], "autotargetings": [],
            "remove_criteria": value, "activated": False}
    plan["plan_hash"] = hashlib.sha256(json.dumps(
        plan, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    plan["summary"] = {"campaigns": 0, "ads": 0, "autotargetings": 0,
                       "removed_keywords": len(value["keyword_ids"]),
                       "removed_regional_modifiers": len(value["regional_modifier_ids"])}
    return plan


def complete(data, key):
    rows = data.get(key)
    if (data.get("truncated") or data.get("error") or not isinstance(rows, list)
            or any(not isinstance(r, dict) or not r.get("id") for r in rows)
            or len({r["id"] for r in rows}) != len(rows)):
        raise ValueError("Incomplete or duplicate inventory: " + key)
    return sorted(deepcopy(rows), key=lambda r: r["id"])


async def snapshot(api, plan):
    login, ids = plan["client_login"], plan["remove_criteria"]["campaign_ids"]
    cs = complete(await campaigns.read_settings(api, login, campaign_ids=ids), "campaigns")
    if ({r["id"] for r in cs} != set(ids) or any(
            r.get("state") != "OFF" or r.get("status") != "DRAFT"
            or r.get("type") != "UNIFIED_CAMPAIGN" for r in cs)):
        raise ValueError("Owned OFF/DRAFT unified campaigns required")
    gs = complete(await adgroups.read(api, login, campaign_ids=ids, limit=10000), "groups")
    ad = complete(await ads.read(api, login, campaign_ids=ids, limit=10000,
                                 preview_limit=None), "ads")
    ks = complete(await keywords.read(api, login, campaign_ids=ids, limit=10000), "keywords")
    ms = complete(await account._bid_modifiers(api, login, ids), "items")
    if any(r.get("campaign_id") not in ids for r in gs + ad + ks + ms):
        raise ValueError("Foreign campaign object")
    group_ids = {r["id"] for r in gs}
    if any(r.get("ad_group_id") not in group_ids for r in ad + ks):
        raise ValueError("Foreign group object")
    # Serving eligibility may change independently; targeting/state remain exact.
    for row in gs + ad + ks:
        row.pop("serving_status", None)
    return {"campaigns": cs, "groups": gs, "ads": ad, "keywords": ks, "modifiers": ms}


async def preflight(api, plan):
    before = await snapshot(api, plan)
    v = plan["remove_criteria"]
    selected = {r["id"]: r for r in before["keywords"] if r["id"] in v["keyword_ids"]}
    if set(selected) != set(v["keyword_ids"]) or any(
            r.get("keyword") == "---autotargeting" for r in selected.values()):
        raise ValueError("Only existing manual keywords can be removed")
    for group in {r["ad_group_id"] for r in selected.values()}:
        if not any(r["ad_group_id"] == group and r["id"] not in selected
                   and r.get("keyword") != "---autotargeting" for r in before["keywords"]):
            raise ValueError("Cannot remove the last manual keyword from a group")
    modifiers = {r["id"]: r for r in before["modifiers"]
                 if r["id"] in v["regional_modifier_ids"]}
    if set(modifiers) != set(v["regional_modifier_ids"]) or any(
            r.get("type") != "REGIONAL_ADJUSTMENT" for r in modifiers.values()):
        raise ValueError("Only existing regional modifiers can be removed")
    return {"status": "PASS", "before": before,
            "remove_keywords": list(selected.values()),
            "remove_regional_modifiers": list(modifiers.values())}


async def apply(api, plan, expected_preflight):
    checked = await preflight(api, plan)
    if checked != expected_preflight:
        raise ValueError("Removal snapshot changed after preview")
    result = {**plan, "status": "complete", "executed": True,
              "preflight": checked, "added": {}, "activated": False}
    for field, service in (("keyword_ids", "keywords"),
                           ("regional_modifier_ids", "bidmodifiers")):
        ids = plan["remove_criteria"][field]
        if not ids:
            continue
        response = await api.call_v501(service, "delete", {"SelectionCriteria": {"Ids": ids}},
                                      client_login=plan["client_login"])
        rows = response.get("DeleteResults", [])
        result["added"][field] = rows
        if (len(rows) != len(ids) or {r.get("Id") for r in rows} != set(ids)
                or any(r.get("Errors") for r in rows)):
            result["status"] = "partial"
            break
    return result


async def readback(api, plan, before, added=None):
    current, expected = await snapshot(api, plan), deepcopy(before)
    for key, field in (("keywords", "keyword_ids"), ("modifiers", "regional_modifier_ids")):
        expected[key] = [r for r in expected[key] if r["id"] not in plan["remove_criteria"][field]]
    return {"verified": current == expected, "current": current}
