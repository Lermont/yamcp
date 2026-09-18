"""Reports must request actual campaign goals/model or an explicit comparison."""

import json
from copy import deepcopy

import pytest

from test_server import _in_server
from yadirect_mcp import report_context, reports, store


def campaign(cid=1, goal=77, model="AUTO"):
    return {
        "Id": cid,
        "Type": "UNIFIED_CAMPAIGN",
        "State": "ON",
        "TimeZone": "Europe/Moscow",
        "UnifiedCampaign": {
            "CounterIds": {"Items": [100]},
            "AttributionModel": model,
            "PriorityGoals": {"Items": [{"GoalId": goal, "Value": 1_000_000}]},
            "BiddingStrategy": {
                "Search": {
                    "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                    "WbMaximumConversionRate": {"GoalId": 13},
                }
            },
        },
    }


class Api:
    last_units = None

    def __init__(self, rows=None, **extra):
        self.response = {"Campaigns": rows if rows is not None else [campaign()], **extra}
        self.calls = []

    async def call_v501(self, service, method, params, **kwargs):
        assert (service, method) == ("campaigns", "get")
        self.calls.append(params)
        return deepcopy(self.response)


def contract(**overrides):
    return reports.normalize(
        **{
            "fields": ["CampaignId", "Clicks", "Cost", "Conversions"],
            "report_type": "CUSTOM_REPORT",
            "goals": None,
            "attribution_models": None,
            "filters": None,
            "order_by": None,
            "limit": None,
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_auto_goals_expand_service_goal_13_and_request_actual_model():
    api = Api()
    result, metadata = await report_context.resolve(api, "client", contract())
    assert result["goals"] == ["77"]
    assert result["attribution_models"] == ["AUTO"]
    assert metadata["campaign_settings"][0]["goal_source"] == "strategy_priority_goals"
    assert metadata["metric_semantics"] == "selected_goal_completions_not_verified_leads"
    assert "ARCHIVED" in api.calls[0]["SelectionCriteria"]["States"]
    assert api.calls[0]["Page"]["Limit"] == 10000


@pytest.mark.asyncio
async def test_strategy_single_goal_overrides_priority_goal_and_ignores_serving_off():
    row = campaign()
    strategy = row["UnifiedCampaign"]["BiddingStrategy"]
    strategy["Search"]["WbMaximumConversionRate"]["GoalId"] = 88
    strategy["Network"] = {"BiddingStrategyType": "SERVING_OFF", "AverageCpa": {"GoalId": 99}}
    result, _ = await report_context.resolve(Api([row]), "client", contract())
    assert result["goals"] == ["88"]


@pytest.mark.asyncio
@pytest.mark.parametrize("other", [campaign(2, model="LC"), campaign(2, goal=88)])
async def test_different_campaign_settings_require_split_or_explicit_comparison(other):
    with pytest.raises(ValueError, match="Разделите"):
        await report_context.resolve(Api([campaign(), other]), "client", contract())
    result, metadata = await report_context.resolve(
        Api([campaign(), other]),
        "client",
        contract(goals=["77"], attribution_models=["AUTO"]),
    )
    assert result["goals"] == ["77"]
    assert metadata["comparison_mismatch_campaign_ids"] == [2]
    assert metadata["resolution"] == "explicit_comparison"


@pytest.mark.asyncio
async def test_explicit_campaign_scope_excludes_other_accounts_campaigns():
    api = Api([campaign(), campaign(2, goal=88)])
    result, metadata = await report_context.resolve(
        api,
        "client",
        contract(
            filters=[
                {
                    "Field": "CampaignId",
                    "Operator": "IN",
                    "Values": ["1"],
                }
            ]
        ),
    )
    assert api.calls[0]["SelectionCriteria"] == {"Ids": [1]}
    assert result["goals"] == ["77"]
    assert len(metadata["campaign_settings"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "api,filters,match",
    [
        (Api([], LimitedBy=1), None, "не полностью"),
        (Api([]), None, "Не найдены"),
        (
            Api(),
            [{"Field": "CampaignId", "Operator": "IN", "Values": ["1", "2"]}],
            "всех CampaignId",
        ),
        (Api([campaign(model=None)]), None, "Атрибуция"),
    ],
)
async def test_incomplete_context_never_falls_back_to_lc(api, filters, match):
    with pytest.raises(ValueError, match=match):
        await report_context.resolve(api, "client", contract(filters=filters))


@pytest.mark.asyncio
async def test_portfolio_or_missing_goals_require_explicit_parameters():
    row = campaign()
    row["UnifiedCampaign"]["PackageBiddingStrategy"] = {"StrategyId": 500}
    with pytest.raises(ValueError, match="Цели"):
        await report_context.resolve(Api([row]), "client", contract())
    _, metadata = await report_context.resolve(
        Api([row]),
        "client",
        contract(goals=["77"], attribution_models=["AUTO"]),
    )
    assert metadata["campaign_settings"][0]["goals"] is None
    assert metadata["comparison_mismatch_campaign_ids"] == [1]


@pytest.mark.asyncio
async def test_unresolved_goal13_and_more_than_ten_goals_are_not_silently_reduced():
    row = campaign()
    row["UnifiedCampaign"].pop("PriorityGoals")
    with pytest.raises(ValueError, match="Цели"):
        await report_context.resolve(Api([row]), "client", contract())
    row["UnifiedCampaign"]["PriorityGoals"] = {"Items": [{"GoalId": n} for n in range(20, 31)]}
    with pytest.raises(ValueError, match="от 1 до 10"):
        await report_context.resolve(Api([row]), "client", contract())


@pytest.mark.asyncio
async def test_conversion_filter_also_triggers_resolution():
    result, _ = await report_context.resolve(
        Api(),
        "client",
        contract(
            fields=["CampaignId", "Cost"],
            filters=[{"Field": "Conversions", "Operator": "GREATER_THAN", "Values": ["0"]}],
        ),
    )
    assert result["attribution_models"] == ["AUTO"]


@pytest.mark.asyncio
async def test_cost_only_reports_need_no_campaign_read():
    result, metadata = await report_context.resolve(object(), "client", contract(fields=["Cost"]))
    assert result["goals"] is None
    assert metadata["resolution"] == "not_applicable"


@pytest.mark.asyncio
async def test_all_goals_aggregate_requires_explicit_scope_and_is_labelled():
    result, metadata = await report_context.resolve(
        object(), "client", contract(), conversion_scope="all_goals"
    )
    assert result["goals"] is None
    assert metadata["metric_semantics"] == "all_goal_completions_not_leads"
    assert metadata["attribution_models"] == ["LC"]
    assert metadata["warnings"]
    with pytest.raises(ValueError, match="несовместим"):
        await report_context.resolve(
            object(), "client", contract(goals=["77"]), conversion_scope="all_goals"
        )


def test_report_metadata_persists_with_page_read_and_detects_stale_tsv(tmp_path):
    metadata = {"goals": ["77"], "attribution_models": ["AUTO"], "include_vat": True}
    payload = store.persist(
        "Clicks\tConversions_77_AUTO\n10\t2\n",
        out_dir=tmp_path,
        stem="sample",
        inline_rows=1,
        metadata=metadata,
    )
    path = tmp_path / "sample.tsv"
    assert store.read_back(path, 0, 1)["metadata"] == payload["metadata"]
    assert json.loads((tmp_path / "sample.metadata.json").read_text(encoding="utf-8"))["goals"] == [
        "77"
    ]
    path.write_text("Clicks\n99\n", encoding="utf-8")
    read = store.read_back(path, 0, 1)
    assert "metadata" not in read and "metadata_warning" in read


def test_malformed_report_still_preserves_metadata(tmp_path):
    result = store.persist(
        "Clicks\tCost\n1\n", out_dir=tmp_path, stem="bad", inline_rows=1, metadata={"goals": ["77"]}
    )
    assert result["parse_error"]
    assert result["metadata"]["goals"] == ["77"]


def test_server_sends_resolved_goals_and_auto_and_returns_metadata(tmp_path):
    result = _in_server(
        """import asyncio, json
from yadirect_mcp import server as s
from test_report_context import Api
class ReportApi(Api):
    async def report(self, spec, **kwargs):
        self.spec = spec
        return "Clicks\\tCost\\tConversions_77_AUTO\\n10\\t100\\t2\\n"
    def report_name(self, spec):
        return "resolved"
api = ReportApi()
s._api = lambda: api
payload = asyncio.run(s.direct_report(
    "client", "2026-09-01", "2026-09-04", ["Clicks", "Cost", "Conversions"])).structuredContent
print(json.dumps({"payload": payload, "spec": api.spec}, ensure_ascii=False))
""",
        str(tmp_path),
        YD_MODE="report",
    )
    assert result["spec"]["Goals"] == ["77"]
    assert result["spec"]["AttributionModels"] == ["AUTO"]
    assert result["spec"]["IncludeVAT"] == "YES"
    metadata = result["payload"]["metadata"]
    assert metadata["date_from"] == "2026-09-01"
    assert metadata["received_at"] and metadata["requested_at"]
    assert result["payload"]["totals"]["Conversions_77_AUTO"] == 2
