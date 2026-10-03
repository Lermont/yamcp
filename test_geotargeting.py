"""Offline regressions for the retired campaign geo switch."""
import asyncio
from copy import deepcopy

import pytest

from test_bundle import source_bundle
from test_executor import FakeApi, readback_payloads
from test_executor import mock_preflight_http as mock_preflight_http
from test_report_pipeline import register
from test_server import _in_server
from yadirect_mcp import bundle, executor, policy

OPTION = "ENABLE_AREA_OF_INTEREST_TARGETING"


@pytest.mark.parametrize("value", [True, False])
def test_new_bundle_rejects_retired_setting_with_actionable_message(value):
    raw = source_bundle()
    raw["settings"] = {OPTION: value}
    with pytest.raises(ValueError, match="удалите.*settings"):
        bundle.compile_bundle(raw, "client")


@pytest.mark.parametrize("value", ["YES", "NO"])
@pytest.mark.parametrize("operation", [executor.preflight, executor.apply])
def test_saved_plan_cannot_send_retired_setting(value, operation):
    plan = bundle.compile_bundle(source_bundle(), "client")
    plan["campaigns"][0]["campaign"]["UnifiedCampaign"]["Settings"].append(
        {"Option": OPTION, "Value": value},
    )
    before = deepcopy(plan)
    api = FakeApi()
    with pytest.raises(ValueError, match="повторите direct_campaign_plan"):
        asyncio.run(operation(api, plan))
    assert api.calls == []
    assert plan == before


@pytest.mark.parametrize("expected", ["YES", "NO"])
@pytest.mark.parametrize("actual", [None, "YES", "NO"])
def test_historical_readback_ignores_only_retired_setting(expected, actual):
    plan = bundle.compile_bundle(source_bundle(), "client")
    execution = asyncio.run(executor.apply(FakeApi(), plan))
    settings, groups, ads, keywords = readback_payloads(plan, execution)
    baseline = executor.compare_readback(plan, execution, settings, groups, ads, keywords)
    plan["campaigns"][0]["campaign"]["UnifiedCampaign"]["Settings"].append(
        {"Option": OPTION, "Value": expected},
    )
    if actual is not None:
        settings["campaigns"][0]["settings"][OPTION] = actual
    before = deepcopy((plan, execution, settings))
    result = executor.compare_readback(plan, execution, settings, groups, ads, keywords)
    assert result == baseline
    assert (plan, execution, settings) == before
    # Real differences must still fail the same independent readback.
    settings["campaigns"][0]["settings"]["ENABLE_SITE_MONITORING"] = "NO"
    result = executor.compare_readback(plan, execution, settings, groups, ads, keywords)
    assert not result["verified"]
    assert any(row["rule"] == "readback.settings" and row["status"] == policy.BLOCK
               for row in result["findings"])


@pytest.mark.parametrize("mode", ["report", "campaign_setup"])
def test_current_geo_guidance_reaches_tools_without_changing_frozen_policy(tmp_path, mode):
    from yadirect_mcp import geotargeting

    snapshot = policy.get()
    response = _in_server(
        "import asyncio, json\n"
        "import yadirect_mcp.server as s\n"
        "print(json.dumps({'instructions': s.mcp.instructions, "
        "'policy': asyncio.run(s.direct_policy()).structured_content, "
        "'features': s.runtime.describe(s.SETTINGS)['features']}))",
        str(tmp_path), YD_MODE=mode,
    )
    assert geotargeting.INSTRUCTIONS in response["instructions"]
    assert response["policy"]["geotargeting"] == geotargeting.guidance()
    assert response["policy"]["policy"] == snapshot
    assert "retired_geotargeting_guard" in response["features"]


def test_client_report_brief_does_not_treat_retirement_as_a_finding(tmp_path):
    from yadirect_mcp import geotargeting

    _, response, _, _ = register(tmp_path)
    assert geotargeting.INSTRUCTIONS in response["brief"]["rules"]
