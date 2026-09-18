"""Client report acceptance: numbers, history, stale analysis, failure recovery."""

from __future__ import annotations

import json
import locale
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx

from test_report_context import campaign
from test_reports import settings
from yadirect_mcp import report_pipeline as pipeline
from yadirect_mcp.client import DirectClient
from yadirect_mcp.report_runner import refresh_all

ROOT = Path(__file__).parent
HEADER = "Date\tCampaignId\tAdNetworkType\tImpressions\tClicks\tCost\tConversions_77_AUTO\n"
TSV = HEADER + "2026-08-01\t1\tSEARCH\t100\t10\t120.25\t2.5\n"


@pytest.fixture(autouse=True)
def isolated_breakdown_collector(monkeypatch):
    # These tests exercise the daily refresh transaction; full slice integration
    # and fail-closed behavior are exercised in test_report_breakdowns.py.
    async def collect(api, config, start, end, raw_dir):
        from yadirect_mcp.report_breakdowns import SLICES
        return {"schema": "client_report_breakdowns_v1", "start": start, "end": end,
                "collectedAt": "2026-08-03T00:00:00+00:00", "slices": {
                    key: {"status": "collected", "scope": "all", "rows": [],
                          "reconciliation": []} for key in SLICES}}
    monkeypatch.setattr(pipeline.report_breakdowns, "collect", collect)


def register(tmp_path, login="client", measurement=True, history=False, two_channels=False):
    model = json.loads((ROOT / "templates/client-report/example-data.json").read_text("utf-8"))
    model.update(demo=False, clientLogin=login)
    model["campaigns"] = (
        [model["campaigns"][0], model["campaigns"][2]] if two_channels else model["campaigns"][:1]
    )
    ids = {c["id"] for c in model["campaigns"]}
    model["periods"] = [p for p in model["periods"] if history and p["id"] == "2026-07"]
    for p in model["periods"]:
        p["budget"] = {key: value for key, value in p["budget"].items() if key in ids}
    model["daily"] = [
        r for r in model["daily"] if history and r["date"] < "2026-08-01" and r["campaign"] in ids
    ]
    config = {
        "start_date": "2026-08-01",
        "days": 14,
        "currency": "RUB",
        "campaigns": [{"id": 1, "report_id": model["campaigns"][0]["id"], "channel": "search"}],
        "measurement": {"goal_id": "77", "goal": "Отправка формы", "attribution_model": "AUTO"}
        if measurement
        else None,
    }
    source = tmp_path / f"{login}-seed.json"
    if two_channels:
        config["campaigns"].append(
            {"id": 1, "report_id": model["campaigns"][1]["id"], "channel": "network"}
        )
    source.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    cfg = replace(settings(tmp_path), allowed_logins=frozenset())
    result = pipeline.initialize(cfg, login, str(source), config)
    return cfg, result, model, config


class API:
    def __init__(self, tsv=TSV, goal=77, fail=False):
        self.tsv, self.goal, self.fail = tsv, goal, fail
        self.calls = []

    async def call_v501(self, service, method, params, **kwargs):
        self.calls.append((service, method, deepcopy(params)))
        return {
            "Campaigns": [campaign(cid, self.goal) for cid in params["SelectionCriteria"]["Ids"]]
        }

    async def report(self, spec, **kwargs):
        self.calls.append(("reports", deepcopy(spec)))
        if self.fail:
            raise RuntimeError("API temporarily unavailable")
        return self.tsv


@pytest.mark.asyncio
async def test_refresh_preserves_setup_and_builds_weighted_totals(tmp_path):
    cfg, initial, model, _ = register(tmp_path)
    api = API()
    response = await pipeline.refresh(cfg, api, "client", as_of="2026-08-03")
    brief = response["brief"]
    assert brief["totals"]["spend"] == 120.25
    assert brief["totals"]["cpc"] == 12.025
    assert brief["totals"]["cpa"] == 48.1
    assert brief["recent_comparison"]["change_percent"]["spend"] == -100
    assert brief["recent_comparison"]["change_percent"]["cpc"] is None
    assert len(api.calls) == 2
    spec = api.calls[-1][1]
    assert spec["IncludeVAT"] == "YES" and spec["Goals"] == ["77"]
    assert "Page" not in spec and spec["SelectionCriteria"]["DateTo"] == "2026-08-02"
    state = pipeline._load(tmp_path / "client")
    assert state["model"]["setup"] == model["setup"]
    assert state["model"]["campaigns"] == model["campaigns"]
    assert state["model"]["daily"][1]["clicks"] == 0
    assert (
        tmp_path / f"client/.client-report/revisions/{initial['revision']}/index.html"
    ).is_file()
    assert brief["needs_insight"] is True
    assert response["llm_calls"] == 0 and response["published"] is False
    assert len(json.dumps(brief, ensure_ascii=False)) < pipeline.MAX_BRIEF_CHARS


@pytest.mark.asyncio
async def test_retry_cached_then_backfill_invalidates_old_insight(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    first = await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02")
    brief = first["brief"]
    saved = pipeline.write_insight(
        cfg,
        "client",
        brief["period_id"],
        brief["data_revision"],
        {"title": "Первый результат", "text": "Получено 10 кликов."},
    )
    assert saved["brief"]["needs_insight"] is False
    cached_api = API()
    cached = await pipeline.refresh(cfg, cached_api, "client", as_of="2026-08-02")
    assert cached["status"] == "cached" and cached_api.calls == []
    same = await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02", force=True)
    assert same["brief"]["needs_insight"] is False
    new = await pipeline.refresh(
        cfg, API(TSV.replace("120.25", "240.50")), "client", as_of="2026-08-02", force=True
    )
    assert new["brief"]["needs_insight"] is True
    assert new["brief"]["previous_insight"][0]["title"] == "Первый результат"
    with pytest.raises(ValueError, match="изменилась"):
        pipeline.write_insight(
            cfg,
            "client",
            brief["period_id"],
            brief["data_revision"],
            {"title": "Старый", "text": "Старый вывод"},
        )
    assert len(pipeline._load(tmp_path / "client")["model"]["daily"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api",
    [
        API(fail=True),
        API(goal=88),
        API(HEADER + "bad\trow\n"),
        API(TSV + TSV.split("\n")[1] + "\n"),
        API(TSV.replace("SEARCH", "AD_NETWORK")),
        API(TSV.replace("120.25", "NaN")),
    ],
)
async def test_failures_keep_last_report_and_revision(tmp_path, api):
    cfg, _, _, _ = register(tmp_path)
    await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02")
    folder = tmp_path / "client"
    before = (folder / "index.html").read_bytes()
    revision = pipeline._load(folder)["revision"]
    with pytest.raises((ValueError, RuntimeError)):
        await pipeline.refresh(cfg, api, "client", as_of="2026-08-03")
    assert (folder / "index.html").read_bytes() == before
    assert pipeline._load(folder)["revision"] == revision
    assert not (folder / ".client-report/refresh.lock").exists()


@pytest.mark.asyncio
async def test_unknown_values_not_zero_and_traffic_mode(tmp_path):
    cfg, _, _, _ = register(tmp_path, measurement=False)
    header = HEADER.replace("\tConversions_77_AUTO", "")
    api = API(header + "2026-08-01\t1\tSEARCH\t100\t10\t--\n")
    result = await pipeline.refresh(cfg, api, "client", as_of="2026-08-03")
    assert "Goals" not in api.calls[-1][1]
    assert result["brief"]["totals"]["spend"] is None
    assert result["brief"]["totals"]["conversions"] is None
    assert result["brief"]["totals"]["cpc"] is None
    assert result["brief"]["totals"]["clicks"] == 10


@pytest.mark.asyncio
async def test_final_day_then_stop_and_no_future_queries(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    early = API()
    assert (await pipeline.refresh(cfg, early, "client", as_of="2026-08-01"))["status"] == "not_due"
    assert early.calls == []
    end = await pipeline.refresh(cfg, API(), "client", as_of="2026-08-15")
    assert end["brief"]["end"] == "2026-08-14"
    later = API()
    assert (await pipeline.refresh(cfg, later, "client", as_of="2026-08-20"))["status"] == "cached"
    assert later.calls == []
    with pytest.raises(ValueError, match="будущую"):
        await pipeline.refresh(cfg, API(), "client", as_of="2999-01-01")


@pytest.mark.asyncio
async def test_successful_empty_response_is_zero_but_empty_document_is_error(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    zero = await pipeline.refresh(cfg, API(HEADER), "client", as_of="2026-08-02")
    assert zero["brief"]["totals"]["clicks"] == 0
    assert zero["brief"]["totals"]["cpa"] is None
    with pytest.raises(ValueError, match="Колонки"):
        await pipeline.refresh(cfg, API(""), "client", as_of="2026-08-03")


def test_identity_existing_report_and_path_protection(tmp_path):
    cfg, _, model, config = register(tmp_path)
    source = tmp_path / "client-seed.json"
    with pytest.raises(ValueError, match="уже существует"):
        pipeline.initialize(cfg, "client", str(source), config)
    with pytest.raises(ValueError, match="clientLogin"):
        pipeline.initialize(cfg, "another", str(source), config)
    with pytest.raises(ValueError, match="client_login"):
        pipeline.read_brief(cfg, "../../secret")
    model["demo"] = True
    source.write_text(json.dumps(model), encoding="utf-8")
    with pytest.raises(ValueError, match="demo=false"):
        pipeline.initialize(cfg, "client", str(source), config)


@pytest.mark.asyncio
async def test_lock_and_export_recovery(tmp_path):
    cfg, initial, _, _ = register(tmp_path)
    folder = tmp_path / "client"
    with pipeline._lock(folder), pytest.raises(ValueError, match="уже обновляется"):
        await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02")
    done = await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02")
    (folder / "index.html").write_text("interrupted export", encoding="utf-8")
    await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02")
    assert (folder / "index.html").read_text("utf-8").startswith("<!doctype html>")
    assert pipeline._load(folder)["revision"] == done["revision"] != initial["revision"]


@pytest.mark.asyncio
async def test_batch_isolates_client_errors(tmp_path):
    cfg, _, _, _ = register(tmp_path, "client-a")
    register(tmp_path, "client-b")
    with pipeline._lock(tmp_path / "client-a"):
        outcome = await refresh_all(cfg, API(), as_of="2026-08-02")
    assert outcome["status"] == "partial"
    assert [r["status"] for r in outcome["clients"]] == ["failed", "updated"]
    assert all("brief" not in row for row in outcome["clients"])


def test_render_escapes_insight_text_and_does_not_mutate_model(tmp_path):
    _, _, model, _ = register(tmp_path)
    snapshot = deepcopy(model)
    html = pipeline._renderer().render(model)
    assert model == snapshot
    assert "SergeyMushtuk" in html and "+79099994402" in html


@pytest.mark.asyncio
async def test_both_channels_and_old_period_are_preserved(tmp_path):
    cfg, _, old_model, _ = register(tmp_path, history=True, two_channels=True)
    api = API(TSV + "2026-08-01\t1\tAD_NETWORK\t200\t20\t200.00\t1\n")
    response = await pipeline.refresh(cfg, api, "client", as_of="2026-08-02")
    model = pipeline._load(tmp_path / "client")["model"]
    assert model["periods"][1] == old_model["periods"][0]
    assert [r for r in model["daily"] if r["date"] < "2026-08-01"] == sorted(
        old_model["daily"],
        key=lambda r: (r["date"], r["campaign"]),
    )
    assert response["brief"]["totals"]["spend"] == 320.25
    assert response["brief"]["totals"]["cpc"] == 10.675
    assert len(response["brief"]["by_channel"]) == 2
    assert api.calls[0][2]["SelectionCriteria"]["Ids"] == [1]


@pytest.mark.asyncio
async def test_http_client_contract_and_actual_units_tracking(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:
        settings_route = router.post("https://api.direct.yandex.com/json/v501/campaigns").mock(
            return_value=httpx.Response(
                200, json={"result": {"Campaigns": [campaign()]}}, headers={"Units": "11/989/1000"}
            )
        )
        report_route = router.post("https://api.direct.yandex.com/json/v501/reports").mock(
            return_value=httpx.Response(200, text=TSV)
        )
        async with DirectClient(cfg) as api:
            response = await pipeline.refresh(cfg, api, "client", as_of="2026-08-02")
    assert settings_route.call_count == report_route.call_count == 1
    assert report_route.calls[0].request.headers["returnMoneyInMicros"] == "false"
    assert report_route.calls[0].request.headers["Client-Login"] == "client"
    assert response["units_usage"]["spent"] == 11
    assert response["units_usage"]["requests_with_units"] == 1


@pytest.mark.asyncio
async def test_insight_is_escaped_in_html(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    ready = (await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02"))["brief"]
    payload = "</script><script>alert('x')</script>"
    pipeline.write_insight(
        cfg,
        "client",
        ready["period_id"],
        ready["data_revision"],
        {"title": "Пример", "text": payload},
    )
    html = (tmp_path / "client/index.html").read_text("utf-8")
    assert payload not in html and "\\u003c/script>" in html


def test_report_renders_without_optional_fonts(tmp_path, monkeypatch):
    renderer = pipeline._renderer()
    template = tmp_path / "template"
    (template / "assets").mkdir(parents=True)
    for name in ("styles.css", "template.html", "report.js", "assets/logo.svg"):
        (template / name).write_bytes((renderer.ROOT / name).read_bytes())
    monkeypatch.setattr(renderer, "ROOT", template)
    model = json.loads((ROOT / "templates/client-report/example-data.json").read_text("utf-8"))
    html = renderer.render(model)
    assert html.startswith("<!doctype html>")
    assert "__FONT_" not in html and "@font-face" not in html
    assert "data:font/ttf" not in html
    assert "Arial,sans-serif" in html
    assert "data:image/svg+xml;base64," in html
    assert model["client"]["name"] in html


@pytest.mark.asyncio
async def test_refresh_timestamp_works_in_c_locale(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    previous = locale.setlocale(locale.LC_TIME)
    try:
        locale.setlocale(locale.LC_TIME, "C")
        outcome = await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02")
    finally:
        locale.setlocale(locale.LC_TIME, previous)
    assert outcome["status"] == "updated"
    model = pipeline._load(tmp_path / "client")["model"]
    assert model["periods"][0]["updated"].endswith(" · Москва")
