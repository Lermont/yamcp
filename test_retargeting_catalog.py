import asyncio

import pytest

from test_adgroups_goals import FakeApi
from yadirect_mcp import goals


def test_retargeting_catalog_is_scoped_and_preserves_segment_kind():
    api = FakeApi(v4=[{"GoalID": 42, "Name": "Visitors", "Type": "segment",
                      "GoalDomain": "example.test", "Login": "client"}])
    result = asyncio.run(goals.read_retargeting(api, "client"))
    assert api.calls == [("v4", "GetRetargetingGoals", {"Logins": ["client"]})]
    assert result["goals"][0]["type"] == "segment"
    assert result["goals"][0]["id"] == 42
    assert result["complete"]


@pytest.mark.parametrize("value", [
    None, {}, [{"GoalID": 42, "Login": "foreign"}],
    [{"GoalID": 42}, {"GoalID": 42}], [{"GoalID": True}],
])
def test_retargeting_catalog_rejects_incomplete_foreign_duplicate_and_invalid_ids(value):
    with pytest.raises(ValueError):
        asyncio.run(goals.read_retargeting(FakeApi(v4=value), "client"))




@pytest.mark.parametrize("goal_type,login,expected", [
    ("goal", "client", True), ("segment", "client", False),
    ("goal", "foreign", False),
])
def test_retargeting_visit_goal_uses_client_catalog(goal_type, login, expected):
    from yadirect_mcp import audience_setup

    class Api(FakeApi):
        async def call_v501(self, *args, **kwargs):
            return {"Campaigns": [{"Id": 123}]}

    api = Api(v4=[{"GoalID": 42, "Type": goal_type, "Login": login}])
    plan = {"client_login": "client", "goal_catalog_campaign_id": 123,
            "campaigns": [{"groups": [{"audience": {
                "retargeting_list": {"Type": "RETARGETING", "Rules": [{
                    "Operator": "ANY", "Arguments": [
                        {"ExternalId": 42, "MembershipLifeSpan": 30}]}]}}}]}]}
    if expected:
        result = asyncio.run(audience_setup.preflight(api, plan))
        assert result["retargeting_goal_ids"] == [42]
    else:
        with pytest.raises(ValueError):
            asyncio.run(audience_setup.preflight(api, plan))


@pytest.mark.parametrize("kind", ["segment", "audience_segment"])
def test_segment_rules_omit_days_and_validate_type(kind):
    from yadirect_mcp import audience_setup

    spec = audience_setup.compile_interest({
        "name": "Non bounces",
        "retargeting_rules": [{"operator": "ANY", "goals": [{"segment_id": 42}]}],
    }, "network")
    args = spec["retargeting_list"]["Rules"][0]["Arguments"]
    assert args == [{"ExternalId": 42}]

    class Api(FakeApi):
        async def call_v501(self, *args, **kwargs):
            return {"Campaigns": [{"Id": 123}]}

    api = Api(v4=[{"GoalID": 42, "Type": kind, "Login": "client"}])
    plan = {"client_login": "client", "goal_catalog_campaign_id": 123,
            "campaigns": [{"groups": [{"audience": spec}]}]}
    assert asyncio.run(audience_setup.preflight(api, plan))["retargeting_goal_ids"] == [42]
    api.v4[0]["Type"] = "goal"
    with pytest.raises(ValueError):
        asyncio.run(audience_setup.preflight(api, plan))


def test_segment_cannot_silently_accept_ignored_days():
    from yadirect_mcp import audience_setup
    with pytest.raises(ValueError):
        audience_setup.compile_interest({
            "name": "Non bounces",
            "retargeting_rules": [{"operator": "ANY",
                                  "goals": [{"segment_id": 42, "days": 30}]}],
        }, "network")


@pytest.mark.asyncio
async def test_only_retargeting_catalog_uses_live4_transport(tmp_path):
    import httpx
    import respx

    from test_wordstat import settings
    from yadirect_mcp.client import DirectClient

    with respx.mock:
        live = respx.post("https://api.direct.yandex.ru/live/v4/json/").mock(
            return_value=httpx.Response(200, json={"data": []}))
        standard = respx.post("https://api.direct.yandex.ru/v4/json/").mock(
            return_value=httpx.Response(200, json={"data": []}))
        async with DirectClient(settings(tmp_path)) as api:
            await api.call_v4("GetRetargetingGoals", {"Logins": ["client"]})
            await api.call_v4("GetStatGoals", {"CampaignID": 123})
        assert live.call_count == standard.call_count == 1
