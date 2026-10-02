import json
from pathlib import Path

import pytest

from yadirect_mcp import forecast
from yadirect_mcp.client import DirectError


class API:
    def __init__(self, status="Done", rows=None):
        self.calls = []
        self.status = status
        self.rows = rows if rows is not None else [{"Phrase": "test", "Currency": "RUB"}]

    async def call_v4(self, method, param=None):
        self.calls.append((method, param))
        return {"CreateNewForecast": 123,
                "GetForecastList": [{"ForecastID": 123, "StatusForecast": self.status}],
                "GetForecast": {"Phrases": self.rows}, "DeleteForecastReport": 1}[method]


@pytest.mark.asyncio
async def test_roundtrip_and_cached_get(tmp_path):
    api = API()
    created = await forecast.run(api, out_dir=tmp_path, action="create",
                                 phrases=["test", "test"], geo_ids=[213])
    assert created["status"] == "pending"
    done = await forecast.run(api, out_dir=tmp_path, action="get",
                             artifact_path=created["artifact_path"])
    assert done["complete"] and done["status"] == "done"
    assert api.calls[-1] == ("DeleteForecastReport", 123)
    before = len(api.calls)
    assert await forecast.run(api, out_dir=tmp_path, action="get",
                              artifact_path=done["artifact_path"]) == done
    assert len(api.calls) == before
    assert json.loads((tmp_path / done["artifact_path"]).read_text())["response"]


@pytest.mark.asyncio
async def test_pending_missing_and_incomplete(tmp_path):
    api = API(status="Pending", rows=[])
    a = await forecast.run(api, out_dir=tmp_path, action="create",
                           phrases=["test"], geo_ids=[213])
    assert (await forecast.run(api, out_dir=tmp_path, action="get",
                               artifact_path=a["artifact_path"]))["status"] == "pending"
    api.status = "Unknown"
    with pytest.raises(DirectError):
        await forecast.run(api, out_dir=tmp_path, action="get", artifact_path=a["artifact_path"])
    api.status = "Done"
    assert not (await forecast.run(api, out_dir=tmp_path, action="get",
                                   artifact_path=a["artifact_path"]))["complete"]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"action": "create", "phrases": ["test"]},
    {"action": "create", "phrases": [], "geo_ids": [213]},
    {"action": "create", "phrases": ["test"], "geo_ids": [-1]},
    {"action": "get", "artifact_path": "../outside.json"},
    {"action": "delete"},
])
async def test_invalid_requests_do_not_call_provider(tmp_path, args):
    api = API()
    with pytest.raises(ValueError):
        await forecast.run(api, out_dir=tmp_path, **args)
    assert not api.calls


@pytest.mark.asyncio
async def test_forecast_methods_use_live4(tmp_path):
    import httpx
    import respx

    from test_wordstat import settings
    from yadirect_mcp.client import DirectClient

    with respx.mock:
        live = respx.post("https://api.direct.yandex.ru/live/v4/json/").mock(
            return_value=httpx.Response(200, json={"data": []}))
        async with DirectClient(settings(tmp_path)) as api:
            for method in ["CreateNewForecast", "GetForecast", "GetForecastList",
                           "DeleteForecastReport"]:
                await api.call_v4(method)
        assert live.call_count == 4


@pytest.mark.asyncio
async def test_explicit_pending_probe_preserves_provider_error(tmp_path):
    class PendingAPI(API):
        async def call_v4(self, method, param=None):
            if method == "GetForecast":
                raise DirectError("Preparing", code=74)
            return await super().call_v4(method, param)

    api = PendingAPI(status="Pending")
    created = await forecast.run(api, out_dir=tmp_path, action="create",
                                 phrases=["test"], geo_ids=[213])
    with pytest.raises(DirectError):
        await forecast.run(api, out_dir=tmp_path, action="get", probe_pending=True,
                           artifact_path=created["artifact_path"])
    assert not any(m == "DeleteForecastReport" for m, _ in api.calls)


@pytest.mark.asyncio
async def test_explicit_probe_accepts_ready_data_from_stale_queue(tmp_path):
    api = API(status="Pending")
    created = await forecast.run(api, out_dir=tmp_path, action="create",
                                 phrases=["test"], geo_ids=[213])
    done = await forecast.run(api, out_dir=tmp_path, action="get", probe_pending=True,
                              artifact_path=created["artifact_path"])
    assert done["status"] == "done" and done["complete"]


@pytest.mark.asyncio
async def test_legacy_completion_is_not_budget_readiness_including_cached_get(tmp_path):
    api = API()
    pending = await forecast.run(api, out_dir=tmp_path, action="create",
                                 phrases=["test"], geo_ids=[213])
    done = await forecast.run(api, out_dir=tmp_path, action="get",
                              artifact_path=pending["artifact_path"])
    path = Path(done["artifact_path"])
    saved = path.read_bytes()
    calls = list(api.calls)
    cached = await forecast.run(api, out_dir=tmp_path, action="get",
                                artifact_path=str(path))
    assert done["status"] == "done" and done["complete"] is True
    assert cached == done
    assert api.calls == calls
    assert path.read_bytes() == saved
    assert "quality_contract" not in json.loads(saved)
    for result in (pending, done, cached):
        contract = result["quality_contract"]
        assert contract["requirements_uri"] == "direct://kb/forecast-quality"
        assert contract["complete_scope"] == "provider_response_rows_only"
        assert contract["traffic_level_clicks_available"] is False
        assert contract["ready_for_budget_optimization"] is False


@pytest.mark.parametrize("mode", ["report", "campaign_setup"])
def test_forecast_requirements_are_exposed_in_each_server_mode(tmp_path, mode):
    from test_server import _in_server

    result = _in_server(
        "import asyncio, json\n"
        "import yadirect_mcp.server as s\n"
        "tools = asyncio.run(mcp.list_tools())\n"
        "tool = next(t for t in tools if t.name == 'direct_forecast')\n"
        "print(json.dumps({'instructions': s._instructions(s.SETTINGS), "
        "'description': tool.description, "
        "'doc': s.knowledge.read('forecast-quality'), "
        "'index': s.knowledge.index(), 'runtime': s.runtime.describe(s.SETTINGS)}))",
        str(tmp_path), YD_MODE=mode,
    )
    assert forecast.INSTRUCTIONS in result["instructions"]
    assert "direct://kb/forecast-quality" in result["description"]
    assert "direct://kb/forecast-quality" in str(result["index"])
    assert "максимум релевантных прогнозных кликов" in result["doc"]
    assert "forecast_quality_requirements" in result["runtime"]["features"]
    assert result["runtime"]["restart_required"] is False
