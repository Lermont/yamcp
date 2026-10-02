"""Guarded append-only region exclusions for owned stopped UPC groups."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from . import adgroups, campaigns, regions
from .identifiers import parse_id


def normalize(raw, login):
    if not isinstance(raw, dict) or set(raw) != {"campaign_id", "group_ids", "region_ids"}:
        raise ValueError("region_exclusions: campaign_id, group_ids, region_ids required")
    value = {"campaign_id": parse_id(raw["campaign_id"], "campaign_id")}
    for key in ("group_ids", "region_ids"):
        rows = raw[key]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise ValueError(key + ": 1-100 IDs required")
        value[key] = [parse_id(x, key) for x in rows]
        if any(x <= 0 for x in value[key]) or len(set(value[key])) != len(rows):
            raise ValueError(key + ": unique positive IDs required")
    plan = {"schema": "direct_campaign_repair_v1", "client_login": login,
            "api_version": "v501", "campaigns": [], "ads": [], "autotargetings": [],
            "region_exclusions": value, "activated": False}
    plan["plan_hash"] = hashlib.sha256(json.dumps(
        plan, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    plan["summary"] = {"campaigns": 0, "ads": 0, "autotargetings": 0,
                       "groups_with_region_exclusions": len(value["group_ids"])}
    return plan


async def snapshot(api, plan):
    value, login = plan["region_exclusions"], plan["client_login"]
    cs = await campaigns.read_settings(api, login, campaign_ids=[value["campaign_id"]])
    rows = cs["campaigns"]
    if (len(rows) != 1 or rows[0]["id"] != value["campaign_id"]
            or rows[0]["type"] != "UNIFIED_CAMPAIGN" or rows[0]["state"] != "OFF"):
        raise ValueError("Owned stopped unified campaign required")
    gs = await adgroups.read(api, login, ad_group_ids=value["group_ids"], limit=10000)
    found = gs["groups"]
    if (gs.get("truncated") or len(found) != len(value["group_ids"])
            or {g["id"] for g in found} != set(value["group_ids"])
            or any(g["campaign_id"] != value["campaign_id"] for g in found)):
        raise ValueError("Incomplete, duplicate or foreign groups")
    for row in found:
        if 0 in row["region_ids"] or not any(x > 0 for x in row["region_ids"]):
            raise ValueError("Explicit positive regions required; worldwide is unsupported")
        row.pop("status", None)
        row.pop("serving_status", None)
    rows[0].pop("status", None)
    return {"campaign": rows[0], "groups": sorted(found, key=lambda x: x["id"])}


def updates(plan, before):
    excluded = {-x for x in plan["region_exclusions"]["region_ids"]}
    result = []
    for row in before["groups"]:
        if set(row["region_ids"]) & {-x for x in excluded}:
            raise ValueError("Exclusion conflicts with an explicitly included region")
        result.append({"Id": row["id"], "RegionIds": sorted(set(row["region_ids"]) | excluded)})
    return result


async def preflight(api, plan):
    before = await snapshot(api, plan)
    resolved = await regions.lookup(api, ids=plan["region_exclusions"]["region_ids"],
                                    client_login=plan["client_login"])
    if resolved.get("unknown_ids"):
        raise ValueError("Unknown excluded regions")
    # Negative RegionIds only subtract descendants of included regions (API 5120).
    # Resolve the entire ancestor chain, failing closed on broken/cyclic data.
    nodes = {r["id"]: r for r in resolved["resolved"]}
    for region_id in plan["region_exclusions"]["region_ids"]:
        ancestors, seen = set(), {region_id}
        parent = nodes[region_id].get("parent_id")
        for _ in range(regions.MAX_DEPTH):
            if parent is None:
                break
            parent = int(parent)
            if parent in seen:
                raise ValueError("Cyclic region ancestry")
            seen.add(parent)
            ancestors.add(parent)
            if parent not in nodes:
                result = await regions.lookup(api, ids=[parent], client_login=plan["client_login"])
                if result.get("unknown_ids") or not result.get("resolved"):
                    raise ValueError("Incomplete region ancestry")
                nodes.update({r["id"]: r for r in result["resolved"]})
            parent = nodes[parent].get("parent_id")
        if parent is not None:
            raise ValueError("Incomplete region ancestry")
        if any(not ancestors.intersection(g["region_ids"]) for g in before["groups"]):
            raise ValueError("Excluded region must be inside an included region in every group")
    return {"status": "PASS", "before": before, "updates": updates(plan, before),
            "regions": resolved["resolved"]}


async def apply(api, plan, expected_preflight):
    checked = await preflight(api, plan)
    if expected_preflight != checked:
        raise ValueError("Region exclusion snapshot changed after preview")
    response = await api.call_v501("adgroups", "update", {"AdGroups": checked["updates"]},
                                  client_login=plan["client_login"])
    rows = response.get("UpdateResults", [])
    ids = [r.get("Id") for r in rows]
    complete = (len(ids) == len(checked["updates"]) and set(ids) ==
                set(plan["region_exclusions"]["group_ids"]) and
                not any(r.get("Errors") for r in rows))
    return {**plan, "status": "complete" if complete else "partial", "executed": True,
            "preflight": checked, "added": {"updated_groups": rows}, "activated": False}


async def readback(api, plan, before, added=None):
    current, expected = await snapshot(api, plan), deepcopy(before)
    values = {r["Id"]: r["RegionIds"] for r in updates(plan, before)}
    for row in expected["groups"]:
        row["region_ids"] = values[row["id"]]
    for row in current["groups"]:
        row["region_ids"] = sorted(row["region_ids"])
    return {"verified": current == expected, "current": current}
