"""Short-term network interests: typed plan, live catalog, guarded writes and readback."""

from __future__ import annotations

from typing import Any

from . import policy
from .identifiers import parse_id

AGE_BINS = [(0, 17, "AGE_0_17"), (18, 24, "AGE_18_24"), (25, 34, "AGE_25_34"),
            (35, 44, "AGE_35_44"), (45, 54, "AGE_45_54"), (55, None, "AGE_55")]


def age_modifiers(minimum: Any, maximum: Any = None) -> list[dict[str, Any]]:
    if type(minimum) is not int or minimum not in {row[0] for row in AGE_BINS}:
        raise ValueError("bundle.age_min: допустимы 0, 18, 25, 35, 45, 55")
    if maximum is not None and (type(maximum) is not int
                               or maximum not in {17, 24, 34, 44, 54}
                               or maximum < minimum):
        raise ValueError("bundle.age_max: 17, 24, 34, 44, 54 или null; не меньше age_min")
    excluded = [{"Age": code, "BidModifier": 0} for lower, upper, code in AGE_BINS
                if (upper is not None and upper < minimum)
                or (maximum is not None and lower > maximum)]
    return [{"DemographicsAdjustments": excluded}] if excluded else []


def compile_interest(source: dict[str, Any], channel: str) -> dict[str, Any] | None:
    if "retargeting_rules" in source:
        if (channel != "network" or source.get("audience_interest_ids") or source.get("keywords")
                or source.get("autotargeting") or source.get("offer_retargeting")):
            raise ValueError("Ретаргетинг по целям требует отдельную группу РСЯ")
        raw = source["retargeting_rules"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
            raise ValueError("retargeting_rules: от 1 до 10 правил")
        rules = []
        for rule in raw:
            if not isinstance(rule, dict) or set(rule) != {"operator", "goals"}:
                raise ValueError("retargeting_rules: обязательны operator и goals")
            if rule["operator"] not in {"ALL", "ANY", "NONE"}:
                raise ValueError("retargeting_rules.operator: ALL, ANY или NONE")
            if not isinstance(rule["goals"], list) or not 1 <= len(rule["goals"]) <= 10:
                raise ValueError("retargeting_rules.goals: от 1 до 10 целей")
            args = []
            for goal in rule["goals"]:
                if not isinstance(goal, dict) or set(goal) != {"goal_id", "days"}:
                    raise ValueError("Цель ретаргетинга: goal_id и days")
                days = goal["days"]
                if type(days) is not int or not 1 <= days <= 540:
                    raise ValueError("Период ретаргетинга: от 1 до 540 дней")
                args.append({"ExternalId": parse_id(goal["goal_id"]), "MembershipLifeSpan": days})
            rules.append({"Operator": rule["operator"], "Arguments": args})
        if all(r["Operator"] == "NONE" for r in rules):
            raise ValueError("Ретаргетинг требует хотя бы одно включающее правило ALL/ANY")
        priority = source.get("audience_priority", "NORMAL")
        if priority not in {"LOW", "NORMAL", "HIGH"}:
            raise ValueError("audience_priority: LOW, NORMAL или HIGH")
        return {"retargeting_list": {"Name": str(source["name"])[:250],
                                    "Type": "RETARGETING", "Rules": rules},
                "target": {"StrategyPriority": priority}}
    if "audience_interest_ids" not in source:
        if "audience_priority" in source:
            raise ValueError("audience_priority требует audience_interest_ids")
        return None
    ids = source["audience_interest_ids"]
    if isinstance(ids, list):
        ids = [parse_id(value, "audience_interest_ids") for value in ids]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 10
            or any(type(value) is not int or not str(value).startswith("10") for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("audience_interest_ids: 1–10 уникальных ID краткосрочных интересов")
    if channel != "network":
        raise ValueError("audience_interest_ids допустимы только в РСЯ")
    if source.get("keywords") or source.get("autotargeting") or source.get("offer_retargeting"):
        raise ValueError("Группы по интересам должны быть отдельными, без ключей и автотаргетинга")
    priority = source.get("audience_priority", "NORMAL")
    if priority not in {"LOW", "NORMAL", "HIGH"}:
        raise ValueError("audience_priority: LOW, NORMAL или HIGH")
    return {
        "retargeting_list": {
            "Name": str(source["name"])[:250], "Type": "AUDIENCE",
            "Rules": [{"Operator": "ANY", "Arguments": [{"ExternalId": value} for value in ids]}],
        },
        "target": {"StrategyPriority": priority},
    }


def planned(plan: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    return [(ci, gi, group["audience"]) for ci, campaign in enumerate(plan["campaigns"])
            for gi, group in enumerate(campaign["groups"]) if group.get("audience")]


async def preflight(api: Any, plan: dict[str, Any]) -> dict[str, Any]:
    specs = planned(plan)
    if not specs:
        return {"status": policy.PASS, "interests": []}
    retargeting = [
        spec for _, _, spec in specs if spec["retargeting_list"]["Type"] == "RETARGETING"
    ]
    checked_goals = []
    if retargeting:
        from . import goals
        campaign_id = plan.get("goal_catalog_campaign_id")
        if not campaign_id:
            raise ValueError("Ретаргетинг требует goal_catalog_campaign_id для проверки целей")
        ownership = await api.call_v501("campaigns", "get", {
            "SelectionCriteria": {"Ids": [campaign_id]}, "FieldNames": ["Id"]},
            client_login=plan["client_login"])
        if ownership.get("LimitedBy") is not None or not any(
            row.get("Id") == campaign_id for row in ownership.get("Campaigns", [])
        ):
            raise ValueError("Кампания каталога целей не принадлежит клиенту")
        catalog = await goals.read(api, campaign_id)
        available = {row["id"] for row in catalog["goals"]}
        wanted = {arg["ExternalId"] for spec in retargeting
                  for rule in spec["retargeting_list"]["Rules"] for arg in rule["Arguments"]}
        if wanted - available:
            raise ValueError("Цели ретаргетинга отсутствуют в проверенном каталоге клиента")
        checked_goals = sorted(wanted)
    interests = [spec for _, _, spec in specs if spec["retargeting_list"]["Type"] == "AUDIENCE"]
    if not interests:
        return {"status": policy.PASS, "interests": [], "retargeting_goal_ids": checked_goals}
    response = await api.call_v501(
        "dictionaries", "get", {"DictionaryNames": ["AudienceInterests"]},
        client_login=plan["client_login"],
    )
    if response.get("LimitedBy") is not None:
        raise ValueError("Справочник AudienceInterests усечён")
    catalog = {row["Id"]: row for row in response.get("AudienceInterests", [])}
    ids = {arg["ExternalId"] for spec in interests
           for arg in spec["retargeting_list"]["Rules"][0]["Arguments"]}
    if any(value not in catalog or catalog[value].get("InterestType") != "SHORT_TERM"
           for value in ids):
        raise ValueError(
            "audience_interest_ids отсутствуют в живом каталоге краткосрочных интересов"
        )
    return {"status": policy.PASS, "interests": [catalog[value] for value in sorted(ids)],
            "retargeting_goal_ids": checked_goals}


def _id(action: dict[str, Any]) -> int | None:
    value = action.get("Id")
    return value if type(value) is int and value > 0 and not action.get("Errors") else None


async def apply(api: Any, plan: dict[str, Any], campaigns: list[dict[str, Any]],
                add: Any) -> dict[str, Any]:
    specs = planned(plan)
    groups = {(campaign["plan_index"], group["plan_index"]): group
              for campaign in campaigns for group in campaign["groups"] if group.get("id")}
    rows = []
    selected = []
    for ci, gi, spec in specs:
        row = {"campaign_index": ci, "group_index": gi,
               "ad_group_id": groups.get((ci, gi), {}).get("id"),
               "retargeting_list": {}, "audience_target": {}}
        rows.append(row)
        if row["ad_group_id"]:
            selected.append((row, spec))
    result = {"rows": rows, "complete": False}
    try:
        actions = await add(
            api, "retargetinglists", "RetargetingLists",
            [spec["retargeting_list"] for _, spec in selected], plan["client_login"],
        )
    except Exception as exc:  # noqa: BLE001 - preserve partial IDs; never retry writes
        actions = getattr(exc, "results", [])
        result["error"] = str(exc)
    for (row, _), action in zip(selected, actions, strict=False):
        row["retargeting_list"] = action
    if "error" in result:
        return result
    targets = [(row, spec) for row, spec in selected if _id(row["retargeting_list"])]
    try:
        actions = await add(api, "audiencetargets", "AudienceTargets", [
            {**spec["target"], "AdGroupId": row["ad_group_id"],
             "RetargetingListId": _id(row["retargeting_list"])} for row, spec in targets
        ], plan["client_login"])
    except Exception as exc:  # noqa: BLE001 - preserve successful preceding batches
        actions = getattr(exc, "results", [])
        result["error"] = str(exc)
    for (row, _), action in zip(targets, actions, strict=False):
        row["audience_target"] = action
    result["complete"] = "error" not in result and all(
        _id(row["retargeting_list"]) and _id(row["audience_target"]) for row in rows
    )
    return result


async def readback(api: Any, plan: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    if not planned(plan):
        return {"verified": True, "rows": []}
    rows = (execution.get("audiences") or {}).get("rows", [])
    group_ids = [row["ad_group_id"] for row in rows if row.get("ad_group_id")]
    targets = []
    for offset in range(0, len(group_ids), 1000):
        response = await api.call_v501("audiencetargets", "get", {
            "SelectionCriteria": {"AdGroupIds": group_ids[offset:offset + 1000]},
            "FieldNames": ["Id", "AdGroupId", "RetargetingListId", "StrategyPriority", "State"],
            "Page": {"Limit": 10000},
        }, client_login=plan["client_login"])
        if response.get("LimitedBy") is not None:
            raise RuntimeError("audiencetargets.get усекает readback")
        targets.extend(response.get("AudienceTargets", []))
    list_ids = sorted({row["RetargetingListId"] for row in targets if row.get("RetargetingListId")})
    lists = []
    for offset in range(0, len(list_ids), 10000):
        response = await api.call_v501("retargetinglists", "get", {
            "SelectionCriteria": {"Ids": list_ids[offset:offset + 10000]},
            "FieldNames": ["Id", "Type", "Name", "Rules", "IsAvailable"],
            "Page": {"Limit": 10000},
        }, client_login=plan["client_login"])
        if response.get("LimitedBy") is not None:
            raise RuntimeError("retargetinglists.get усекает readback")
        lists.extend(response.get("RetargetingLists", []))
    # Interest-only groups must not acquire parallel keyword/autotarget criteria.
    keyword_groups = set()
    for offset in range(0, len(group_ids), 1000):
        response = await api.call_v501("keywords", "get", {
            "SelectionCriteria": {"AdGroupIds": group_ids[offset:offset + 1000]},
            "FieldNames": ["Id", "AdGroupId"], "Page": {"Limit": 10000},
        }, client_login=plan["client_login"])
        if response.get("LimitedBy") is not None:
            raise RuntimeError("keywords.get усекает проверку групп по интересам")
        keyword_groups.update(row["AdGroupId"] for row in response.get("Keywords", []))
    by_list = {row["Id"]: row for row in lists}
    checks = []
    by_position = {(row["campaign_index"], row["group_index"]): row for row in rows}
    for ci, gi, spec in planned(plan):
        row = by_position.get((ci, gi), {})
        group_id = row.get("ad_group_id")
        actual = [item for item in targets if item.get("AdGroupId") == group_id]
        target = actual[0] if len(actual) == 1 else {}
        actual_list = by_list.get(target.get("RetargetingListId"), {})
        expected_list = spec["retargeting_list"]
        # API may return MembershipLifeSpan=null for non-Metrika segments.
        rules = [{"Operator": rule.get("Operator"), "Arguments": [
            {**{"ExternalId": arg.get("ExternalId")},
             **({"MembershipLifeSpan": arg["MembershipLifeSpan"]}
                if arg.get("MembershipLifeSpan") is not None else {})}
            for arg in rule.get("Arguments", [])
        ]} for rule in actual_list.get("Rules", [])]
        matches = (
            len(actual) == 1
            and _id(row.get("audience_target", {})) == target.get("Id")
            and _id(row.get("retargeting_list", {})) == target.get("RetargetingListId")
            and target.get("StrategyPriority") == spec["target"]["StrategyPriority"]
            and target.get("State") == "ON"
            and actual_list.get("IsAvailable") == "YES"
            and actual_list.get("Type") == expected_list["Type"]
            and rules == expected_list["Rules"]
            and (
                expected_list["Type"] == "RETARGETING"
                or actual_list.get("Name") == expected_list["Name"]
            )
            and group_id not in keyword_groups
        )
        checks.append({"ad_group_id": group_id, "verified": matches,
                       "target": target, "retargeting_list": actual_list})
    return {"verified": all(row["verified"] for row in checks), "rows": checks}
