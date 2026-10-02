"""Consolidate identical, never-moderated Search groups without launching ads.

The caller explicitly lists the complete campaign inventory and final semantics.
Only redundant draft objects are deleted, after checking the retained group.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from . import bundle, campaigns, group_append, phrases
from .identifiers import parse_id


def normalize(raw, login):
    fields = {"campaign_id", "group_ids", "keep_group_id", "name", "keywords",
              "negative_keywords", "research_note"}
    if not isinstance(raw, dict) or set(raw) - {"prepared_only", "empty_groups_only"} != fields:
        raise ValueError("draft_group_merge requires " + ", ".join(sorted(fields)))
    item = deepcopy(raw)
    if "prepared_only" in item and type(item["prepared_only"]) is not bool:
        raise ValueError("prepared_only must be boolean")
    if "empty_groups_only" in item and (type(item["empty_groups_only"]) is not bool
                                       or not item.get("prepared_only")):
        raise ValueError("empty_groups_only requires prepared_only")
    for key in ("campaign_id", "keep_group_id"):
        item[key] = parse_id(item[key])
        if item[key] <= 0:
            raise ValueError("Positive IDs required")
    if not isinstance(item["group_ids"], list) or not 2 <= len(item["group_ids"]) <= 100:
        raise ValueError("List 2-100 source groups explicitly")
    item["group_ids"] = [parse_id(x) for x in item["group_ids"]]
    if (len(set(item["group_ids"])) != len(item["group_ids"])
            or min(item["group_ids"]) <= 0 or item["keep_group_id"] not in item["group_ids"]):
        raise ValueError("Unique positive groups including keep_group_id required")
    for key, limit in (("name", 255), ("research_note", 4000)):
        value = item[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError("Nonempty bounded " + key + " required")
        item[key] = value.strip()
    values = item["keywords"]
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        raise ValueError("1-200 final keywords required")
    for value in values:
        if (not isinstance(value, str) or value != value.strip()
                or len(value) > 4096 or len(value.split()) > 7
                or value == "---autotargeting"):
            raise ValueError("Invalid manual keyword")
        phrases.validate(value, "draft_group_merge.keywords")
    if len({phrases.canonical(x) for x in values}) != len(values):
        raise ValueError("Duplicate keywords")
    if not isinstance(item["negative_keywords"], list):
        raise ValueError("negative_keywords must be a list")
    item["negative_keywords"] = bundle._negative_phrases(
        item["negative_keywords"], "draft_group_merge.negative_keywords",
        maximum_total_length=4096)
    plan = {"schema": "direct_campaign_repair_v1", "client_login": login,
            "api_version": "v501", "campaigns": [], "ads": [], "autotargetings": [],
            "draft_group_merge": item, "activated": False}
    plan["plan_hash"] = hashlib.sha256(json.dumps(
        plan, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    plan["summary"] = {"source_groups": len(item["group_ids"]), "final_groups": 1,
                       "final_ads": 1, "final_keywords": len(values),
                       "deleted_draft_groups": len(item["group_ids"]) - 1}
    return plan


async def _raw(api, login, service, collection, cid, fields):
    params = {
        "SelectionCriteria": {"CampaignIds": [cid]}, "FieldNames": fields,
        "Page": {"Limit": 10000}}
    if service == "bidmodifiers":
        params["SelectionCriteria"]["Levels"] = ["CAMPAIGN", "AD_GROUP"]
        params["DemographicsAdjustmentFieldNames"] = ["Gender", "Age", "BidModifier", "Enabled"]
    result = await api.call_v501(service, "get", params, client_login=login)
    if result == {}:  # Direct omits the collection for a successful empty get.
        return []
    rows = result.get(collection)
    if (not isinstance(rows, list) or result.get("LimitedBy") is not None
            or len({r.get("Id") for r in rows}) != len(rows)):
        raise ValueError("Incomplete " + collection)
    return sorted(rows, key=lambda r: parse_id(r["Id"]))


async def snapshot(api, plan):
    login, item = plan["client_login"], plan["draft_group_merge"]
    cid = item["campaign_id"]
    cs = group_append.complete(await campaigns.read_settings(
        api, login, campaign_ids=[cid], limit=10000), "campaigns")
    if len(cs) != 1 or parse_id(cs[0]["id"]) != cid:
        raise ValueError("Owned campaign unavailable")
    c = cs[0]
    strategy = c.get("bidding_strategy") or {}
    if (c.get("type") != "UNIFIED_CAMPAIGN" or c.get("state") != "OFF"
            or c.get("status") != "DRAFT" or c.get("package_bidding_strategy")
            or strategy.get("Search", {}).get("PlacementTypes", {}).get("SearchResults") != "YES"
            or strategy.get("Network", {}).get("BiddingStrategyType") != "SERVING_OFF"):
        raise ValueError("Only OFF/DRAFT search-only UnifiedCampaign is supported")
    inv = await group_append.inventory(api, login, [cid])
    owners = {parse_id(g["id"]): parse_id(g["campaign_id"]) for g in inv["groups"]}
    if (any(parse_id(r["campaign_id"]) != cid for rows in inv.values() for r in rows)
            or any(owners.get(parse_id(r["ad_group_id"])) != cid
                   for key in ("ads", "keywords") for r in inv[key])):
        raise ValueError("Foreign objects in campaign inventory")
    if any(g.get("status") != "DRAFT" or g.get("type") != "UNIFIED_AD_GROUP"
           or g.get("subtype") != "NONE" or g.get("offer_retargeting") != "NO"
           for g in inv["groups"]):
        raise ValueError("Only plain draft UnifiedAdGroups are supported")
    if any(a.get("state") != "OFF" or a.get("status") != "DRAFT"
           or a.get("type") != "RESPONSIVE_AD" for a in inv["ads"]):
        raise ValueError("All ads must be OFF/DRAFT ResponsiveAds")
    targets = await _raw(api, login, "audiencetargets", "AudienceTargets", cid,
                         ["Id", "AdGroupId", "CampaignId"])
    modifiers = await _raw(api, login, "bidmodifiers", "BidModifiers", cid,
                           ["Id", "AdGroupId", "CampaignId", "Type"])
    if targets or any(r.get("AdGroupId") is not None for r in modifiers):
        raise ValueError("Audience targets and group bid modifiers are unsupported")
    return {"campaigns": cs, **inv, "modifiers": modifiers, "targets": targets}


def _without(row, fields):
    return {k: v for k, v in row.items() if k not in fields}


async def preflight(api, plan):
    before = await snapshot(api, plan)
    item = plan["draft_group_merge"]
    gids = set(item["group_ids"])
    if {parse_id(g["id"]) for g in before["groups"]} != gids:
        raise ValueError("Source IDs must match the entire campaign")
    baseline = _without(before["groups"][0], {"id", "name", "serving_status"})
    others = [g for g in before["groups"] if parse_id(g["id"]) != item["keep_group_id"]]
    if item.get("prepared_only"):
        baseline = _without(others[0], {"id", "name", "serving_status"})
        target = next(g for g in before["groups"] if parse_id(g["id"]) == item["keep_group_id"])
        if (target["name"] != item["name"]
                or not phrases.equivalent(target["negative_keywords"], item["negative_keywords"])
                or _without(target, {"id", "name", "serving_status", "negative_keywords"})
                != _without(baseline, {"negative_keywords"})):
            raise ValueError("Prepared target settings differ")
        manual = [k for k in before["keywords"]
                  if parse_id(k["ad_group_id"]) == item["keep_group_id"]
                  and k["keyword"] != "---autotargeting"]
        if not _keys_match(manual, item, {parse_id(k["id"]) for k in manual}):
            raise ValueError("Prepared target keywords differ")
    comparison = others if item.get("prepared_only") else before["groups"]
    if any(_without(g, {"id", "name", "serving_status"}) != baseline for g in comparison):
        raise ValueError("Source group settings differ")
    ads = before["ads"]
    expected_ad_groups = {item["keep_group_id"]} if item.get("empty_groups_only") else gids
    if (len(ads) != len(expected_ad_groups)
            or {parse_id(a["ad_group_id"]) for a in ads} != expected_ad_groups):
        raise ValueError("Exactly one ad per source group required")
    if item.get("empty_groups_only") and any(
            parse_id(k["ad_group_id"]) != item["keep_group_id"]
            and k["keyword"] != "---autotargeting" for k in before["keywords"]):
        raise ValueError("Obsolete groups still have manual keywords")
    baseline_ad = _without(ads[0], {"id", "ad_group_id"})
    if any(_without(a, {"id", "ad_group_id"}) != baseline_ad for a in ads):
        raise ValueError("Source ads differ")
    autos = [k for k in before["keywords"] if k["keyword"] == "---autotargeting"]
    if (len(autos) != len(gids) or {parse_id(k["ad_group_id"]) for k in autos} != gids
            or any(k.get("state") != "ON" for k in before["keywords"])
            or any(k.get("autotargeting") != autos[0].get("autotargeting") for k in autos)):
        raise ValueError("Identical active autotargeting and active manual keys required")
    return {"status": "PASS", "before": before, "client_login": plan["client_login"],
            "plan_hash": plan["plan_hash"]}


def stable(value):
    if isinstance(value, dict):
        return {k: sorted(phrases.canonical(x) for x in v) if k == "negative_keywords"
                else stable(v) for k, v in value.items()
                if k not in {"serving_status", "autotargeting_search_bid_is_auto"}}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


async def _write(api, login, service, method, params, results):
    result = await api.call_v501(service, method, params, client_login=login)
    results.append({"service": service, "method": method, "result": result})
    actions = result.get({"add": "AddResults", "update": "UpdateResults",
                          "delete": "DeleteResults"}[method], [])
    requested = (params["SelectionCriteria"]["Ids"] if method == "delete"
                 else next(iter(params.values())))
    ids = [parse_id(r["Id"]) for r in actions if r.get("Id") is not None]
    if (len(actions) != len(requested) or len(ids) != len(set(ids))
            or len(ids) != len(actions) or any(r.get("Errors") for r in actions)
            or (method == "delete" and set(ids) != set(requested))
            or (method == "update" and set(ids) != {r["Id"] for r in requested})):
        raise ValueError("Partial or ambiguous " + service + "." + method)
    return ids


async def apply(api, plan, expected_preflight):
    checked = await preflight(api, plan)
    if checked != expected_preflight:
        raise ValueError("Draft campaign changed after preview")
    item, login = plan["draft_group_merge"], plan["client_login"]
    keep, before = item["keep_group_id"], checked["before"]
    result = {**plan, "status": "partial", "executed": True, "preflight": checked,
              "updates": [], "added": {"keywords": []}, "activated": False}
    writes = result["updates"]
    async def delete(service, ids):
        if ids:
            await _write(api, login, service, "delete", {"SelectionCriteria": {"Ids": ids}}, writes)
    try:
        old_manual = [parse_id(k["id"]) for k in before["keywords"]
                      if parse_id(k["ad_group_id"]) == keep and k["keyword"] != "---autotargeting"]
        if item.get("prepared_only"):
            result["added"] = {"keywords": old_manual, "reused_keywords": True}
        else:
            await delete("keywords", old_manual)
            result["added"]["keywords"] = await _write(api, login, "keywords", "add", {
                "Keywords": [{"AdGroupId": keep, "Keyword": k} for k in item["keywords"]]}, writes)
            await _write(api, login, "adgroups", "update", {"AdGroups": [{"Id": keep,
                "Name": item["name"],
                "NegativeKeywords": {"Items": item["negative_keywords"]}}]}, writes)
        # Before removing redundant objects, independently verify the replacement
        # and preservation of all other draft groups, ads and campaign settings.
        intermediate = await snapshot(api, plan)
        expected = deepcopy(before)
        for g in expected["groups"]:
            if parse_id(g["id"]) == keep:
                g.update(name=item["name"], negative_keywords=item["negative_keywords"])
        actual_keys = intermediate.pop("keywords")
        original_keys = expected.pop("keywords")
        kept_original = [k for k in original_keys if parse_id(k["id"]) not in old_manual]
        new_ids = set(result["added"]["keywords"])
        observed_old = [k for k in actual_keys if parse_id(k["id"]) not in new_ids]
        observed_new = [k for k in actual_keys if parse_id(k["id"]) in new_ids]
        if (stable(intermediate) != stable(expected)
                or stable(observed_old) != stable(kept_original)
                or not _keys_match(observed_new, item, new_ids)):
            raise ValueError("Replacement verification failed; redundant groups retained")
        obsolete = set(item["group_ids"]) - {keep}
        await delete("ads", [parse_id(a["id"]) for a in before["ads"]
                             if parse_id(a["ad_group_id"]) in obsolete])
        await delete("keywords", [parse_id(k["id"]) for k in before["keywords"]
                                  if parse_id(k["ad_group_id"]) in obsolete
                                  and k["keyword"] != "---autotargeting"])
        # Native UnifiedAdGroup autotargeting is not deletable via Keywords.delete.
        # It is intrinsic to the group, and disappears with the empty group.
        await delete("adgroups", sorted(obsolete))
        result["status"] = "complete"
    except Exception as exc:  # noqa: BLE001 - preserve partial writes, never replay
        result["error"] = str(exc)
    return result


def _keys_match(rows, item, ids):
    return (len(rows) == len(ids) == len(item["keywords"])
            and {parse_id(r["id"]) for r in rows} == ids
            and all(parse_id(r["ad_group_id"]) == item["keep_group_id"]
                    and r.get("state") == "ON" for r in rows)
            and phrases.equivalent([r["keyword"] for r in rows], item["keywords"]))


async def readback(api, plan, before, added):
    current, expected = await snapshot(api, plan), deepcopy(before)
    item = plan["draft_group_merge"]
    keep = item["keep_group_id"]
    expected["groups"] = [g for g in expected["groups"] if parse_id(g["id"]) == keep]
    expected["groups"][0].update(name=item["name"], negative_keywords=item["negative_keywords"])
    expected["ads"] = [a for a in expected["ads"] if parse_id(a["ad_group_id"]) == keep]
    expected["keywords"] = [k for k in expected["keywords"]
                            if parse_id(k["ad_group_id"]) == keep
                            and k["keyword"] == "---autotargeting"]
    manual = [k for k in current["keywords"] if k["keyword"] != "---autotargeting"]
    current["keywords"] = [k for k in current["keywords"] if k["keyword"] == "---autotargeting"]
    ids = {parse_id(x) for x in (added or {}).get("keywords", [])}
    verified = stable(current) == stable(expected) and _keys_match(manual, item, ids)
    return {"verified": verified,
            "mismatches": [] if verified else [{"reason": "merge_or_preservation"}],
            "counts": {"groups": len(current["groups"]), "ads": len(current["ads"]),
                       "manual_keywords": len(manual)}}
