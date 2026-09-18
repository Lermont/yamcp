"""Native API defaults, restricted delivery and schedule readback normalization."""

import pytest

from yadirect_mcp import audit, schedule


def row(day, percent=100):
    return ",".join(map(str, [day] + [percent] * 24))


@pytest.mark.parametrize("items", [[], [row(1)], [row(day) for day in range(1, 8)]])
def test_native_and_expanded_api_defaults_are_24_7(items):
    value = {"Schedule": {"Items": items}, "HolidaysSchedule": None,
             "ConsiderWorkingWeekends": "YES"}
    assert audit._is_24_7(value)
    assert schedule.matches(value, None)


@pytest.mark.parametrize("holiday", [
    {"SuspendOnHolidays": "YES"},
    {"SuspendOnHolidays": "NO", "StartHour": 9, "EndHour": 19},
])
def test_holiday_restrictions_are_not_round_the_clock(holiday):
    value = {"Schedule": {"Items": []}, "HolidaysSchedule": holiday}
    assert not audit._is_24_7(value)
    assert not schedule.matches(value, None)


def test_bid_adjustments_allow_24_7_but_do_not_match_native_default():
    value = {"Schedule": {"Items": [row(1, 120)]}}
    assert audit._is_24_7(value)
    assert not schedule.matches(value, None)


def test_custom_readback_accepts_reordered_days_and_null_holidays():
    expected = {"Schedule": {"Items": [row(1, 0), row(2)]}, "ConsiderWorkingWeekends": "NO"}
    actual = {"Schedule": {"Items": [row(2), row(1, 0)]},
              "ConsiderWorkingWeekends": "NO", "HolidaysSchedule": None}
    assert schedule.matches(actual, expected)
    actual["ConsiderWorkingWeekends"] = "YES"
    assert not schedule.matches(actual, expected)


@pytest.mark.parametrize("value", [None, {"Schedule": {"Items": ["bad"]}},
                                     {"Schedule": {"Items": [row(1), row(1)]}}])
def test_unreadable_targeting_is_not_accepted_as_native(value):
    assert not audit._is_24_7(value)
    assert not schedule.matches(value, None)
