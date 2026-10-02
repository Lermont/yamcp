"""Fresh goals in an unlaunched account: strict read-only catalog checks."""

import pytest

from test_executor import FakeApi
from test_start_defaults import current
from yadirect_mcp import bundle, executor, landing, regions


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def pages(rows, **kwargs):
        return [{"url": row["url"], "ok": True, "status_code": 200} for row in rows]
    monkeypatch.setattr(landing, "inspect_pages", pages)


class CatalogApi(FakeApi):
    def __init__(self, state="OFF", status="DRAFT", kind="goal", domain="example.test"):
        super().__init__(existing=[{"Id": 1, "Name": "System", "State": state,
                                   "Status": status,
                                   "UnifiedCampaign": {"CounterIds": {"Items": [12345]}}}])
        self.kind, self.domain = kind, domain

    async def call_v501(self, service, method, params, **kwargs):
        if service == "campaigns" and method == "get" and "States" in params["SelectionCriteria"]:
            assert "Status" in params["FieldNames"]
        return await super().call_v501(service, method, params, **kwargs)

    async def call_v4(self, method, param=None):
        if method == "GetStatGoals":
            return []
        assert method == "GetRetargetingGoals"
        assert param == {"Logins": ["client"]}
        return [{"GoalID": 77, "Name": "Fresh", "Type": self.kind,
                 "GoalDomain": self.domain, "Login": "client"}]


@pytest.mark.asyncio
async def test_fresh_goal_verified_against_complete_client_catalog():
    regions.reset_cache()
    result = await executor.preflight(CatalogApi(), bundle.compile_bundle(current(), "client"))
    assert result["goals"]["catalog_source"] == "GetRetargetingGoals"
    assert result["goals"]["stat_catalog_missing"] == [77]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"state": "ON"}, {"status": "ACCEPTED"}, {"kind": "segment"},
    {"domain": "another.test"},
])
async def test_fresh_goal_does_not_relax_live_or_foreign_goal_checks(changes):
    regions.reset_cache()
    with pytest.raises(ValueError):
        await executor.preflight(CatalogApi(**changes), bundle.compile_bundle(current(), "client"))
