"""Feed inventory and guarded URL-feed creation; no automatic retries or launches."""

from __future__ import annotations

import hashlib
import json

from . import landing, products
from .identifiers import parse_id

FIELDS = [
    "Id",
    "Name",
    "BusinessType",
    "SourceType",
    "FilterSchema",
    "UpdatedAt",
    "NumberOfItems",
    "Status",
    "TitleAndTextSources",
]


async def read(api, login: str, ids: list | None = None) -> dict:
    requested = None if ids is None else {parse_id(i, "feed_id") for i in ids}
    if requested is not None and (not requested or len(requested) > 10000):
        raise ValueError("feed_ids: 1–10000 ID")
    rows, seen, offset = [], set(), 0
    while True:
        params = {
            "FieldNames": FIELDS,
            "UrlFeedFieldNames": ["Url", "RemoveUtmTags"],
            "Page": {"Limit": 1000, "Offset": offset},
        }
        if requested is not None:
            params["SelectionCriteria"] = {"Ids": sorted(requested)}
        result = await api.call_v501("feeds", "get", params, client_login=login)
        page = result.get("Feeds")
        if not isinstance(page, list):
            raise ValueError("feeds.get: нет полного списка Feeds")
        for row in page:
            identifier = parse_id(row.get("Id"), "feed.Id")
            if identifier in seen or requested is not None and identifier not in requested:
                raise ValueError("feeds.get: дубли или посторонние ID")
            seen.add(identifier)
            rows.append({**row, "Id": identifier})
        cursor = result.get("LimitedBy")
        if cursor is None:
            break
        if not page or type(cursor) is not int or cursor <= offset or cursor >= 10000:
            raise ValueError("feeds.get: усечённый ответ или неверная пагинация")
        offset = cursor
    if requested is not None and seen != requested:
        raise ValueError("Фид отсутствует, недоступен или принадлежит другому клиенту")
    return {"client_login": login, "feeds": rows, "complete": True}


def normalize(raw: dict, login: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"name", "url"}:
        raise ValueError("feed: name и url; источник website создаётся через интерфейс")
    if not isinstance(raw["name"], str) or not 1 <= len(raw["name"].strip()) <= 255:
        raise ValueError("feed.name: 1–255 символов")
    plan = {
        "schema": "direct_feed_v1",
        "client_login": login,
        "feed": {
            "Name": raw["name"].strip(),
            "BusinessType": "RETAIL",
            "SourceType": "URL",
            "UrlFeed": {"Url": products.url(raw["url"]), "RemoveUtmTags": "NO"},
        },
    }
    plan["summary"] = {
        "feed_name": plan["feed"]["Name"],
        "feed_url": plan["feed"]["UrlFeed"]["Url"],
        "source_type": "URL",
    }
    plan["plan_hash"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return plan


async def preflight(api, plan: dict) -> dict:
    await landing._public_target(plan["feed"]["UrlFeed"]["Url"])
    inventory = await read(api, plan["client_login"])
    duplicates = [
        r["Id"]
        for r in inventory["feeds"]
        if r.get("Name") == plan["feed"]["Name"]
        or (r.get("UrlFeed") or {}).get("Url") == plan["feed"]["UrlFeed"]["Url"]
    ]
    if duplicates:
        raise ValueError(f"Фид с таким именем или URL уже есть: {duplicates}; используйте его ID")
    if len(inventory["feeds"]) >= 50:
        raise ValueError("Достигнут лимит 50 фидов клиента")
    return {"status": "PASS", "existing_feeds": len(inventory["feeds"])}


async def apply(api, plan: dict) -> dict:
    await preflight(api, plan)
    response = await api.call_v501(
        "feeds", "add", {"Feeds": [plan["feed"]]}, client_login=plan["client_login"]
    )
    actions = response.get("AddResults")
    if not isinstance(actions, list) or len(actions) != 1:
        raise RuntimeError("feeds.add: неизвестный исход; повтор запрещён, проверьте журнал")
    action = actions[0]
    result = {
        "client_login": plan["client_login"],
        "plan_hash": plan["plan_hash"],
        "executed": True,
        "activated": False,
        "action": action,
        "status": "partial",
    }
    if action.get("Errors") or not action.get("Id"):
        return result
    identifier = parse_id(action["Id"], "feed_id")
    result["feed_id"] = identifier
    try:
        actual = (await read(api, plan["client_login"], [identifier]))["feeds"][0]
        expected = plan["feed"]
        verified = all(actual.get(k) == expected[k] for k in ("Name", "BusinessType", "SourceType"))
        verified = verified and all(
            (actual.get("UrlFeed") or {}).get(k) == v for k, v in expected["UrlFeed"].items()
        )
        result.update(
            status="complete" if verified else "readback_failed",
            verified=verified,
            feed=actual,
            ready_for_campaign=verified
            and actual.get("Status") == "DONE"
            and type(actual.get("NumberOfItems")) is int
            and actual["NumberOfItems"] > 0,
        )
    except Exception as exc:  # noqa: BLE001 — сохранить ID после успешной записи, без повтора
        result.update(status="readback_failed", verified=False, error=str(exc))
    return result
