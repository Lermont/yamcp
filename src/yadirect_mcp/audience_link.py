"""Link one existing Metrika segment to an owned, stopped network group.

Threat boundary: no new campaign, no launch, no replacement of existing targets.
The expected segment ID, list ID, group ID and live snapshot bind the approval.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import goals
from .identifiers import parse_id


def normalize(raw: Any, login: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != {
        "ad_group_id", "retargeting_list_id", "segment_id",
    }:
        raise ValueError("audience_link requires ad_group_id, retargeting_list_id, segment_id")
    link = {key: parse_id(value, key) for key, value in raw.items()}
    plan = {"schema": "direct_campaign_repair_v1", "client_login": login,
            "api_version": "v501", "campaigns": [], "ads": [], "autotargetings": [],
            "audience_link": link, "activated": False}
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan["plan_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    plan["summary"] = {"campaigns": 0, "ads": 0, "autotargetings": 0, "audience_links": 1}
    return plan


def _rules(rows: list) -> list:
    # This contract accepts only a catalog-verified Metrika segment; API ignores its lifespan.
    return [{"Operator": row.get("Operator"), "Arguments": [
        {key: value for key, value in arg.items()
         if value is not None and key != "MembershipLifeSpan"}
        for arg in row.get("Arguments", [])]} for row in rows]


async def snapshot(api: Any, plan: dict) -> dict:
    link, login = plan["audience_link"], plan["client_login"]

    async def read(service, params, collection):
        value = await api.call_v501(service, "get", params, client_login=login)
        # Direct get returns an empty structure when no objects match.
        if value == {}:
            return []
        if value.get("LimitedBy") is not None or not isinstance(value.get(collection), list):
            raise ValueError(
                "Incomplete audience link snapshot: " + service + ": " + json.dumps(value))
        return value[collection]

    groups = await read("adgroups", {
        "SelectionCriteria": {"Ids": [link["ad_group_id"]]},
        "FieldNames": ["Id", "CampaignId", "Type", "Status"],
        "UnifiedAdGroupFieldNames": ["OfferRetargeting"],
    }, "AdGroups")
    if len(groups) != 1 or groups[0].get("Id") != link["ad_group_id"]:
        raise ValueError("Audience group not owned or unavailable")
    group = groups[0]
    if group.get("Type") != "UNIFIED_AD_GROUP":
        raise ValueError("Only unified groups are supported")
    if group.get("UnifiedAdGroup", {}).get("OfferRetargeting") != "NO":
        raise ValueError("Offer retargeting must be disabled")
    campaigns = await read("campaigns", {
        "SelectionCriteria": {"Ids": [group["CampaignId"]]},
        "FieldNames": ["Id", "Type", "State"],
        "UnifiedCampaignFieldNames": ["BiddingStrategy"],
    }, "Campaigns")
    if len(campaigns) != 1 or campaigns[0].get("Id") != group["CampaignId"]:
        raise ValueError("Audience campaign not owned or unavailable")
    campaign = campaigns[0]
    strategy = campaign.get("UnifiedCampaign", {}).get("BiddingStrategy", {})
    if (campaign.get("Type") != "UNIFIED_CAMPAIGN" or campaign.get("State") != "OFF"
            or strategy.get("Search", {}).get("BiddingStrategyType") != "SERVING_OFF"
            or strategy.get("Network", {}).get("BiddingStrategyType") == "SERVING_OFF"
            or not strategy.get("Network")):
        raise ValueError("Only stopped network-only campaigns can receive this audience")
    lists = await read("retargetinglists", {
        "SelectionCriteria": {"Ids": [link["retargeting_list_id"]]},
        "FieldNames": ["Id", "Type", "Rules", "IsAvailable", "Scope"],
    }, "RetargetingLists")
    expected = [{"Operator": "ANY", "Arguments": [{"ExternalId": link["segment_id"]}]}]
    if (len(lists) != 1 or lists[0].get("Id") != link["retargeting_list_id"]
            or lists[0].get("Type") != "RETARGETING"
            or lists[0].get("IsAvailable") != "YES"
            or lists[0].get("Scope") not in {"FOR_TARGETS_AND_ADJUSTMENTS", "FOR_TARGETS_ONLY"}
            or _rules(lists[0].get("Rules", [])) != expected):
        raise ValueError(
            "Existing audience list mismatch: " + json.dumps(lists, ensure_ascii=False))
    catalog = await goals.read_retargeting(api, login)
    selected = [row for row in catalog["goals"] if row["id"] == link["segment_id"]]
    if len(selected) != 1 or selected[0]["type"] != "segment":
        raise ValueError("Selected Metrika segment unavailable")
    keywords = await read("keywords", {
        "SelectionCriteria": {"AdGroupIds": [link["ad_group_id"]]},
        "FieldNames": ["Id", "Keyword", "State"], "Page": {"Limit": 10000},
    }, "Keywords")
    if any(row.get("Keyword") != "---autotargeting" or row.get("State") != "SUSPENDED"
           for row in keywords):
        raise ValueError("Parallel keywords or active autotargeting would broaden the audience")
    targets = await read("audiencetargets", {
        "SelectionCriteria": {"AdGroupIds": [link["ad_group_id"]]},
        "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "State", "StrategyPriority"],
        "Page": {"Limit": 10000},
    }, "AudienceTargets")
    return {"group": group, "campaign": campaign, "list": lists[0],
            "segment": selected[0], "keywords": keywords, "targets": targets}


async def preflight(api: Any, plan: dict) -> dict:
    before = await snapshot(api, plan)
    if before["targets"]:
        raise ValueError("Group already has audience targets; no duplicate or replacement allowed")
    return {"status": "PASS", "before": before}


async def apply(api: Any, plan: dict, expected_preflight: dict | None) -> dict:
    checked = await preflight(api, plan)
    if expected_preflight is None or checked != expected_preflight:
        raise ValueError("Audience link snapshot changed after preview")
    link = plan["audience_link"]
    response = await api.call_v501("audiencetargets", "add", {"AudienceTargets": [{
        "AdGroupId": link["ad_group_id"], "RetargetingListId": link["retargeting_list_id"],
        "StrategyPriority": "NORMAL",
    }]}, client_login=plan["client_login"])
    actions = response.get("AddResults", [])
    complete = len(actions) == 1 and bool(actions[0].get("Id")) and not actions[0].get("Errors")
    return {**plan, "status": "complete" if complete else "partial", "executed": True,
            "preflight": checked, "added": {"audience_targets": actions},
            "activated": False}


async def readback(api: Any, plan: dict, before: dict, added: dict | None) -> dict:
    current = await snapshot(api, plan)
    targets = current.pop("targets")
    expected = {key: value for key, value in before.items() if key != "targets"}
    recorded = (added or {}).get("audience_targets", [])
    link = plan["audience_link"]
    verified = (
        current == expected and len(targets) == len(recorded) == 1
        and targets[0].get("Id") == recorded[0].get("Id")
        and targets[0].get("AdGroupId") == link["ad_group_id"]
        and targets[0].get("RetargetingListId") == link["retargeting_list_id"]
        and targets[0].get("State") == "ON"
        and targets[0].get("StrategyPriority") == "NORMAL"
    )
    return {"verified": verified, "target": targets, "campaign_state": "OFF"}
