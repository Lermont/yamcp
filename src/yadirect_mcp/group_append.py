"""Append Search or Network groups through the repair guard.

This module never changes a campaign strategy/budget, moderates, or resumes ads.
All API mutations are called with the repair job's JournalAPI.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from . import (
    adgroups,
    ads,
    assets,
    bundle,
    completeness,
    creative,
    executor,
    keywords,
    link_checks,
    phrases,
    policy,
    preflight_refs,
    regions,
    semantics,
)
from .identifiers import parse_id

GROUP_FIELDS = {
    "campaign_id", "name", "region_ids", "ads", "keywords", "negative_keywords",
    "autotargeting", "sitelink_set_id", "semantic", "channel", "neuro_ad",
}
AD_FIELDS = {
    "hypothesis", "titles", "texts", "href", "display_url_path", "sitelink_set_id",
    "ad_extension_ids", "ad_image_hashes", "action_button",
}
VOLATILE = {"status", "serving_status", "autotargeting_search_bid_is_auto"}


def normalize(raw: Any, research: Any) -> tuple[list[dict], dict, list[dict]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 1000:
        raise ValueError("new_groups: требуется от 1 до 1000 групп")
    result, findings, campaigns = [], [], {}
    names, keys = set(), set()
    for index, value in enumerate(raw):
        prefix = f"new_groups[{index}]"
        if not isinstance(value, dict):
            raise ValueError(f"{prefix}: требуется объект")
        bundle._reject_unknown(value, GROUP_FIELDS, prefix)
        cid = parse_id(value.get("campaign_id"), prefix + ".campaign_id")
        if cid <= 0:
            raise ValueError(f"{prefix}.campaign_id должен быть положительным")
        channel = value.get("channel", "search")
        if channel not in {"search", "network"}:
            raise ValueError(f"{prefix}.channel: search либо network")
        if "region_ids" not in value or (channel == "search" and "autotargeting" not in value):
            raise ValueError(f"{prefix}: обязательны region_ids и для Поиска autotargeting")
        for ad in value.get("ads", []):
            if not isinstance(ad, dict):
                raise ValueError(f"{prefix}.ads: требуется объект")
            bundle._reject_unknown(ad, AD_FIELDS, prefix + ".ads")
        source = {k: v for k, v in value.items() if k not in {"campaign_id", "channel"}}
        if channel == "network" and source.get("autotargeting") is not None:
            bundle._autotargeting(source["autotargeting"], prefix)
            if not source["autotargeting"]:
                source["autotargeting"] = deepcopy(bundle.SAFE_AUTOTARGETING)
        group, creatives, criteria, checks, _ = bundle._group(
            source, channel=channel, default_region_ids=[], index=index,
        )
        geo = group["RegionIds"]
        if (not geo or len(geo) != len(set(geo)) or not any(i >= 0 for i in geo)
                or (0 in geo and len(geo) != 1)):
            raise ValueError(f"{prefix}.region_ids: недопустимая география")
        name = (cid, group["Name"].casefold())
        if name in names:
            raise ValueError("new_groups: дубликат имени группы в кампании")
        names.add(name)
        for row in criteria:
            if row["Keyword"] != semantics.AUTOTARGETING:
                key = (cid, semantics.phrase_key(row["Keyword"]))
                if key in keys:
                    raise ValueError("new_groups: дубликат ключа между группами кампании")
                keys.add(key)
        compiled = {"campaign_id": cid, "channel": channel,
                    "ad_group": group, "ads": creatives,
                    "keywords": criteria, "semantic": deepcopy(value.get("semantic")),
                    "action_buttons": [creative.action_button(a.get("action_button"), a.get("href"))
                                       for a in source["ads"]],
                    "ad_hypotheses": [a.get("hypothesis") for a in source["ads"]]}
        neuro = creative.neuro_ad(source, default_enabled=True)
        if neuro:
            compiled["neuro_ad"] = neuro
        result.append(compiled)
        findings.extend(checks)
        campaign = campaigns.setdefault(cid, {
            "channel": channel, "campaign": {"Name": str(cid)}, "groups": [],
            "group_count_reason": "Добавление групп к существующей кампании",
        })
        if campaign["channel"] != channel:
            raise ValueError("new_groups: одна кампания не может иметь разные channel")
        campaign["groups"].append(compiled)
    review, checks = semantics.review(
        research, list(campaigns.values()), today=datetime.now(UTC).date(),
    )
    findings.extend(checks)
    blocked = [r for r in findings if r["status"] == policy.BLOCK]
    if blocked:
        raise ValueError("new_groups BLOCK: " + "; ".join(r["message"] for r in blocked))
    return result, review, findings


def stable(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if k not in VOLATILE}
            for row in sorted(rows, key=lambda r: parse_id(r["id"]))]


def complete(payload: dict, key: str) -> list[dict]:
    rows = payload.get(key, [])
    ids = [parse_id(row["id"]) for row in rows]
    if completeness.sources(**{key: payload}) or len(ids) != len(set(ids)):
        raise ValueError(f"new_groups: неполная выборка или дубли {key}")
    return rows


async def inventory(api, login: str, ids: list[int]) -> dict:
    return {
        "groups": complete(await adgroups.read(api, login, campaign_ids=ids, limit=10000),
                           "groups"),
        "ads": complete(await ads.read(api, login, campaign_ids=ids, limit=10000,
                                       include_archived=True, preview_limit=None), "ads"),
        "keywords": complete(await keywords.read(api, login, campaign_ids=ids, limit=10000),
                             "keywords"),
    }


async def preflight(api, plan: dict, campaign_rows: list[dict]) -> dict:
    groups = plan.get("new_groups", [])
    if not groups:
        return {}
    login = plan["client_login"]
    ids = sorted({g["campaign_id"] for g in groups})
    selected = {parse_id(c["id"]): c for c in campaign_rows}
    for cid in ids:
        c = selected[cid]
        strategy = c.get("bidding_strategy") or {}
        search = strategy.get("Search") or {}
        network = strategy.get("Network") or {}
        channel = next(g.get("channel", "search") for g in groups if g["campaign_id"] == cid)
        active, inactive = (search, network) if channel == "search" else (network, search)
        placements = active.get("PlacementTypes") or {}
        placement = "SearchResults" if channel == "search" else "Network"
        allowed_states = {"OFF", "SUSPENDED"}
        if plan.get("append_drafts_only") is True:
            if any(plan.get(key) for key in ("campaigns", "ads", "autotargetings")):
                raise ValueError("append_drafts_only: разрешено только добавление черновиков")
            allowed_states.add("ON")
        if (c.get("state") not in allowed_states
                or c.get("type") != "UNIFIED_CAMPAIGN"
                or c.get("package_bidding_strategy")
                or active.get("BiddingStrategyType") not in {
                    "WB_MAXIMUM_CLICKS", "WB_MAXIMUM_CONVERSION_RATE"}
                or placements.get(placement) != "YES"
                or (channel == "network" and placements.get("Maps") != "NO")
                or inactive.get("BiddingStrategyType") != "SERVING_OFF"):
            raise ValueError("new_groups: требуется остановленная ЕПК выбранного channel "
                             "(для ON явно задайте append_drafts_only=true), "
                             "отдельный Поиск или РСЯ без пакетной стратегии")
    before = await inventory(api, login, ids)
    group_owners = {parse_id(g["id"]): parse_id(g["campaign_id"]) for g in before["groups"]}
    if (any(parse_id(r["campaign_id"]) not in ids
            for rows in before.values() for r in rows)
            or any(group_owners.get(parse_id(r["ad_group_id"])) != parse_id(r["campaign_id"])
                   for key in ("ads", "keywords") for r in before[key])):
        raise ValueError("new_groups: неверная принадлежность объектов кампании")
    names = {(parse_id(g["campaign_id"]), str(g["name"]).strip().casefold())
             for g in before["groups"]}
    keys = {(parse_id(k["campaign_id"]), semantics.phrase_key(k["keyword"]))
            for k in before["keywords"] if k.get("state") != "ARCHIVED"}
    counts = Counter(parse_id(g["campaign_id"]) for g in before["groups"])
    for g in groups:
        cid = g["campaign_id"]
        counts[cid] += 1
        if counts[cid] > policy.GROUPS_MAX_PER_CAMPAIGN:
            raise ValueError("new_groups: превышен лимит 1000 групп кампании")
        if (cid, g["ad_group"]["Name"].casefold()) in names:
            raise ValueError("new_groups: группа с таким именем уже существует")
        if any((cid, semantics.phrase_key(k["Keyword"])) in keys
               for k in g["keywords"] if k["Keyword"] != semantics.AUTOTARGETING):
            raise ValueError("new_groups: ключ уже существует в кампании")
    geo = sorted({abs(i) for g in groups for i in g["ad_group"]["RegionIds"] if i})
    if geo and (await regions.lookup(api, ids=geo, client_login=login)).get("unknown_ids"):
        raise ValueError("new_groups: неизвестные регионы")
    values = [a["ResponsiveAd"] for g in groups for a in g["ads"]]
    sets = await assets.read_sets(api, login, [v["SitelinkSetId"] for v in values])
    if any(not policy.SITELINKS_MINIMUM <= len(s["Sitelinks"]) <= policy.SITELINKS_MAXIMUM
           for s in sets.values()):
        raise ValueError("new_groups: требуется от 4 до 8 быстрых ссылок")
    await preflight_refs.assets(api, login, values)
    projected_groups, projected_ads = [], []
    for index, g in enumerate(groups, 1):
        # New IDs are not assigned yet; macros are checked with representative IDs.
        projected_groups.append({"id": index, "campaign_id": g["campaign_id"],
                                 "region_ids": g["ad_group"]["RegionIds"]})
        for v in g["ads"]:
            ad = v["ResponsiveAd"]
            projected_ads.append({"id": len(projected_ads) + 1,
                                  "campaign_id": g["campaign_id"], "ad_group_id": index,
                                  "href": ad.get("Href"),
                                  "sitelink_set_id": ad.get("SitelinkSetId")})
        for button in g.get("action_buttons", []):
            if button:
                projected_ads.append({"id": len(projected_ads) + 1,
                                      "campaign_id": g["campaign_id"], "ad_group_id": index,
                                      "href": button["href"]})
    pages = await link_checks.check_live(
        api, login, campaign_rows, projected_groups, projected_ads, sitelink_sets=sets,
    )
    if not link_checks.summary(pages)["all_ok"]:
        raise ValueError("new_groups: конечные URL с метками не прошли проверку")
    return {"inventory": before, "sitelinks": sets,
            "effective_link_checks": {**link_checks.summary(pages), "pages": pages}}


async def apply(api, plan: dict) -> dict:
    """Stop after the first failed batch; keep all returned IDs, never retry add."""
    result: dict[str, Any] = {"groups": [], "ads": [], "keywords": [], "status": "complete"}
    groups, login = plan["new_groups"], plan["client_login"]

    async def add(service, collection, rows, metadata):
        limit = executor.ADD_BATCH_LIMITS[service]
        for offset in range(0, len(rows), limit):
            part = rows[offset:offset + limit]
            try:
                response = await api.call_v501(service, "add", {collection: part},
                                              client_login=login)
                actions = response.get("AddResults", [])
                if not isinstance(actions, list):
                    raise ValueError(f"{service}.add: некорректный ответ")
                # Preserve even a malformed response, but do not bind ambiguous IDs.
                for i, action in enumerate(actions):
                    meta = metadata[offset + i] if i < len(part) else {}
                    result["groups" if service == "adgroups" else service].append(
                        {**meta, "result": action})
                valid = [parse_id(a["Id"]) for a in actions
                         if isinstance(a, dict) and a.get("Id") is not None]
                if (len(actions) != len(part) or len(valid) != len(part)
                        or len(set(valid)) != len(valid) or any(i <= 0 for i in valid)
                        or any(a.get("Errors") for a in actions)):
                    raise ValueError(f"{service}.add: частичный или неоднозначный результат")
            except Exception as exc:  # noqa: BLE001 - durable journal retains unknown outcomes
                result.update(status="partial", error=str(exc), failed_stage=service)
                return False
        return True

    if not await add("adgroups", "AdGroups",
                     [{**g["ad_group"], "CampaignId": g["campaign_id"]} for g in groups],
                     [{"group_index": i} for i in range(len(groups))]):
        return result
    ids = [parse_id(r["result"]["Id"]) for r in result["groups"]]
    for service, collection in (("ads", "Ads"), ("keywords", "Keywords")):
        rows, metadata = [], []
        for gi, g in enumerate(groups):
            for index, row in enumerate(g[service]):
                rows.append({**row, "AdGroupId": ids[gi]})
                metadata.append({"group_index": gi, "item_index": index})
        if not await add(service, collection, rows, metadata):
            return result
    return result


def manual_actions(groups: list[dict], execution: dict | None = None) -> list[dict]:
    """Keep UI work visible in previews and bind results only to journaled IDs."""
    group_ids, ad_ids = {}, {}
    if execution is not None:
        def bindings(key):
            rows = [r for r in execution.get(key, [])
                    if isinstance(r, dict) and isinstance(r.get("result"), dict)]
            ids = Counter(str(r.get("result", {}).get("Id")) for r in rows)
            positions = Counter((r.get("group_index"), r.get("item_index")) for r in rows)
            found = {}
            for row in rows:
                result = row.get("result") or {}
                gi, ai = row.get("group_index"), row.get("item_index")
                if (type(gi) is not int or not 0 <= gi < len(groups) or result.get("Errors")
                        or result.get("Id") is None or ids[str(result["Id"])] != 1
                        or positions[gi, ai] != 1):
                    continue
                if key == "ads" and (type(ai) is not int or not 0 <= ai < len(groups[gi]["ads"])):
                    continue
                try:
                    if parse_id(result["Id"]) <= 0:
                        continue
                except ValueError:
                    continue
                found[gi if key == "groups" else (gi, ai)] = str(result["Id"])
            return found
        group_ids, ad_ids = bindings("groups"), bindings("ads")
    actions = []
    for gi, group in enumerate(groups):
        if execution is not None and gi not in group_ids:
            continue
        target = {"campaign_id": str(group["campaign_id"]), "group_index": gi}
        if execution is not None:
            target["group_id"] = group_ids[gi]
        if group.get("neuro_ad"):
            actions.append(creative.neuro_requirement(group["neuro_ad"], **target))
        for ai, ad in enumerate(group["ads"]):
            if execution is not None and (gi, ai) not in ad_ids:
                continue
            ad_target = {**target, "ad_index": ai}
            if execution is not None:
                ad_target["ad_id"] = ad_ids[gi, ai]
            buttons = group.get("action_buttons", [None] * len(group["ads"]))
            if creative.needs_button(ad["ResponsiveAd"]) or buttons[ai]:
                actions.append(creative.button_requirement(buttons[ai], **ad_target))
            if group.get("channel") == "network":
                actions.append(policy.network_carousel_requirement(**ad_target))
    return actions


async def readback(api, plan: dict, execution: dict | None, before: dict) -> dict:
    """Bind expected payloads only to journaled add IDs; never guess by name."""
    groups = plan["new_groups"]
    ids = sorted({g["campaign_id"] for g in groups})
    actual = await inventory(api, plan["client_login"], ids)
    mismatches = []
    maps = {key: {parse_id(r["id"]): r for r in rows} for key, rows in actual.items()}
    expected_counts = {"groups": len(groups), "ads": sum(len(g["ads"]) for g in groups),
                       "keywords": sum(len(g["keywords"]) for g in groups)}
    execution = execution or {}
    group_ids = {}
    seen_ids = {}
    for key, count in expected_counts.items():
        records = execution.get(key, [])
        values = []
        if len(records) != count:
            mismatches.append({"service": key, "reason": "add_result_count"})
        seen_positions = set()
        for record in records:
            action = record.get("result") or {}
            if action.get("Errors") or action.get("Id") is None:
                mismatches.append({"service": key, "reason": "add_error_or_missing_id"})
                continue
            identifier = parse_id(action["Id"])
            values.append(identifier)
            gi, index = record.get("group_index"), record.get("item_index")
            position = (gi, index)
            if (type(gi) is not int or not 0 <= gi < len(groups)
                    or position in seen_positions):
                mismatches.append({"service": key, "reason": "invalid_result_binding"})
                continue
            seen_positions.add(position)
            row = maps[key].get(identifier)
            g = groups[gi]
            if row is None or parse_id(row["campaign_id"]) != g["campaign_id"]:
                mismatches.append({"service": key, "id": identifier, "reason": "missing_or_owner"})
                continue
            if key == "groups":
                group_ids[gi] = identifier
                want = g["ad_group"]
                if (row.get("name") != want["Name"]
                        or sorted(row.get("region_ids", [])) != sorted(want["RegionIds"])
                        or row.get("offer_retargeting") != "NO"
                        or not phrases.equivalent(
                            row.get("negative_keywords") or [],
                            want.get("NegativeKeywords", {}).get("Items", []))):
                    mismatches.append({"service": key, "id": identifier, "reason": "group_fields"})
            elif (type(index) is not int or not 0 <= index < len(g[key])
                  or parse_id(row["ad_group_id"]) != group_ids.get(gi)):
                mismatches.append({"service": key, "id": identifier, "reason": "group_binding"})
            elif key == "ads":
                want = g["ads"][index]["ResponsiveAd"]
                fields = {"Titles": "titles", "Texts": "texts", "Href": "href",
                          "DisplayUrlPath": "display_url_path", "SitelinkSetId": "sitelink_set_id",
                          "AdExtensionIds": "ad_extension_ids"}
                for source, target in fields.items():
                    expected, observed = want.get(source), row.get(target)
                    if source == "AdExtensionIds":
                        expected, observed = sorted(expected or []), sorted(observed or [])
                    if expected != observed:
                        mismatches.append({"service": key, "id": identifier, "reason": target})
                if "AdImageHashes" in want and sorted(want["AdImageHashes"]) != sorted(
                    row.get("ad_image_hashes") or []
                ):
                    mismatches.append({"service": key, "id": identifier,
                                       "reason": "ad_image_hashes"})
                if row.get("state") not in {"OFF", "SUSPENDED"}:
                    mismatches.append({"service": key, "id": identifier, "reason": "ad_not_off"})
                if plan.get("append_drafts_only") and row.get("status") != "DRAFT":
                    mismatches.append({"service": key, "id": identifier,
                                       "reason": "ad_not_draft"})
            else:
                want = g["keywords"][index]
                if (semantics.phrase_key(row.get("keyword") or "").replace("ё", "е")
                        != semantics.phrase_key(want["Keyword"]).replace("ё", "е")):
                    mismatches.append({"service": key, "id": identifier, "reason": "keyword"})
                for source, target in (("StrategyPriority", "strategy_priority"),
                                       ("UserParam1", "user_param_1"),
                                       ("UserParam2", "user_param_2")):
                    if source in want and row.get(target) != want[source]:
                        mismatches.append({"service": key, "id": identifier, "reason": target})
                if want["Keyword"] == semantics.AUTOTARGETING:
                    auto = want["AutotargetingSettings"]
                    if row.get("autotargeting") != {"categories": auto["Categories"],
                                                   "brand_options": auto["BrandOptions"]}:
                        mismatches.append({"service": key, "id": identifier,
                                           "reason": "autotargeting"})
        seen_ids[key] = set(values)
        if len(values) != len(set(values)):
            mismatches.append({"service": key, "reason": "duplicate_add_ids"})
    # Existing repair patches are verified by repair.readback, all other objects stay intact.
    ignored = {"groups": set(), "ads": {r["Id"] for r in plan["ads"]},
               "keywords": {r["Id"] for r in plan["autotargetings"]}}
    for key, rows in before.get("append_inventory", {}).items():
        old_ids = {parse_id(r["id"]) for r in rows}
        if old_ids & seen_ids[key]:
            mismatches.append({"service": key, "reason": "add_id_already_existed"})
        if set(maps[key]) != old_ids | seen_ids[key]:
            mismatches.append({"service": key, "reason": "unexpected_inventory_change"})
        for old in rows:
            identifier = parse_id(old["id"])
            if identifier in ignored[key]:
                continue
            new = maps[key].get(identifier)
            if new is None or stable([old]) != stable([new]):
                mismatches.append({"service": key, "id": identifier,
                                   "reason": "existing_object_changed"})
    if not before.get("append_inventory"):
        mismatches.append({"reason": "missing_append_before_snapshot"})
    return {"verified": not mismatches, "mismatches": mismatches,
            "counts": {key: len(value) for key, value in seen_ids.items()}}
