"""Counter attachment never removes counters or changes active campaigns; offline."""
from copy import deepcopy

import pytest

from test_repair import FakeApi
from yadirect_mcp import repair


class Api(FakeApi):
    def __init__(self, *, state="OFF", status="DRAFT", counters=None):
        super().__init__()
        self.row = {"Id": 10, "Name": "Draft", "Type": "UNIFIED_CAMPAIGN",
                    "State": state, "Status": status,
                    "UnifiedCampaign": {"CounterIds": {"Items": counters or []}}}

    async def call_v501(self, service, method, params, **kwargs):
        if service == "campaigns" and method == "get":
            return {"Campaigns": [deepcopy(self.row)]}
        if service == "campaigns" and method == "update":
            self.calls.append((service, method, deepcopy(params)))
            self.row["UnifiedCampaign"].update(params["Campaigns"][0]["UnifiedCampaign"])
            return {"UpdateResults": [{"Id": 10}]}
        return await super().call_v501(service, method, params, **kwargs)


def plan(counters=None):
    return repair.normalize({"campaigns": [{"id": 10, "counter_ids":
                                            [123] if counters is None else counters}]}, "client")


@pytest.mark.parametrize("values", [[], [0], [-1], [True], [123, 123], "123"])
def test_invalid(values):
    with pytest.raises(ValueError):
        plan(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"state": "ON"}, {"status": "ACCEPTED"},
                                    {"counters": [456]}])
async def test_live_or_removal_blocked(kwargs):
    api = Api(**kwargs)
    with pytest.raises(ValueError):
        await repair.preflight(api, plan())
    assert not api.calls


@pytest.mark.asyncio
async def test_attach_readback_and_mismatch():
    api, p = Api(), plan()
    pre = await repair.preflight(api, p)
    result = await repair.apply(api, p, expected_preflight=pre)
    assert result["activated"] is False
    assert [(s, m) for s, m, _ in api.calls] == [("campaigns", "update")]
    before = {"campaigns": pre["before"]["campaigns"]}
    assert (await repair.readback(api, p, before=before))["verified"]
    api.row["UnifiedCampaign"]["CounterIds"] = {"Items": [456]}
    assert not (await repair.readback(api, p, before=before))["verified"]
