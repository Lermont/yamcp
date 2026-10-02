"""The recheck wrapper may read goal catalogs, but cannot call other Live methods."""
from unittest.mock import AsyncMock

import pytest

from yadirect_mcp.verification import ReadOnlyAPI


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GetRetargetingGoals", "GetStatGoals"])
async def test_goal_catalog_allowed(method):
    api = AsyncMock()
    api.call_v4.return_value = [{"GoalID": 123}]
    args = {"Logins": ["client"]} if method == "GetRetargetingGoals" else {"CampaignID": 1}
    assert await ReadOnlyAPI(api).call_v4(method, args) == [{"GoalID": 123}]
    api.call_v4.assert_awaited_once_with(method, args)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["CreateNewForecast", "DeleteForecastReport", "GetForecast",
                                   "Retargeting", "GetRetargetingGoals ", "getretargetinggoals"])
async def test_other_live_methods_blocked_before_call(method):
    api = AsyncMock()
    with pytest.raises(PermissionError):
        await ReadOnlyAPI(api).call_v4(method, {})
    api.call_v4.assert_not_awaited()
