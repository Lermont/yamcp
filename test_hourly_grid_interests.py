"""Offline tests for heterogeneous schedules and the read-only live dictionary."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from test_start_defaults import current
from yadirect_mcp import bundle, executor, interest_catalog, schedule


def grid():
    return [[100 if day < 5 and 8 <= hour < 16 else 50 for hour in range(24)]
            for day in range(7)]


def test_heterogeneous_grid_is_bound_to_plan_and_checked_by_readback():
    raw = current()
    raw["schedule"] = {"weekly_bid_percents": grid()}
    raw["profile_context"]["schedule_reason"] = "Staffed hours and off-hours coefficients"
    plan = bundle.compile_bundle(raw, "client")
    executor.validate_plan(plan)
    expected = plan["campaigns"][0]["campaign"]["TimeTargeting"]
    rows = [[int(n) for n in r.split(",")] for r in expected["Schedule"]["Items"]]
    assert rows == [[d + 1, *row] for d, row in enumerate(grid())]
    assert expected["ConsiderWorkingWeekends"] == "NO"
    assert "HolidaysSchedule" not in expected
    assert schedule.is_24_7(expected)
    altered = deepcopy(expected)
    altered["Schedule"]["Items"][0] = altered["Schedule"]["Items"][0].replace("50", "100", 1)
    assert not schedule.matches(altered, expected)


@pytest.mark.parametrize("value", [None, [], [[50] * 24] * 6, [[50] * 23] * 7,
                                       [[True] * 24] * 7, [[50.0] * 24] * 7,
                                       [[55] * 24] * 7, [[210] * 24] * 7,
                                       [[0] * 24] * 7])
def test_invalid_grid_blocks(value):
    with pytest.raises(ValueError, match="weekly_bid_percents"):
        bundle._time_targeting({"weekly_bid_percents": value})


@pytest.mark.parametrize("field", ["days", "hours", "bid_percent"])
def test_ambiguous_grid_blocks(field):
    with pytest.raises(ValueError, match="несовместим"):
        bundle._time_targeting({"weekly_bid_percents": grid(), field: 100})


def test_uniform_grid_keeps_native_default():
    assert bundle._time_targeting({"weekly_bid_percents": [[100] * 24] * 7}) is None


@pytest.mark.asyncio
async def test_dictionary_filters_short_term_without_writes():
    api = AsyncMock()
    api.call_v501.return_value = {"AudienceInterests": [
        {"Id": 10001, "Name": "Building", "InterestType": "SHORT_TERM"},
        {"Id": 10002, "Name": "Other", "InterestType": "SHORT_TERM"},
        {"Id": 20001, "Name": "Building", "InterestType": "LONG_TERM"},
    ]}
    result = await interest_catalog.read(api, "client", "BUILD")
    assert [row["Id"] for row in result["interests"]] == [10001]
    assert result["complete"] and result["catalog_count"] == 3
    api.call_v501.assert_awaited_once_with(
        "dictionaries", "get", {"DictionaryNames": ["AudienceInterests"]},
        client_login="client",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, {"AudienceInterests": [], "LimitedBy": 1},
                                      {"AudienceInterests": [{"Id": 1}, {"Id": 1}]}])
async def test_incomplete_dictionary_blocks(response):
    api = AsyncMock()
    api.call_v501.return_value = response
    with pytest.raises(ValueError):
        await interest_catalog.read(api, "client")
