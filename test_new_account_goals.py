"""No external requests: all catalogs use a fake Direct API."""

from copy import deepcopy

import pytest

from yadirect_mcp import new_account_goals


class Api:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def call_v4(self, method, params):
        assert method == "GetRetargetingGoals"
        assert params == {"Logins": ["client"]}
        self.calls.append((method, params))
        return deepcopy(self.rows)


def row():
    return {"GoalID": 201, "Name": "Form", "Type": "goal",
            "GoalDomain": "example.test", "Login": "client"}


@pytest.mark.asyncio
async def test_first_campaign_real_goal_is_verified_without_writes():
    api = Api([row()])
    result = await new_account_goals.verify(api, "client", {201}, ["https://example.test/"])
    assert result["status"] == "PASS"
    assert result["counter_mapping"] == "caller_reviewed"
    assert len(api.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing", "segment", "audience_segment", "foreign",
                                 "domain", "empty_domain", "duplicate", "incomplete"])
async def test_rejects_unverified_goals(bad):
    rows = [row()]
    if bad == "missing":
        rows = []
    elif bad in {"segment", "audience_segment"}:
        rows[0]["Type"] = bad
    elif bad == "foreign":
        rows[0]["Login"] = "another"
    elif bad == "domain":
        rows[0]["GoalDomain"] = "another.test"
    elif bad == "empty_domain":
        rows[0]["GoalDomain"] = None
    elif bad == "duplicate":
        rows.append(row())
    else:
        rows = None
    with pytest.raises(ValueError):
        await new_account_goals.verify(Api(rows), "client", {201}, ["https://example.test/"])
