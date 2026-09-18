"""Read-only keyword and autotargeting inventory through Direct API v501."""

from __future__ import annotations

from typing import Any

from .identifiers import DirectId, parse_id

MAX_LIMIT = 10000
CATEGORY_FIELDS = ["Exact", "Narrow", "Alternative", "Accessory", "Broader"]
BRAND_FIELDS = [
    "WithoutBrands",
    "WithAdvertiserBrand",
    "WithCompetitorsBrand",
]


def _shape(item: dict[str, Any]) -> dict[str, Any]:
    settings = item.get("AutotargetingSettings") or {}
    return {
        "id": item.get("Id"),
        "keyword": item.get("Keyword"),
        "campaign_id": item.get("CampaignId"),
        "ad_group_id": item.get("AdGroupId"),
        "state": item.get("State"),
        "status": item.get("Status"),
        "serving_status": item.get("ServingStatus"),
        "strategy_priority": item.get("StrategyPriority"),
        "autotargeting_search_bid_is_auto": item.get(
            "AutotargetingSearchBidIsAuto"
        ),
        "autotargeting": {
            "categories": settings.get("Categories") or {},
            "brand_options": settings.get("BrandOptions") or {},
        }
        if item.get("Keyword") == "---autotargeting"
        else None,
        "user_param_1": item.get("UserParam1"),
        "user_param_2": item.get("UserParam2"),
    }


async def read(
    api: Any,
    client_login: str,
    *,
    campaign_ids: list[DirectId] | None = None,
    ad_group_ids: list[DirectId] | None = None,
    keyword_ids: list[DirectId] | None = None,
    limit: int = 10000,
) -> dict[str, Any]:
    """Read keywords including the current autotargeting categories and brands."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit должен быть от 1 до {MAX_LIMIT}")
    filters = {"campaign_ids": campaign_ids, "ad_group_ids": ad_group_ids,
               "keyword_ids": keyword_ids}
    for field, maximum in (("campaign_ids", 10), ("ad_group_ids", 1000),
                           ("keyword_ids", 10000)):
        values = list(dict.fromkeys(parse_id(v, field) for v in filters[field] or []))
        filters[field] = values or None
        if len(values) <= maximum:
            continue
        rows = []
        truncated = False
        for offset in range(0, len(values), maximum):
            if len(rows) >= limit:
                truncated = True
                break
            part = await read(api, client_login,
                              **{**filters, field: values[offset:offset + maximum]},
                              limit=limit - len(rows))
            rows.extend(part["keywords"])
            truncated = truncated or bool(part.get("truncated"))
        return {"client_login": client_login, "api_version": "v501", "keywords": rows,
                "count": len(rows), "truncated": truncated,
                "manual_count": sum(r["keyword"] != "---autotargeting" for r in rows),
                "autotargeting_count": sum(r["keyword"] == "---autotargeting" for r in rows)}
    criteria: dict[str, Any] = {}
    if campaign_ids:
        criteria["CampaignIds"] = filters["campaign_ids"]
    if ad_group_ids:
        criteria["AdGroupIds"] = filters["ad_group_ids"]
    if keyword_ids:
        criteria["Ids"] = filters["keyword_ids"]
    if not criteria:
        raise ValueError(
            "Нужен хотя бы один фильтр: campaign_ids, ad_group_ids или keyword_ids"
        )

    result = await api.call_v501(
        "keywords",
        "get",
        {
            "SelectionCriteria": criteria,
            "FieldNames": [
                "Id",
                "Keyword",
                "AdGroupId",
                "CampaignId",
                "State",
                "Status",
                "ServingStatus",
                "AutotargetingSearchBidIsAuto",
                "StrategyPriority",
                "UserParam1",
                "UserParam2",
            ],
            "AutotargetingSettingsCategoriesFieldNames": CATEGORY_FIELDS,
            "AutotargetingSettingsBrandOptionsFieldNames": BRAND_FIELDS,
            "Page": {"Limit": limit},
        },
        client_login=client_login,
    )
    rows = [_shape(item) for item in result.get("Keywords", [])]
    payload: dict[str, Any] = {
        "client_login": client_login,
        "api_version": "v501",
        "keywords": rows,
        "count": len(rows),
        "manual_count": sum(row["keyword"] != "---autotargeting" for row in rows),
        "autotargeting_count": sum(
            row["keyword"] == "---autotargeting" for row in rows
        ),
    }
    if result.get("LimitedBy") is not None:
        payload.update(
            {
                "truncated": True,
                "limited_by": result["LimitedBy"],
                "note": "Выдача Keywords.get усечена; сузьте фильтр или увеличьте limit.",
            }
        )
    return payload
