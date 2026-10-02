"""Compare Direct schedules by hours, including the native 24/7 defaults."""

from __future__ import annotations

from typing import Any


def _normalize(value: Any) -> tuple | None:
    """None means unreadable; an empty schedule means the documented API default."""
    if not isinstance(value, dict):
        return None
    schedule = value.get("Schedule") or {}
    if not isinstance(schedule, dict) or not isinstance(schedule.get("Items", []), list):
        return None
    items = schedule.get("Items", [])
    legacy = bool(items) and all(isinstance(item, dict) for item in items)
    week = [[0 if legacy else 100] * 24 for _ in range(7)]
    try:
        seen = set()
        for item in items:
            if legacy:
                percent = int(item.get("BidPercent", 100))
                days = [int(day) for day in item["Days"]]
                hours = [int(hour) for hour in item["Hours"]]
                if not 0 <= percent <= 200 or percent % 10:
                    return None
                if not days or not hours:
                    return None
                for day in days:
                    for hour in hours:
                        if day not in range(1, 8) or hour not in range(24):
                            return None
                        week[day - 1][hour] = percent
            else:
                if not isinstance(item, str):
                    return None
                day, *percents = [int(part) for part in item.split(",")]
                if day not in range(1, 8) or day in seen or len(percents) != 24:
                    return None
                if any(not 0 <= percent <= 200 or percent % 10 for percent in percents):
                    return None
                seen.add(day)
                week[day - 1] = percents

        consider = value.get("ConsiderWorkingWeekends", "NO")
        if consider not in {"YES", "NO"}:
            return None
        # Swapping identical days does not change delivery or bid adjustments.
        if all(day == week[0] for day in week):
            consider = "NO"
        holiday = value.get("HolidaysSchedule")
        holiday_hours = None
        if holiday is not None:
            if not isinstance(holiday, dict):
                return None
            suspend = holiday.get("SuspendOnHolidays")
            if suspend == "YES":
                holiday_hours = [0] * 24
            elif suspend == "NO":
                start, end = int(holiday["StartHour"]), int(holiday["EndHour"])
                percent = int(holiday.get("BidPercent", 100))
                if not 0 <= start < end <= 24 or not 10 <= percent <= 200 or percent % 10:
                    return None
                holiday_hours = [percent if start <= hour < end else 0 for hour in range(24)]
            else:
                return None
            if all(day == holiday_hours for day in week):
                holiday_hours = None
    except (KeyError, TypeError, ValueError):
        return None
    return tuple(tuple(day) for day in week), consider, (
        tuple(holiday_hours) if holiday_hours is not None else None
    )


def is_24_7(value: Any) -> bool:
    normalized = _normalize(value)
    if normalized is None:
        return False
    week, _, holiday = normalized
    return all(percent > 0 for day in week for percent in day) and (
        holiday is None or all(percent > 0 for percent in holiday)
    )


def matches(actual: Any, expected: Any) -> bool:
    """Omitted targeting in a creation plan requests native 24/7; missing readback is unknown."""
    normalized = _normalize(actual)
    return normalized is not None and normalized == _normalize(
        {} if expected is None else expected
    )
