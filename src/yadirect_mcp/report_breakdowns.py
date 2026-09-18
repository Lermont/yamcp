"""Full-period client report slices. Read-only API requests, no top-N truncation."""

from __future__ import annotations

import asyncio
import csv
import io
import math
from datetime import UTC, datetime
from pathlib import Path

from . import ads, reports, store

# Dimensions are attributes, so rows partition the campaign/channel totals.
SLICES = {
    "keywords": ("Ключевые фразы и условия показа", "CRITERIA_PERFORMANCE_REPORT",
                 ["AdGroupId", "AdGroupName", "CriterionId", "CriterionType", "Criterion"]),
    "queries": ("Поисковые запросы", "SEARCH_QUERY_PERFORMANCE_REPORT",
                ["AdGroupId", "AdGroupName", "Query"]),
    "placements": ("Площадки РСЯ", "CUSTOM_REPORT", ["Placement"]),
    "groups": ("Группы объявлений", "ADGROUP_PERFORMANCE_REPORT", ["AdGroupId", "AdGroupName"]),
    "ads": ("Объявления", "AD_PERFORMANCE_REPORT", ["AdGroupId", "AdGroupName", "AdId"]),
    "regions": ("Регионы", "CUSTOM_REPORT", ["LocationOfPresenceId", "LocationOfPresenceName"]),
    "devices": ("Устройства", "CUSTOM_REPORT", ["Device"]),
    "demographics": ("Пол и возраст", "CUSTOM_REPORT", ["Gender", "Age"]),
}
METRICS = {"spend": "Cost", "impressions": "Impressions", "clicks": "Clicks"}
CHANNELS = {"SEARCH": "search", "AD_NETWORK": "network"}


def number(value: str, integer: bool = False) -> float | int | None:
    if value in store.EMPTY:
        return None
    parsed = float(value.replace(",", "."))
    if not math.isfinite(parsed) or parsed < 0 or integer and not parsed.is_integer():
        raise ValueError("Некорректное число в детальной статистике")
    return int(parsed) if integer else parsed


def parse(tsv: str, config: dict, key: str, dimensions: list[str]) -> list[dict]:
    reader = csv.DictReader(io.StringIO(tsv.lstrip("\ufeff")), delimiter="\t")
    measurement = config["measurement"]
    metrics = dict(METRICS)
    if measurement:
        metrics["conversions"] = (
            f"Conversions_{measurement['goal_id']}_{measurement['attribution_model']}"
        )
    required = {"CampaignId", *dimensions, *metrics.values()}
    if key != "queries":
        required.add("AdNetworkType")
    if not required <= set(reader.fieldnames or []):
        raise ValueError(f"Колонки среза {key} не соответствуют запросу: {sorted(required)}")
    mapping = {(str(c["id"]), c["channel"]): c["report_id"] for c in config["campaigns"]}
    output, seen = [], set()
    for raw in reader:
        if None in raw or any(raw[f] is None for f in required):
            raise ValueError(f"Повреждённая строка среза {key}")
        channel = "search" if key == "queries" else CHANNELS.get(raw["AdNetworkType"])
        if channel is None:
            raise ValueError(f"Неизвестный канал среза {key}")
        campaign = mapping.get((raw["CampaignId"], channel))
        # A mixed campaign may be registered for just one channel.
        if campaign is None:
            if raw["CampaignId"] not in {cid for cid, _ in mapping}:
                raise ValueError(f"Неизвестная кампания среза {key}")
            continue
        if key == "placements" and channel != "network":
            raise ValueError("В выгрузку площадок РСЯ попал другой канал")
        identity = (campaign, *(raw[f] for f in dimensions))
        if identity in seen:
            raise ValueError(f"Дубликат строки среза {key}")
        seen.add(identity)
        row = {"campaign": campaign, "channel": channel,
               "dimensions": {f: raw[f] for f in dimensions}, "conversions": None}
        row.update({m: number(raw[f], m in ("impressions", "clicks")) for m, f in metrics.items()})
        output.append(row)
    return sorted(output, key=lambda r: (r["campaign"], tuple(r["dimensions"].values())))


def reconcile(rows: list[dict], controls: list[dict], key: str) -> list[dict]:
    """Infer missing goal zeros only when a full partition matches a measured total."""
    checks = []
    for control in controls:
        if key == "queries" and control["channel"] != "search":
            continue
        if key == "placements" and control["channel"] != "network":
            continue
        selected = [r for r in rows if r["campaign"] == control["campaign"]]
        traffic_matches = all(
            control[m] is not None and all(r[m] is not None for r in selected)
            and sum(r[m] for r in selected) == control[m]
            for m in ("clicks", "impressions")
        )
        inferred = 0
        known = sum(r["conversions"] for r in selected if r["conversions"] is not None)
        if (key != "queries" and traffic_matches and control["conversions"] is not None
                and math.isclose(known, control["conversions"], abs_tol=0.00001, rel_tol=0)):
            for row in selected:
                if row["conversions"] is None:
                    row["conversions"] = 0
                    inferred += 1
        checks.append({"campaign": control["campaign"], "trafficMatches": traffic_matches,
                       "inferredGoalZeros": inferred, "reference": {
                           m: control[m] for m in (*METRICS, "conversions")}})
    return checks


async def _request(api, config: dict, start: str, end: str, key: str,
                   report_type: str, dimensions: list[str]) -> str:
    measurement = config["measurement"]
    fields = ["CampaignId", *dimensions]
    if key != "queries":
        fields.append("AdNetworkType")
    fields += list(METRICS.values()) + (["Conversions"] if measurement else [])
    filters = [{"Field": "CampaignId", "Operator": "IN",
                "Values": sorted({str(c["id"]) for c in config["campaigns"]})}]
    if key == "placements":
        filters.append({"Field": "AdNetworkType", "Operator": "EQUALS", "Values": ["AD_NETWORK"]})
    contract = reports.normalize(
        fields=fields, report_type=report_type, filters=filters, order_by=None, limit=None,
        goals=[measurement["goal_id"]] if measurement else None,
        attribution_models=[measurement["attribution_model"]] if measurement else None,
    )
    spec = {"SelectionCriteria": {"DateFrom": start, "DateTo": end, "Filter": filters},
            "FieldNames": contract["fields"], "ReportType": report_type,
            "DateRangeType": "CUSTOM_DATE", "Format": "TSV", "IncludeVAT": "YES",
            "IncludeDiscount": "YES" if config.get("include_discount", False) else "NO"}
    if measurement:
        spec.update(Goals=contract["goals"], AttributionModels=contract["attribution_models"])
    await asyncio.sleep(0.55)
    return await api.report(spec, client_login=config["client_login"])


async def collect(api, config: dict, start: str, end: str, raw_dir: Path) -> dict:
    await asyncio.to_thread(raw_dir.mkdir, parents=True, exist_ok=False)
    tsv = await _request(api, config, start, end, "totals", "CUSTOM_REPORT", [])
    await asyncio.to_thread((raw_dir / "totals.tsv").write_text, tsv, encoding="utf-8")
    controls = parse(tsv, config, "totals", [])
    result = {"schema": "client_report_breakdowns_v1", "start": start, "end": end,
              "collectedAt": datetime.now(UTC).isoformat(), "slices": {}}
    for key, (title, report_type, dimensions) in SLICES.items():
        tsv = await _request(api, config, start, end, key, report_type, dimensions)
        await asyncio.to_thread((raw_dir / f"{key}.tsv").write_text, tsv, encoding="utf-8")
        rows = parse(tsv, config, key, dimensions)
        result["slices"][key] = {
            "title": title, "status": "collected", "reportType": report_type,
            "scope": "search" if key == "queries" else "network" if key == "placements" else "all",
            "reconciliation": reconcile(rows, controls, key), "rows": rows,
        }
    ad_rows = result["slices"]["ads"]["rows"]
    ad_ids = sorted({int(r["dimensions"]["AdId"]) for r in ad_rows})
    copy = {}
    for offset in range(0, len(ad_ids), 1000):
        payload = await api.call_v501("ads", "get", {
            "SelectionCriteria": {"Ids": ad_ids[offset:offset + 1000]},
            "FieldNames": ["Id"], "TextAdFieldNames": ["Title", "Title2", "Text"],
            "ResponsiveAdFieldNames": ["Titles", "Texts"], "Page": {"Limit": 10000},
        }, client_login=config["client_login"])
        if payload.get("LimitedBy") is not None:
            raise ValueError("Тексты объявлений выгружены не полностью")
        for ad in payload.get("Ads", []):
            shaped = ads._shape(ad)
            copy[str(ad["Id"])] = {"title": shaped.get("title") or "",
                                   "text": shaped.get("text") or ""}
    result["slices"]["ads"]["currentCopy"] = copy
    return result


def summary(details: dict | None) -> dict:
    """Bound the LLM brief independently of the number of queries or placements."""
    if not details:
        return {"status": "not_collected"}
    return {
        key: {"row_count": len(part["rows"]), "scope": part["scope"],
              "traffic_matches": all(c["trafficMatches"] for c in part["reconciliation"]),
              "top_by_clicks": [
                  {"campaign": r["campaign"], "dimensions": {
                      k: v[:120] for k, v in r["dimensions"].items()},
                   **{m: r[m] for m in (*METRICS, "conversions")}}
                  for r in sorted(part["rows"], key=lambda r: r["clicks"] or 0, reverse=True)[:2]
              ]}
        for key, part in details["slices"].items()
    }
