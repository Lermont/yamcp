"""Effective advertising URLs, including inherited tracking and shared sitelinks.

Checks use public GET, no browser cookies, JavaScript, or paid ad clicks.
Concrete dynamic values are representative; they do not simulate every query,
region or placement. Exact live IDs and campaign names are used when available.
"""
from __future__ import annotations

from typing import Any

from . import assets, landing, policy, products, tracking

MAX_URLS_PER_DEVICE = 1000
URLS_PER_BATCH = 150


def _values(campaign: dict, group: dict, ad: dict, device: str) -> dict[str, str]:
    strategy = (campaign.get("bidding_strategy") or {}).get("Search") or {}
    search = strategy.get("BiddingStrategyType", "SERVING_OFF") != "SERVING_OFF"
    name = str(campaign.get("name") or "Тестовая кампания")
    return {
        "campaign_id": str(campaign.get("id") or 1), "campaign_name": name,
        "campaign_name_lat": "Test_Campaign", "campaign_type": "type1",
        "ad_id": str(ad.get("id") or 1), "banner_id": str(ad.get("id") or 1),
        "gbid": str(group.get("id") or 1), "keyword": "тестовый запрос",
        "phrase_id": "1", "retargeting_id": "1", "adtarget_id": "1",
        "creative_id": "1", "coef_goal_context_id": "1",
        "source_type": "search" if search else "context",
        "source": "none" if search else "example.com",
        "position_type": "premium" if search else "none", "position": "1" if search else "0",
        "device_type": device, "region_id": str(next(iter(group.get("region_ids") or []), 213)),
        "region_name": "Москва", "yclid": "1234567890123456789",
        "match_type": "rm", "matched_keyword": "тестовый запрос",
    }


def inventory(
    campaigns: list[dict], groups: list[dict], ads: list[dict], sitelink_sets: dict[int, dict],
) -> list[dict[str, Any]]:
    """Retain the full URL inventory; never discard missing assets or unknown macros."""
    by_campaign = {row["id"]: row for row in campaigns}
    by_group = {row["id"]: row for row in groups}
    pages: dict[tuple, dict] = {}
    for ad in ads:
        campaign = by_campaign.get(ad.get("campaign_id"), {})
        group = by_group.get(ad.get("ad_group_id"), {})
        links = [("main", ad["href"])] if ad.get("href") else []
        links.extend(("product_sample", u) for u in ad.get("product_urls", []))
        if (ad.get("action_button") or {}).get("href"):
            links.append(("button", ad["action_button"]["href"]))
        problems = []
        if not campaign or not group:
            problems.append("Не прочитаны настройки кампании или группы")
        set_id = ad.get("sitelink_set_id")
        if set_id is not None:
            value = sitelink_sets.get(set_id)
            if value is None:
                problems.append(f"Набор быстрых ссылок {set_id} не прочитан")
            else:
                for row in value["Sitelinks"]:
                    if not row.get("Href"):
                        problems.append(f"В наборе {set_id} отсутствует Href")
                    else:
                        links.append(("sitelink", row["Href"]))
        if ad.get("turbo_page"):
            problems.append("Турбо-страница требует отдельной проверки конечного URL")
        for problem in set(problems):
            pages[(str(ad.get("id")), problem)] = {
                "url": ad.get("href"), "base_url": ad.get("href"),
                "campaigns": [ad.get("campaign_id")], "ad_groups": [ad.get("ad_group_id")],
                "ad_ids": [ad.get("id")], "effective_url_check": True,
                "site_check": {"ok": False, "checked": False, "error": problem},
            }
        for kind, href in links:
            for device in ("desktop", "mobile"):
                try:
                    rendered = tracking.render_url(
                        href, campaign.get("tracking_params"), group.get("tracking_params"),
                        ad_tracking=ad.get("tracking_params"),
                        values=_values(campaign, group, ad, device),
                    )
                    error = ("Не подставлены параметры: " + ", ".join(rendered["unknown_macros"])
                             if rendered["unknown_macros"] else None)
                except (ValueError, TypeError) as exc:
                    rendered, error = {"url": href}, str(exc)
                key = (rendered["url"], device)
                page = pages.setdefault(key, {
                    "url": rendered["url"], "base_url": href, "device": device,
                    "campaigns": [], "ad_groups": [], "ad_ids": [], "link_kinds": [],
                    "effective_url_check": True,
                    "dynamic_values": "representative_with_live_ids_when_available",
                })
                for field, value in (("campaigns", ad.get("campaign_id")),
                                     ("ad_groups", ad.get("ad_group_id")),
                                     ("ad_ids", ad.get("id")), ("link_kinds", kind)):
                    if value not in page[field]:
                        page[field].append(value)
                if error:
                    page["site_check"] = {"url": rendered["url"], "ok": False,
                                          "checked": False, "error": error}
    return list(pages.values())


async def check_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound network work; keep every skipped URL explicitly unverified/BLOCK."""
    for device, ua in (("desktop", landing.DESKTOP_USER_AGENT),
                       ("mobile", landing.MOBILE_USER_AGENT)):
        candidates = [p for p in pages if p.get("device") == device and not p.get("site_check")]
        selected = candidates[:MAX_URLS_PER_DEVICE]
        results = []
        for offset in range(0, len(selected), URLS_PER_BATCH):
            results.extend(await landing.inspect_pages(
                selected[offset:offset + URLS_PER_BATCH],
                max_pages=URLS_PER_BATCH, user_agent=ua,
            ))
        by_url = {row["url"]: row for row in results}
        for page in candidates:
            page["site_check"] = by_url.get(page["url"], {
                "url": page["url"], "ok": False, "checked": False,
                "error": "URL не проверен: лимит или неполный результат HTTP-проверки",
            })
    return pages


def summary(pages: list[dict]) -> dict[str, Any]:
    failures = [p for p in pages if not (p.get("site_check") or {}).get("ok")]
    return {
        "status": policy.BLOCK if failures else policy.PASS,
        "effective_urls": len(pages), "failed_or_unchecked": len(failures),
        "all_ok": not failures, "devices": ["desktop", "mobile"],
        "scope": sorted({kind for page in pages for kind in page.get("link_kinds", [])}),
        "dynamic_values": "representative_with_live_ids_when_available",
    }


async def check_live(
    api: Any, client_login: str, campaigns: list[dict], groups: list[dict], ads: list[dict],
    *, sitelink_sets: dict[int, dict] | None = None,
) -> list[dict[str, Any]]:
    if sitelink_sets is None:
        try:
            sitelink_sets = await assets.read_sets(
                api, client_login, [a["sitelink_set_id"] for a in ads if a.get("sitelink_set_id")],
            )
        except (ValueError, RuntimeError):
            sitelink_sets = {}  # inventory records each missing reference as BLOCK
    return await check_pages(inventory(campaigns, groups, ads, sitelink_sets))


async def check_plan(api: Any, plan: dict[str, Any]) -> dict[str, Any]:
    campaigns, groups, ads = [], [], []
    for ci, item in enumerate(plan["campaigns"], 1):
        raw = item["campaign"]
        typed = raw["UnifiedCampaign"]
        campaigns.append({"id": ci, "name": raw["Name"],
                          "tracking_params": typed.get("TrackingParams"),
                          "bidding_strategy": typed.get("BiddingStrategy")})
        for group in item["groups"]:
            gid = len(groups) + 1
            groups.append({"id": gid, "campaign_id": ci,
                           "region_ids": group["ad_group"].get("RegionIds", [])})
            for ai, ad in enumerate(group["ads"]):
                value = products.payload(ad)
                ads.append({"id": len(ads) + 1, "campaign_id": ci, "ad_group_id": gid,
                            "href": value.get("Href"),
                            "product_urls": item.get("product_source", {}).get("sample_urls", []),
                            "action_button": group["action_buttons"][ai],
                            "sitelink_set_id": value.get("SitelinkSetId")})
    ids = [a["sitelink_set_id"] for a in ads if a.get("sitelink_set_id")]
    sets = await assets.read_sets(api, plan["client_login"], ids)
    for row in sets.values():
        if not policy.SITELINKS_MINIMUM <= len(row["Sitelinks"]) <= policy.SITELINKS_MAXIMUM:
            raise ValueError(f"Набор быстрых ссылок {row['Id']}: требуется от 4 до 8 ссылок")
    token = landing.CACHE_SCOPE.set(plan["client_login"] + ":" + plan["plan_hash"])
    try:
        pages = await check_pages(inventory(campaigns, groups, ads, sets))
    finally:
        landing.CACHE_SCOPE.reset(token)
    report = summary(pages)
    report["pages"] = pages
    report["synthetic_object_ids"] = True
    report["sitelinks"] = [{"sitelink_set_id": sid, "count": len(row["Sitelinks"])}
                          for sid, row in sets.items()]
    return report
