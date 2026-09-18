"""Тесты на то, что ломается у всех: поллинг офлайн-отчёта и пересчёт итогов."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from yadirect_mcp import config, reports, store
from yadirect_mcp.client import DirectClient, DirectError
from yadirect_mcp.config import Settings

REPORTS = "https://api.direct.yandex.com/json/v501/reports"

TSV = (
    "Date\tCampaignName\tImpressions\tClicks\tCost\tCtr\n"
    "2026-07-01\tПоиск | Москва\t1000\t50\t2500.00\t5.00\n"
    "2026-07-02\tПоиск | Москва\t3000\t50\t2500.00\t1.67\n"
    "2026-07-03\tРСЯ\t6000\t100\t1000.00\t1.67\n"
    "2026-07-04\tРСЯ\t--\t--\t--\t--\n"
)


def settings(tmp_path) -> Settings:
    return Settings(
        token="t0k3n",
        agency_login="agency",
        allowed_logins=frozenset({"good-client"}),
        out_dir=tmp_path,
        sandbox=False,
        max_inflight=4,
        inline_rows=2,
        report_deadline=30.0,
        lang="ru",
    )


SPEC = {
    "SelectionCriteria": {"DateFrom": "2026-07-01", "DateTo": "2026-07-04"},
    "FieldNames": ["Date", "CampaignName", "Impressions", "Clicks", "Cost", "Ctr"],
    "ReportType": "CUSTOM_REPORT",
    "DateRangeType": "CUSTOM_DATE",
    "Format": "TSV",
    "IncludeVAT": "YES",
    "IncludeDiscount": "NO",
}


@pytest.mark.asyncio
@respx.mock
async def test_offline_report_polls_until_ready(tmp_path, monkeypatch):
    """201 → 202 → 200. Тела у 201/202 пустые: кто смотрит на resp.ok, вернёт ''."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    route = respx.post(REPORTS).mock(
        side_effect=[
            httpx.Response(201, headers={"retryIn": "2", "reportsInQueue": "1"}),
            httpx.Response(202, headers={"retryIn": "1", "reportsInQueue": "1"}),
            httpx.Response(
                200,
                content=TSV.encode("utf-8"),
                headers={"Units": "12/23695/64000"},
            ),
        ]
    )
    async with DirectClient(settings(tmp_path)) as api:
        tsv = await api.report(SPEC, client_login="good-client")

    assert route.call_count == 3
    assert tsv.startswith("Date\tCampaignName")
    assert api.last_units.rest == 23695
    assert api.reports_in_queue == 1


@pytest.mark.asyncio
@respx.mock
async def test_v501_units_are_journaled_per_request_and_aggregated(tmp_path):
    route = respx.post(
        "https://api.direct.yandex.com/json/v501/campaigns"
    ).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"result": {"Campaigns": []}},
                headers={
                    "Units": "11/989/1000",
                    "Units-Used-Login": "good-client",
                    "RequestId": "req-1",
                },
            ),
            httpx.Response(
                200,
                json={"result": {"Campaigns": []}},
                headers={
                    "Units": "13/976/1000",
                    "Units-Used-Login": "good-client",
                    "RequestId": "req-2",
                },
            ),
        ]
    )
    async with DirectClient(settings(tmp_path)) as api:
        mark = api.units_mark()
        await api.call_v501("campaigns", "get", {}, client_login="good-client")
        await api.call_v501("campaigns", "get", {}, client_login="good-client")
        usage = api.units_since(mark)

    assert route.call_count == 2
    assert usage["requests_with_units"] == 2
    assert usage["spent"] == 24
    assert usage["rest"] == 976
    assert usage["daily"] == 1000
    assert usage["truncated"] is False
    assert usage["by_operation"] == [{
        "api_version": "v501",
        "service": "campaigns",
        "method": "get",
        "requests": 2,
        "spent": 24,
    }]
    assert [row["request_id"] for row in usage["requests"]] == ["req-1", "req-2"]
    assert all(row["units_used_login"] == "good-client" for row in usage["requests"])


@pytest.mark.asyncio
async def test_units_scopes_do_not_mix_parallel_tasks(tmp_path):
    async with DirectClient(settings(tmp_path)) as api:
        async def worker(spent):
            mark = api.units_mark()
            await _no_sleep(0)
            api._note_units(
                httpx.Response(200, headers={"Units": f"{spent}/900/1000"}),
                api_version="v501",
                service="campaigns",
                method="get",
                client_login="good-client",
            )
            await _no_sleep(0)
            return api.units_since(mark)

        first, second = await __import__("asyncio").gather(worker(11), worker(13))

    assert first["spent"] == 11
    assert second["spent"] == 13
    assert first["requests_with_units"] == second["requests_with_units"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_report_name_stable_across_polls(tmp_path, monkeypatch):
    """Все попытки должны нести ОДНО имя, иначе каждый поллинг ставит новый отчёт."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    respx.post(REPORTS).mock(
        side_effect=[
            httpx.Response(201, headers={"retryIn": "1"}),
            httpx.Response(200, content=TSV.encode("utf-8")),
        ]
    )
    async with DirectClient(settings(tmp_path)) as api:
        await api.report(SPEC, client_login="good-client")

    names = {
        json.loads(c.request.content)["params"]["ReportName"]
        for c in respx.calls
    }
    assert len(names) == 1


@pytest.mark.asyncio
@respx.mock
async def test_headers_and_client_login(tmp_path):
    respx.post(REPORTS).mock(return_value=httpx.Response(200, content=TSV.encode()))
    async with DirectClient(settings(tmp_path)) as api:
        await api.report(SPEC, client_login="good-client")

    h = respx.calls[0].request.headers
    assert h["Client-Login"] == "good-client"
    assert h["Authorization"] == "Bearer t0k3n"
    assert h["processingMode"] == "auto"
    assert h["returnMoneyInMicros"] == "false"
    assert h["skipReportHeader"] == "true"
    assert "skipColumnHeader" not in h  # имена колонок нам нужны


@pytest.mark.asyncio
@respx.mock
async def test_400_surfaces_error_code(tmp_path):
    respx.post(REPORTS).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"error_code": 8000, "error_string": "Ошибка в параметрах",
                            "error_detail": "Поле Keyword нельзя выводить",
                            "request_id": "42"}},
        )
    )
    async with DirectClient(settings(tmp_path)) as api:
        with pytest.raises(DirectError) as e:
            await api.report(SPEC, client_login="good-client")
    assert e.value.code == 8000
    assert "Keyword" in str(e.value)


def test_totals_recomputed_not_summed(tmp_path):
    out = store.persist(TSV, out_dir=tmp_path, stem="x", inline_rows=2)
    t = out["totals"]
    assert out["rows"] == 4
    assert t["Impressions"] == 10000
    assert t["Clicks"] == 200
    assert t["Cost"] == 6000.0
    # Наивная сумма колонки Ctr дала бы 8.34. Правильный CTR = 200/10000.
    assert t["Ctr"] == 2.0
    assert t["AvgCpc"] == 30.0
    assert out["preview_truncated"] is True
    assert len(out["preview"]) == 2


TSV_GOALS = (
    "Date\tImpressions\tClicks\tCost\tConversions_12345_LSC\tRevenue_12345_LSC\n"
    "2026-07-01\t1000\t50\t2500.00\t5\t50000\n"
    "2026-07-02\t3000\t50\t2500.00\t3\t30000\n"
)


def test_totals_include_goal_suffixed_columns(tmp_path):
    """С Goals Директ отдаёт Conversions_<цель>_<атрибуция>, а не Conversions."""
    t = store.persist(TSV_GOALS, out_dir=tmp_path, stem="g", inline_rows=5)["totals"]
    assert t["Conversions_12345_LSC"] == 8
    assert t["Revenue_12345_LSC"] == 80000
    # Производные пересчитываются на каждую цель, а не суммируются по строкам.
    assert t["ConversionRate_12345_LSC"] == 8.0
    assert t["CostPerConversion_12345_LSC"] == 625.0


def test_persist_keeps_report_when_row_is_ragged(tmp_path):
    """Лишний таб в тексте не должен стоить целой выгрузки: она уже оплачена."""
    out = store.persist(
        "Date\tName\tClicks\n2026-07-01\tA\tB\t50\n",
        out_dir=tmp_path,
        stem="ragged",
        inline_rows=5,
    )
    saved = Path(out["path"])
    assert saved.exists()
    assert "строка 2" in out["parse_error"]


def test_persist_confines_report_to_out_dir(tmp_path):
    """client_login приходит от модели и попадает в имя файла."""
    out_dir = (tmp_path / "reports").resolve()
    out_dir.mkdir()
    out = store.persist(
        "A\n1\n", out_dir=out_dir, stem=r"..\..\pwned_2026-07-01", inline_rows=1
    )
    assert Path(out["path"]).resolve().is_relative_to(out_dir)


def test_persist_writes_bytes_as_received(tmp_path):
    """Файл обрабатывают сторонним кодом — он должен совпадать с ответом API."""
    out = store.persist(TSV, out_dir=tmp_path, stem="raw", inline_rows=1)
    assert Path(out["path"]).read_bytes() == TSV.encode("utf-8")


@pytest.mark.asyncio
@respx.mock
async def test_report_survives_broken_retry_in_header(tmp_path, monkeypatch):
    """int() на мусорном заголовке ронял поллинг вместе с заказанным отчётом."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    respx.post(REPORTS).mock(
        side_effect=[
            httpx.Response(201, headers={"retryIn": "soon"}),
            httpx.Response(200, content=TSV.encode("utf-8")),
        ]
    )
    async with DirectClient(settings(tmp_path)) as api:
        assert await api.report(SPEC, client_login="good-client")


@pytest.mark.asyncio
@respx.mock
async def test_report_retries_gateway_errors(tmp_path, monkeypatch):
    """502/503/504 отдаёт балансировщик; отчёт при этом уже в очереди."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    route = respx.post(REPORTS).mock(
        side_effect=[
            httpx.Response(502, text="<html>Bad Gateway</html>"),
            httpx.Response(503),
            httpx.Response(200, content=TSV.encode("utf-8")),
        ]
    )
    async with DirectClient(settings(tmp_path)) as api:
        assert await api.report(SPEC, client_login="good-client")
    assert route.call_count == 3


def test_whitelist_blocks_foreign_login(tmp_path):
    with pytest.raises(ValueError, match="не разрешён"):
        settings(tmp_path).check_login("someone-elses-account")


def test_package_import_does_not_require_token(monkeypatch):
    monkeypatch.delenv("YD_TOKEN", raising=False)
    import yadirect_mcp

    assert callable(yadirect_mcp.main)


def test_config_rejects_unsafe_queue_size(monkeypatch, tmp_path):
    monkeypatch.setenv("YD_TOKEN", "dummy")
    monkeypatch.setenv("YD_OUT_DIR", str(tmp_path))
    monkeypatch.setenv("YD_MAX_INFLIGHT", "6")
    with pytest.raises(RuntimeError, match="от 1 до 5"):
        config.load()


def test_config_validates_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("YD_TOKEN", "dummy")
    monkeypatch.setenv("YD_OUT_DIR", str(tmp_path))
    monkeypatch.setenv("YD_MODE", "write-everything")
    with pytest.raises(RuntimeError, match="YD_MODE"):
        config.load()


def test_read_back_validates_pagination(tmp_path):
    path = tmp_path / "report.tsv"
    path.write_text(TSV, encoding="utf-8")
    with pytest.raises(ValueError, match="offset"):
        store.read_back(path, -1, 10)
    with pytest.raises(ValueError, match="limit"):
        store.read_back(path, 0, 1001)


def test_parse_tsv_rejects_ragged_rows():
    with pytest.raises(ValueError, match="строка 2"):
        store.parse_tsv("A\tB\nonly-one-field\n")


def normalize_report(**overrides):
    params = {
        "fields": ["Date", "CampaignName", "Impressions", "Clicks", "Cost"],
        "report_type": "CUSTOM_REPORT",
        "goals": None,
        "attribution_models": None,
        "filters": None,
        "order_by": None,
        "limit": None,
    }
    params.update(overrides)
    return reports.normalize(**params)


@pytest.mark.parametrize("model", ["FC", "LSC", "LYDC", "LYDCCD"])
def test_reports_rejects_deprecated_attribution_models(model):
    with pytest.raises(ValueError, match="Устаревшие"):
        normalize_report(goals=["123"], attribution_models=[model])


def test_reports_accepts_current_attribution_models():
    out = normalize_report(
        goals=["123"], attribution_models=["FCCD", "LC", "LSCCD", "AUTO"]
    )
    assert out["attribution_models"] == ["FCCD", "LC", "LSCCD", "AUTO"]


def test_reports_rejects_field_for_wrong_report_type():
    with pytest.raises(ValueError, match="недоступно"):
        normalize_report(fields=["Query", "Clicks"], report_type="CUSTOM_REPORT")


def test_reports_rejects_filter_only_field_in_output():
    with pytest.raises(ValueError, match="только в filters"):
        normalize_report(fields=["Keyword", "Clicks"])


def test_reports_enforces_documented_incompatible_fields():
    with pytest.raises(ValueError, match="взаимоисключающие"):
        normalize_report(fields=["Date", "Month", "Clicks"])
    with pytest.raises(ValueError, match="ClickType несовместим"):
        normalize_report(fields=["ClickType", "Impressions"])
    with pytest.raises(ValueError, match=r"Criteria\*"):
        normalize_report(fields=["Criteria", "Criterion", "Clicks"])


def test_reports_validates_filter_and_order_contract():
    out = normalize_report(
        filters=[{"Field": "CampaignId", "Operator": "in", "Values": [123]}],
        order_by=[{"Field": "Cost", "SortOrder": "descending"}],
        limit=100,
    )
    assert out["filters"] == [
        {"Field": "CampaignId", "Operator": "IN", "Values": ["123"]}
    ]
    assert out["order_by"] == [{"Field": "Cost", "SortOrder": "DESCENDING"}]
    assert out["limit"] == 100


def test_reach_report_requires_campaign_id():
    with pytest.raises(ValueError, match="CampaignId"):
        normalize_report(
            fields=["Impressions", "ImpressionReach"],
            report_type="REACH_AND_FREQUENCY_PERFORMANCE_REPORT",
        )


@pytest.mark.asyncio
@respx.mock
async def test_json_api_rejects_invalid_success_body(tmp_path):
    url = "https://api.direct.yandex.com/json/v5/campaigns"
    respx.post(url).mock(return_value=httpx.Response(200, text="not-json"))
    async with DirectClient(settings(tmp_path)) as api:
        with pytest.raises(DirectError, match="некорректный JSON"):
            await api.call("campaigns", "get", {}, client_login="good-client")


async def _no_sleep(_seconds):
    return None
