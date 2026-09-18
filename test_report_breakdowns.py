"""Slice integration: full requests, exact identifiers, nulls and atomic enrichment."""

import csv
import io
from copy import deepcopy

import pytest

from test_report_pipeline import API, register
from yadirect_mcp import report_breakdowns as breakdowns
from yadirect_mcp import report_pipeline as pipeline


class SliceAPI(API):
    def __init__(self, fail_slice=False):
        super().__init__()
        self.fail_slice = fail_slice
        self.specs = []

    async def report(self, spec, **kwargs):
        self.specs.append(deepcopy(spec))
        if "Date" in spec["FieldNames"]:
            return await super().report(spec, **kwargs)
        if self.fail_slice and "Device" in spec["FieldNames"]:
            raise RuntimeError("device report failed")
        fields = ["Conversions_77_AUTO" if f == "Conversions" else f for f in spec["FieldNames"]]
        values = {"CampaignId": "1", "AdNetworkType": "SEARCH", "Cost": "120.25",
                  "Impressions": "100", "Clicks": "10", "Conversions_77_AUTO": "2.5",
                  "AdGroupId": "33", "AdGroupName": "Claude", "AdId": "1920705538295248721",
                  "CriterionId": "441", "CriterionType": "KEYWORD", "Criterion": "claude max",
                  "Query": "купить claude", "Placement": "example.org", "Device": "DESKTOP",
                  "LocationOfPresenceId": "213", "LocationOfPresenceName": "Москва",
                  "Gender": "MALE", "Age": "AGE_25_34"}
        output = io.StringIO()
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(fields)
        if "Placement" not in fields:
            writer.writerow([values[f] for f in fields])
        return output.getvalue()

    async def call_v501(self, service, method, params, **kwargs):
        if service == "ads":
            assert params["SelectionCriteria"]["Ids"] == [1920705538295248721]
            return {"Ads": [{"Id": 1920705538295248721,
                             "TextAd": {"Title": "Claude MAX", "Text": "Текущий текст"}}]}
        return await super().call_v501(service, method, params, **kwargs)


@pytest.mark.asyncio
async def test_refresh_full_slices_and_enrich_preserves_daily_setup_history(tmp_path):
    cfg, _, model, _ = register(tmp_path, history=True)
    api = SliceAPI()
    result = await pipeline.refresh(cfg, api, "client", as_of="2026-08-02")
    state = pipeline._load(tmp_path / "client")
    current = state["model"]["periods"][0]
    slices = current["breakdowns"]["slices"]
    assert set(slices) == set(breakdowns.SLICES)
    assert slices["ads"]["rows"][0]["dimensions"]["AdId"] == "1920705538295248721"
    assert slices["ads"]["currentCopy"]["1920705538295248721"]["title"] == "Claude MAX"
    assert len(api.specs) == 10
    for spec in api.specs:
        assert "Page" not in spec and "OrderBy" not in spec
        assert spec["Goals"] == ["77"] and spec["IncludeVAT"] == "YES"
        assert spec["SelectionCriteria"]["DateFrom"] == "2026-08-01"
    query = next(s for s in api.specs if s["ReportType"] == "SEARCH_QUERY_PERFORMANCE_REPORT")
    assert "AdNetworkType" not in query["FieldNames"]
    assert result["brief"]["breakdowns"]["groups"]["row_count"] == 1
    before = deepcopy(state["model"])
    enriched = await pipeline.enrich(cfg, SliceAPI(), "client")
    assert enriched["status"] == "enriched"
    after = pipeline._load(tmp_path / "client")["model"]
    assert after["daily"] == before["daily"]
    assert after["setup"] == model["setup"]
    assert after["periods"][1:] == before["periods"][1:]
    assert after["periods"][0]["dataRevision"] == current["dataRevision"]


@pytest.mark.asyncio
async def test_slice_failure_keeps_published_local_revision(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    await pipeline.refresh(cfg, SliceAPI(), "client", as_of="2026-08-02")
    folder = tmp_path / "client"
    before = (folder / "index.html").read_bytes()
    revision = pipeline._load(folder)["revision"]
    with pytest.raises(RuntimeError, match="device report"):
        await pipeline.enrich(cfg, SliceAPI(fail_slice=True), "client")
    assert (folder / "index.html").read_bytes() == before
    assert pipeline._load(folder)["revision"] == revision
    assert not (folder / ".client-report/refresh.lock").exists()


def test_missing_goals_inferred_only_from_matching_partition():
    controls = [{"campaign": "s", "channel": "search", "clicks": 10,
                 "impressions": 100, "spend": 20, "conversions": 2.5}]
    rows = [{**controls[0], "clicks": 5, "impressions": 50},
            {**controls[0], "clicks": 5, "impressions": 50, "conversions": None}]
    original = deepcopy(rows)
    check = breakdowns.reconcile(rows, controls, "groups")
    assert check[0]["inferredGoalZeros"] == 1 and rows[1]["conversions"] == 0
    queries = deepcopy(original)
    breakdowns.reconcile(queries, controls, "queries")
    assert queries[1]["conversions"] is None
    controls[0]["clicks"] = 11
    breakdowns.reconcile(original, controls, "groups")
    assert original[1]["conversions"] is None


@pytest.mark.parametrize("invalid", ["NaN", "-1", "inf", "1.2"])
def test_invalid_counts_rejected(invalid):
    with pytest.raises(ValueError):
        breakdowns.number(invalid, integer=True)


def test_duplicate_unknown_campaign_and_empty_header_rejected():
    config = {"campaigns": [{"id": 1, "channel": "search", "report_id": "s"}], "measurement": None}
    header = "CampaignId\tAdNetworkType\tDevice\tCost\tImpressions\tClicks\n"
    row = "1\tSEARCH\tDESKTOP\t--\t100\t10\n"
    assert breakdowns.parse(header + row, config, "devices", ["Device"])[0]["spend"] is None
    for bad in [header + row + row, header + row.replace("1\tSEARCH", "2\tSEARCH"), ""]:
        with pytest.raises(ValueError):
            breakdowns.parse(bad, config, "devices", ["Device"])
