"""Explicit, guarded resumption of a bounded set of existing ads."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from . import ads, campaigns
from .identifiers import parse_id


def normalize(bundle: dict, login: str) -> dict:
    required = {"targets", "launch_authorized", "allow_pending_moderation"}
    if not isinstance(bundle, dict) or set(bundle) != required:
        raise ValueError("Нужны только targets, launch_authorized, allow_pending_moderation")
    if bundle["launch_authorized"] is not True:
        raise ValueError("Нужна отдельная явная команда пользователя на запуск")
    if not isinstance(bundle["allow_pending_moderation"], bool):
        raise ValueError("allow_pending_moderation должен быть boolean")
    rows = bundle["targets"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("targets: от 1 до 100 объявлений")
    targets = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"ad_id", "group_id", "campaign_id"}:
            raise ValueError("Каждый target требует ad_id, group_id, campaign_id")
        targets.append({key: parse_id(value, key) for key, value in row.items()})
    if len({t["ad_id"] for t in targets}) != len(targets):
        raise ValueError("Повтор ad_id")
    plan = {"schema": "direct_ad_resume_v1", "client_login": login,
            "targets": sorted(targets, key=lambda x: x["ad_id"]),
            "launch_authorized": True,
            "allow_pending_moderation": bundle["allow_pending_moderation"],
            "summary": {"ads": len(targets), "action": "resume",
                        "warning": "Показы могут начаться после допуска модерацией"}}
    plan["plan_hash"] = hashlib.sha256(json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return plan


async def snapshot(api, plan: dict) -> dict:
    ids = sorted({t["campaign_id"] for t in plan["targets"]})
    cs = await campaigns.read_settings(api, plan["client_login"], campaign_ids=ids,
                                       include_archived=True, limit=1000)
    aa = await ads.read(api, plan["client_login"], campaign_ids=ids,
                        include_archived=True, limit=10000, preview_limit=None)
    if cs.get("truncated") or aa.get("truncated") or aa.get("ads_truncated"):
        raise ValueError("Неполный снимок блокирует запуск")
    result = {"campaigns": sorted(cs["campaigns"], key=lambda x: x["id"]),
              "ads": sorted(aa["ads"], key=lambda x: x["id"])}
    for key in result:
        if len({r["id"] for r in result[key]}) != len(result[key]):
            raise ValueError("Дубли в снимке")
    if {r["id"] for r in result["campaigns"]} != set(ids):
        raise ValueError("Кампании недоступны либо принадлежат другому логину")
    return result


async def preflight(api, plan: dict) -> dict:
    before = await snapshot(api, plan)
    by_id = {a["id"]: a for a in before["ads"]}
    for campaign in before["campaigns"]:
        if campaign["state"] != "ON" or campaign["status"] != "ACCEPTED":
            raise ValueError("Родительская кампания должна быть ON/ACCEPTED")
    allowed = {"ACCEPTED", "PREACCEPTED"}
    if plan["allow_pending_moderation"]:
        allowed.add("MODERATION")
    for target in plan["targets"]:
        ad = by_id.get(target["ad_id"])
        if not ad or ad["campaign_id"] != target["campaign_id"] or (
            ad["ad_group_id"] != target["group_id"]
        ):
            raise ValueError("Принадлежность объявления не подтверждена")
        if ad["state"] != "SUSPENDED" or ad["status"] not in allowed:
            raise ValueError(f"Объявление {ad['id']} недоступно для включения: "
                             f"{ad['state']}/{ad['status']}")
    return before


def verify(before: dict, after: dict, target_ids: set[int]) -> dict:
    expected, actual = deepcopy(before), deepcopy(after)
    states = {a["id"]: {"state": a["state"], "status": a["status"]}
              for a in after["ads"] if a["id"] in target_ids}
    # Moderation can finish independently; never conceal state or content changes.
    for snapshot_value in (expected, actual):
        for ad in snapshot_value["ads"]:
            ad.pop("status", None)
            if snapshot_value is expected and ad["id"] in target_ids:
                ad["state"] = "ON"
    return {"verified": expected == actual and len(states) == len(target_ids),
            "target_states": states,
            "campaigns_unchanged": before["campaigns"] == after["campaigns"]}


async def apply(api, plan: dict, before: dict, journal) -> dict:
    current = await preflight(api, plan)
    if current != before:
        raise ValueError("Кабинет изменился после preview; требуется новый preview")
    ids = [t["ad_id"] for t in plan["targets"]]
    response = await api.call_v501("ads", "resume", {"SelectionCriteria": {"Ids": ids}},
                                   client_login=plan["client_login"])
    rows = response.get("ResumeResults")
    valid = (isinstance(rows, list) and len(rows) == len(ids)
             and all(isinstance(r, dict) for r in rows))
    successes = [r.get("Id") for r in (rows if isinstance(rows, list) else [])
                 if isinstance(r, dict) and not r.get("Errors")]
    if not valid or len(successes) != len(set(successes)) or not set(successes) <= set(ids):
        journal.payload["uncertain"] = True
        journal.event(stage="invalid_resume_response", result=response)
    result = {**plan, "executed": True, "status": "partial", "response": response}
    journal.checkpoint(result)
    try:
        after = await snapshot(api, plan)
        result["readback"] = verify(before, after, set(ids))
        result["after"] = after
    except Exception as exc:  # noqa: BLE001 - preserve the completed request
        result["readback"] = {"verified": False, "error": str(exc)}
    if valid and set(successes) == set(ids) and not journal.payload["uncertain"]:
        result["status"] = "complete" if result["readback"]["verified"] else "complete_unverified"
    return result
