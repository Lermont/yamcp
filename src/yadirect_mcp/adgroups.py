"""Read-only чтение групп объявлений через API v501."""

from __future__ import annotations

from typing import Any

MAX_LIMIT = 10000
MAX_AUTO_CAMPAIGNS = 50
CAMPAIGNS_PER_CALL = 10

FIELDS = [
    "Id",
    "Name",
    "CampaignId",
    "RegionIds",
    "RestrictedRegionIds",
    "NegativeKeywords",
    "NegativeKeywordSharedSetIds",
    "TrackingParams",
    "Status",
    "ServingStatus",
    "Type",
    "Subtype",
]


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.get("Items") or [])
    return list(value) if isinstance(value, list) else []


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


async def _campaign_ids(api: Any, client_login: str) -> tuple[list[int], int]:
    result = await api.call_v501(
        "campaigns",
        "get",
        {
            "SelectionCriteria": {
                "States": ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED"]
            },
            "FieldNames": ["Id"],
            "Page": {"Limit": MAX_LIMIT},
        },
        client_login=client_login,
    )
    ids = [int(row["Id"]) for row in result.get("Campaigns", []) if row.get("Id")]
    return ids[:MAX_AUTO_CAMPAIGNS], len(ids)


def _shape(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("Id"),
        "name": item.get("Name"),
        "campaign_id": item.get("CampaignId"),
        "type": item.get("Type"),
        "subtype": item.get("Subtype"),
        "status": item.get("Status"),
        "serving_status": item.get("ServingStatus"),
        "region_ids": item.get("RegionIds") or [],
        "restricted_region_ids": _items(item.get("RestrictedRegionIds")),
        "negative_keywords": _items(item.get("NegativeKeywords")),
        "negative_keyword_shared_set_ids": _items(
            item.get("NegativeKeywordSharedSetIds")
        ),
        "tracking_params": item.get("TrackingParams"),
        "offer_retargeting": (item.get("UnifiedAdGroup") or {}).get(
            "OfferRetargeting"
        ),
    }


async def read(
    api: Any,
    client_login: str,
    *,
    campaign_ids: list[int] | None = None,
    ad_group_ids: list[int] | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    """Прочитать группы, автоматически соблюдая лимит 10 CampaignIds."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit должен быть от 1 до {MAX_LIMIT}")
    if ad_group_ids and len(ad_group_ids) > MAX_LIMIT:
        raise ValueError(f"ad_group_ids принимает не более {MAX_LIMIT} идентификаторов")

    campaigns_total: int | None = None
    if ad_group_ids:
        selections = [{"Ids": [int(value) for value in ad_group_ids]}]
        selected_campaigns: list[int] = []
    else:
        if campaign_ids:
            selected_campaigns = [int(value) for value in campaign_ids]
        else:
            selected_campaigns, campaigns_total = await _campaign_ids(api, client_login)
        if not selected_campaigns:
            return {
                "client_login": client_login,
                "api_version": "v501",
                "groups": [],
                "count": 0,
            }
        selections = [
            {"CampaignIds": chunk}
            for chunk in _chunks(selected_campaigns, CAMPAIGNS_PER_CALL)
        ]

    raw: list[dict[str, Any]] = []
    limited_by: list[Any] = []
    for selection in selections:
        result = await api.call_v501(
            "adgroups",
            "get",
            {
                "SelectionCriteria": selection,
                "FieldNames": FIELDS,
                "UnifiedAdGroupFieldNames": ["OfferRetargeting"],
                "Page": {"Limit": limit},
            },
            client_login=client_login,
        )
        raw.extend(result.get("AdGroups", []))
        if result.get("LimitedBy") is not None:
            limited_by.append(result["LimitedBy"])

    payload: dict[str, Any] = {
        "client_login": client_login,
        "api_version": "v501",
        "groups": [_shape(item) for item in raw],
        "count": len(raw),
    }
    if campaigns_total is not None:
        payload["campaigns_total"] = campaigns_total
        payload["campaigns_scanned"] = len(selected_campaigns)
        if campaigns_total > len(selected_campaigns):
            payload["truncated"] = True
    if limited_by:
        payload["truncated"] = True
        payload["limited_by"] = limited_by
    if payload.get("truncated"):
        payload["note"] = "Выборка усечена; передайте campaign_ids/ad_group_ids явно."
    return payload
