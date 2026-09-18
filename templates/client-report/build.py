"""Build one portable, offline HTML report from JSON. Python 3.10+, stdlib only."""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def validate(data: dict) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    require(isinstance(data.get("demo"), bool), "demo must be explicitly true or false")
    require(isinstance(data.get("client"), dict), "client is required")
    for key in ("name", "description", "shortDescription"):
        require(
            isinstance(data["client"].get(key), str) and bool(data["client"][key]),
            f"client.{key} is required",
        )
    views = data.get("views", ["statistics", "setup"])
    require(
        isinstance(views, list)
        and bool(views)
        and len(set(views)) == len(views)
        and all(v in ("statistics", "setup") for v in views),
        "views: use statistics and/or setup",
    )
    campaigns = data.get("campaigns", [])
    require(isinstance(campaigns, list), "campaigns must be a list")
    ids = set()
    for c in campaigns:
        require(
            isinstance(c.get("id"), str) and c["id"] not in ids,
            "campaign id must be a unique string",
        )
        ids.add(c["id"])
        require(c.get("channel") in ("search", "network"), "supported channels: search, network")
        require(isinstance(c.get("name"), str) and bool(c["name"]), "campaign name is required")
        if "setup" in views:
            for key in ("status", "purpose"):
                require(isinstance(c.get(key), str), f"campaign.{key} is required")
            for key in ("groups", "ads"):
                require(
                    type(c.get(key)) is int and c[key] >= 0,
                    f"campaign.{key} must be a nonnegative integer",
                )
            for key in ("keywords", "negatives"):
                require(
                    isinstance(c.get(key), list) and all(isinstance(v, str) for v in c[key]),
                    f"campaign.{key} must be an array of strings",
                )
            require(isinstance(c.get("settings"), list), "campaign.settings is required")
            for f in c["settings"]:
                require(
                    isinstance(f.get("label"), str) and isinstance(f.get("value"), str),
                    "settings require label/value strings",
                )

    def parse_day(value):
        require(isinstance(value, str) and len(value) == 10, "dates must be YYYY-MM-DD")
        return date.fromisoformat(value)

    def span(p):
        require(parse_day(p["start"]) <= parse_day(p["end"]), "date range is reversed")
        require(
            (parse_day(p["end"]) - parse_day(p["start"])).days <= 365,
            "a report period may contain at most 366 days",
        )

    period_ids = set()
    require(isinstance(data.get("periods", []), list), "periods must be an array")
    if "statistics" in views:
        require(
            bool(data.get("periods")) or "setup" in views and isinstance(data.get("setup"), dict),
            "statistics requires a period or an initial setup report",
        )
    for p in data.get("periods", []):
        require(
            isinstance(p.get("id"), str) and p["id"] not in period_ids, "period id must be unique"
        )
        period_ids.add(p["id"])
        if "campaignIds" in p:
            require(
                isinstance(p["campaignIds"], list)
                and bool(p["campaignIds"])
                and len(set(p["campaignIds"])) == len(p["campaignIds"])
                and set(p["campaignIds"]) <= ids,
                "campaignIds must contain unique known campaign IDs",
            )
        span(p)
        for key in ("label", "source", "updated"):
            require(isinstance(p.get(key), str), f"period.{key} is required")
        if p.get("previous"):
            span(p["previous"])
            require(
                p["previous"]["end"] < p["start"], "comparison must end before the current period"
            )
        m = p.get("measurement", {})
        require(isinstance(m.get("available"), bool), "measurement.available must be true or false")
        if m.get("available"):
            require(
                bool(m.get("goal")) and bool(m.get("attribution")),
                "name the selected goal and attribution",
            )
            require(
                isinstance(m.get("comparable"), bool),
                "state whether goals are comparable across periods",
            )
        for cid, amount in p.get("budget", {}).items():
            require(cid in ids, "budget refers to an unknown campaign")
            require(
                amount is None
                or type(amount) in (int, float)
                and math.isfinite(amount)
                and amount >= 0,
                "budget must be nonnegative or null",
            )
        details = p.get("breakdowns")
        if details is not None:
            require(
                isinstance(details, dict)
                and details.get("schema") == "client_report_breakdowns_v1",
                "unsupported breakdowns schema",
            )
            require(
                details.get("start") == p["start"] and details.get("end") == p["end"],
                "breakdowns must match the period",
            )
            require(
                isinstance(details.get("collectedAt"), str), "breakdowns.collectedAt is required"
            )
            slices = details.get("slices")
            require(
                isinstance(slices, dict)
                and set(slices)
                == {
                    "keywords",
                    "queries",
                    "placements",
                    "groups",
                    "ads",
                    "regions",
                    "devices",
                    "demographics",
                },
                "all eight breakdowns are required",
            )
            channels = {c["id"]: c["channel"] for c in campaigns}
            for _key, part in slices.items():
                require(
                    isinstance(part, dict)
                    and part.get("status") == "collected"
                    and isinstance(part.get("rows"), list),
                    "invalid breakdown slice",
                )
                seen_details = set()
                for row in part["rows"]:
                    require(
                        row.get("campaign") in p.get("campaignIds", ids)
                        and row.get("channel") == channels.get(row.get("campaign")),
                        "breakdown campaign/channel mismatch",
                    )
                    dimensions = row.get("dimensions")
                    require(
                        isinstance(dimensions, dict)
                        and bool(dimensions)
                        and all(
                            isinstance(k, str) and isinstance(v, str) for k, v in dimensions.items()
                        ),
                        "breakdown dimensions and IDs must be strings",
                    )
                    identity = (row["campaign"], tuple(sorted(dimensions.items())))
                    require(identity not in seen_details, "duplicate breakdown row")
                    seen_details.add(identity)
                    for metric in ("spend", "impressions", "clicks", "conversions"):
                        value = row.get(metric)
                        require(
                            value is None
                            or type(value) in (int, float)
                            and math.isfinite(value)
                            and value >= 0,
                            "breakdown metrics must be nonnegative or null",
                        )
    seen = set()
    for r in data.get("daily", []):
        parse_day(r["date"])
        require(r.get("campaign") in ids, "daily row refers to an unknown campaign")
        key = (r["date"], r["campaign"])
        require(key not in seen, "duplicate date/campaign row")
        seen.add(key)
        for metric in ("spend", "impressions", "clicks", "conversions"):
            value = r.get(metric)
            require(
                value is None
                or type(value) in (int, float)
                and math.isfinite(value)
                and value >= 0,
                f"{metric} must be nonnegative or null",
            )
        if r.get("impressions") is not None and r.get("clicks") is not None:
            require(
                r["clicks"] <= r["impressions"], "clicks cannot exceed impressions in this template"
            )
    if "setup" in views:
        s = data.get("setup")
        require(isinstance(s, dict), "setup is required")
        parse_day(s["date"])
        for key in ("title", "status", "objective"):
            require(isinstance(s.get(key), str), f"setup.{key} is required")
        for key in ("summary", "nextStep"):
            require(
                isinstance(s.get(key), dict)
                and isinstance(s[key].get("title"), str)
                and isinstance(s[key].get("text"), str),
                f"setup.{key} requires title/text",
            )
        require(isinstance(s.get("facts"), list), "setup.facts is required")
        for f in s["facts"]:
            require(
                isinstance(f.get("label"), str) and isinstance(f.get("value"), str),
                "facts require label/value",
            )
        require(isinstance(s.get("checks"), list), "setup.checks is required")
        for c in s["checks"]:
            require(
                isinstance(c.get("complete"), bool), "each check must have complete: true/false"
            )
            for k in ("title", "text", "status"):
                require(isinstance(c.get(k), str), f"check.{k} is required")
        ad = s.get("ad", {})
        require(isinstance(ad.get("domain"), str), "ad.domain is required")
        for k in ("titles", "texts", "sitelinks"):
            require(
                isinstance(ad.get(k), list)
                and bool(ad[k])
                and all(isinstance(v, str) for v in ad[k]),
                f"ad.{k} needs at least one string",
            )


def data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def render(data: dict, view: str | None = None) -> str:
    """Render a model without modifying it or writing files."""
    data = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    if view:
        data["views"] = [view]
    data.setdefault("daily", [])
    data.setdefault("periods", [])
    validate(data)
    if data.get("views") == ["setup"]:
        data["periods"] = []
        data["daily"] = []
    elif data.get("views") == ["statistics"]:
        data.pop("setup", None)
        data["campaigns"] = [
            {key: c[key] for key in ("id", "name", "channel")} for c in data["campaigns"]
        ]
    css = (ROOT / "styles.css").read_text(encoding="utf-8")
    for weight in ("regular", "medium", "bold"):
        marker = f"__FONT_{weight.upper()}__"
        font_path = ROOT / "assets" / f"gilroy-{weight}.ttf"
        if font_path.is_file():
            css = css.replace(marker, data_uri(font_path, "font/ttf"))
        else:
            # Fonts are optional local assets, excluded from public distributions.
            # Remove the face entirely so the CSS system-font fallback stays offline.
            css = re.sub(r"@font-face\s*\{[^}]*" + re.escape(marker) + r"[^}]*\}\s*", "", css)
    # Escape script delimiters even in user-provided copy. Rendering uses text/HTML escaping.
    payload = (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    output_html = (ROOT / "template.html").read_text(encoding="utf-8")
    replacements = {
        "/*__STYLES__*/": css,
        "__LOGO__": data_uri(ROOT / "assets" / "logo.svg", "image/svg+xml"),
        "/*__SCRIPT__*/": (ROOT / "report.js").read_text(encoding="utf-8"),
        "/*__DATA__*/": payload,
    }
    # Payload is injected last so report text cannot be mistaken for a template marker.
    for marker, value in replacements.items():
        output_html = output_html.replace(marker, value)
    return output_html


def build(data_path: Path, output: Path, view: str | None = None) -> Path:
    output_html = render(json.loads(data_path.read_text(encoding="utf-8-sig")), view)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(output_html, encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path, nargs="?", default=ROOT / "example-data.json")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "index.html")
    parser.add_argument("--view", choices=("statistics", "setup"))
    args = parser.parse_args()
    try:
        path = build(args.data, args.out, args.view)
        print(f"Built: {path.resolve()} ({path.stat().st_size:,} bytes)")
    except (ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Invalid report data: {exc}\n")
