"""Readback must distinguish inert native placeholders from active expansion."""

import pytest

from test_audience_setup import Api, source
from yadirect_mcp import audience_setup, bundle, executor


@pytest.mark.asyncio
@pytest.mark.parametrize("keyword,state,verified", [
    ("---autotargeting", "SUSPENDED", True),
    ("---autotargeting", "ON", False),
    ("---autotargeting", None, False),
    ("---autotargeting", "UNKNOWN", False),
    ("manual keyword", "SUSPENDED", False),
    ("manual keyword", "ON", False),
])
async def test_only_explicitly_suspended_native_placeholder_is_inert(keyword, state, verified):
    class ContractApi(Api):
        async def call_v501(self, service, method, params, *, client_login=None):
            if service == "keywords" and method == "get":
                assert {"Id", "AdGroupId", "Keyword", "State"} <= set(params["FieldNames"])
            return await super().call_v501(service, method, params, client_login=client_login)

    plan = bundle.compile_bundle(source(), "client")
    api = ContractApi()
    execution = await executor.apply(api, plan)
    group_id = execution["audiences"]["rows"][0]["ad_group_id"]
    api.extra_keywords = [{"Id": 9999, "AdGroupId": group_id,
                           "Keyword": keyword, "State": state}]
    assert (await audience_setup.readback(api, plan, execution))["verified"] is verified


@pytest.mark.asyncio
@pytest.mark.parametrize("lifespan,verified", [(None, True), (0, True), (1, False),
                                               (-1, False), (False, False), (0.0, False)])
async def test_interest_unused_lifespan_only_accepts_null_or_integer_zero(lifespan, verified):
    plan = bundle.compile_bundle(source(), "client")
    api = Api()
    execution = await executor.apply(api, plan)
    api.stored["retargetinglists"][0]["Rules"][0]["Arguments"][0]["MembershipLifeSpan"] = lifespan
    assert (await audience_setup.readback(api, plan, execution))["verified"] is verified


@pytest.mark.asyncio
async def test_goal_lifespan_zero_is_not_normalized():
    plan = bundle.compile_bundle(source(), "client")
    api = Api()
    execution = await executor.apply(api, plan)
    expected = audience_setup.planned(plan)[0][2]["retargeting_list"]
    expected["Type"] = "RETARGETING"
    expected["Rules"][0]["Arguments"][0]["MembershipLifeSpan"] = 30
    actual = api.stored["retargetinglists"][0]
    actual["Type"] = "RETARGETING"
    actual["Rules"][0]["Arguments"][0]["MembershipLifeSpan"] = 0
    assert not (await audience_setup.readback(api, plan, execution))["verified"]
