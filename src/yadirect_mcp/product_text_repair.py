"""Guarded replacement of one fallback text in existing product ads."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from . import ads, campaigns, completeness
from .identifiers import parse_id


def normalize(raw, login):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ValueError("product_ad_texts: 1-100 objects required")
    rows = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"id", "feed_id", "text"}:
            raise ValueError("product_ad_texts: id, feed_id, text required")
        ident, feed = parse_id(item["id"]), parse_id(item["feed_id"])
        text = item["text"]
        if ident <= 0 or feed <= 0:
            raise ValueError("Positive IDs required")
        if (not isinstance(text, str) or not text.strip() or text != text.strip()
                or len(text) > 81 or any(len(w) > 23 for w in text.split())
                or any(ord(c) < 32 for c in text)):
            raise ValueError("One nonempty text up to 81 characters required")
        rows.append({"id": ident, "feed_id": feed, "text": text})
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate ad IDs")
    plan = {"schema": "direct_campaign_repair_v1", "client_login": login,
            "api_version": "v501", "campaigns": [], "ads": [], "autotargetings": [],
            "product_ad_texts": rows, "activated": False}
    plan["plan_hash"] = hashlib.sha256(json.dumps(
        plan, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    plan["summary"] = {"campaigns": 0, "ads": len(rows), "autotargetings": 0}
    return plan


def exact(payload, key, ids):
    rows = payload.get(key, [])
    found = [parse_id(r["id"]) for r in rows]
    if (completeness.sources(**{key: payload}) or len(found) != len(set(found))
            or set(found) != set(ids)):
        raise ValueError("Incomplete, duplicate or missing " + key)
    return sorted(deepcopy(rows), key=lambda r: int(r["id"]))


async def snapshot(api, plan):
    requested, login = plan["product_ad_texts"], plan["client_login"]
    rows = exact(await ads.read(api, login, ad_ids=[r["id"] for r in requested],
                               limit=10000, preview_limit=None),
                 "ads", [r["id"] for r in requested])
    expected = {r["id"]: r for r in requested}
    for row in rows:
        if (row.get("type") not in {"SHOPPING_AD", "LISTING_AD"}
                or row.get("state") not in {"ON", "OFF", "SUSPENDED"}
                or parse_id(row.get("feed_id")) != expected[parse_id(row["id"])]["feed_id"]):
            raise ValueError("Owned product ad with expected feed required")
        row.pop("status", None)
        row.pop("feed_processing_status", None)
    cids = sorted({parse_id(r["campaign_id"]) for r in rows})
    cs = exact(await campaigns.read_settings(api, login, campaign_ids=cids, limit=10000),
               "campaigns", cids)
    for row in cs:
        if row.get("type") != "UNIFIED_CAMPAIGN" or row.get("state") == "ARCHIVED":
            raise ValueError("Owned nonarchived unified campaign required")
        row.pop("status", None)
    return {"campaigns": cs, "ads": rows}


async def preflight(api, plan):
    before = await snapshot(api, plan)
    requested = {r["id"]: r for r in plan["product_ad_texts"]}
    updates = [{"Id": parse_id(r["id"]),
                "ShoppingAd" if r["type"] == "SHOPPING_AD" else "ListingAd": {
                    "DefaultTexts": [requested[parse_id(r["id"])]["text"]]}}
               for r in before["ads"]]
    return {"status": "PASS", "before": before, "updates": updates,
            "client_login": plan["client_login"], "plan_hash": plan["plan_hash"]}


async def apply(api, plan, expected_preflight):
    checked = await preflight(api, plan)
    if checked != expected_preflight:
        raise ValueError("Product ad snapshot changed after preview")
    response = await api.call_v501("ads", "update", {"Ads": checked["updates"]},
                                  client_login=plan["client_login"])
    rows = response.get("UpdateResults", [])
    found = [parse_id(r["Id"]) for r in rows if r.get("Id") is not None]
    complete = (len(rows) == len(found) == len(checked["updates"])
                and set(found) == {r["Id"] for r in checked["updates"]}
                and not any(r.get("Errors") for r in rows))
    return {**plan, "status": "complete" if complete else "partial", "executed": True,
            "preflight": checked, "updates": {"ads": rows}, "activated": False}


async def readback(api, plan, before):
    current, expected = await snapshot(api, plan), deepcopy(before)
    texts = {r["id"]: r["text"] for r in plan["product_ad_texts"]}
    for row in expected["ads"]:
        row["texts"] = [texts[parse_id(row["id"])]]
        row["text"] = texts[parse_id(row["id"])]
    verified = current == expected
    return {"verified": verified, "mismatches": [] if verified else [
        {"reason": "product_text_or_preserved_fields"}],
        "counts": {"ads": len(current["ads"])}}
