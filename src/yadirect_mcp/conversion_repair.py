"""Restricted conversion-strategy updates; never change serving placements."""

from copy import deepcopy

from . import bundle, campaigns, completeness, goals, launch_checks
from .identifiers import parse_id

PLACEMENTS = {"SearchResults", "ProductGallery", "DynamicPlaces", "Maps",
              "SearchOrganizationList"}


def compile_strategy(source, priority_goals):
    required = {"weekly_budget", "goal_id", "search_placements", "network_mode"}
    if (not isinstance(source, dict) or not required <= source.keys()
            or source.keys() - required - {"bid_ceiling", "channel"}):
        raise ValueError("conversion_strategy: invalid or missing fields")
    channel = source.get("channel", "search")
    if channel not in {"search", "network"}:
        raise ValueError("conversion_strategy: unsupported channel")
    placements = source["search_placements"]
    if (not isinstance(placements, dict) or placements.keys() != PLACEMENTS
            or any(v not in {"YES", "NO"} for v in placements.values())
            or (channel == "search" and "YES" not in placements.values())
            or (channel == "network" and "YES" in placements.values())):
        raise ValueError("conversion_strategy: explicit search placements required")
    network_modes = ({"WB_MAXIMUM_CONVERSION_RATE"} if channel == "network"
                     else {"SERVING_OFF", "NETWORK_DEFAULT"})
    if source["network_mode"] not in network_modes:
        raise ValueError("conversion_strategy: unsupported network mode")
    goal_id = parse_id(source["goal_id"], "conversion_strategy.goal_id")
    launch_checks.validate_conversion_goal(goal_id, {g["GoalId"] for g in priority_goals})
    params = {"WeeklySpendLimit": bundle._micros(source["weekly_budget"], "weekly_budget"),
              "GoalId": goal_id}
    if source.get("bid_ceiling") is not None:
        params["BidCeiling"] = bundle._micros(source["bid_ceiling"], "bid_ceiling")
    if channel == "network":
        return {"Search": {"BiddingStrategyType": "SERVING_OFF",
                           "PlacementTypes": deepcopy(placements)},
                "Network": {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                            "WbMaximumConversionRate": params}}
    return {"Search": {"BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
                       "PlacementTypes": deepcopy(placements),
                       "WbMaximumConversionRate": params},
            "Network": {"BiddingStrategyType": source["network_mode"]}}


async def preflight(api, change, before, login):
    unified = change.get("UnifiedCampaign", {})
    expected = unified.get("BiddingStrategy")
    if expected is None:
        return
    if before.get("package_bidding_strategy") or not before.get("counter_ids"):
        raise ValueError("Conversion repair requires a counter and an individual strategy")
    previous = before.get("bidding_strategy") or {}
    channel = "Network" if expected["Search"]["BiddingStrategyType"] == "SERVING_OFF" else "Search"
    if channel == "Network":
        # Network-only conversion must never enable search or remove a placement restriction.
        if (previous.get("Search") != expected["Search"]
                or previous.get("Network", {}).get("BiddingStrategyType")
                not in {"WB_MAXIMUM_CLICKS", "WB_MAXIMUM_CONVERSION_RATE"}
                or previous.get("Network", {}).get("PlacementTypes")):
            raise ValueError("Conversion repair must preserve network-only placements")
    elif (previous.get("Search", {}).get("BiddingStrategyType") == "SERVING_OFF"
            or previous.get("Search", {}).get("PlacementTypes")
            != expected["Search"]["PlacementTypes"]
            or previous.get("Network", {}).get("BiddingStrategyType")
            != expected["Network"]["BiddingStrategyType"]):
        raise ValueError("Conversion repair must preserve placements and network mode")
    selected = (unified.get("PriorityGoals") or {}).get("Items", [])
    goal_ids = {g["GoalId"] for g in selected}
    catalog = await goals.read(api, change["Id"])
    available = {parse_id(g["id"], "goal_id") for g in catalog["goals"]}
    if not catalog["goals"] and not completeness.sources(goals=catalog):
        # GetStatGoals may return no rows for a campaign without goal statistics.
        # A complete catalog from the same client's identical counter set is valid
        # evidence; never use another counter or a different client as fallback.
        inventory = await campaigns.read_settings(api, login, limit=10000)
        rows = inventory["campaigns"]
        if (completeness.sources(campaigns=inventory)
                or len({r["id"] for r in rows}) != len(rows)):
            raise ValueError("Conversion repair requires complete counter ownership evidence")
        for row in rows:
            if (row["id"] == change["Id"] or row.get("state") == "ARCHIVED"
                    or set(row.get("counter_ids") or []) != set(before["counter_ids"])):
                continue
            candidate = await goals.read(api, row["id"])
            if completeness.sources(goals=candidate):
                raise ValueError("Conversion repair received an incomplete goal catalog")
            available = {parse_id(g["id"], "goal_id") for g in candidate["goals"]}
            if goal_ids <= available:
                break
    if completeness.sources(goals=catalog) or not goal_ids <= available:
        raise ValueError("Conversion repair requires verified campaign goals")
    launch_checks.validate_conversion_goal(
        expected[channel]["WbMaximumConversionRate"]["GoalId"], goal_ids,
    )


def matches(actual, expected):
    value = deepcopy(actual)
    if not isinstance(value, dict):
        return False
    for channel in ("Search", "Network"):
        params = value.get(channel, {}).get("WbMaximumConversionRate", {})
        # Direct returns this derived discriminator although update does not accept it.
        if params.get("BudgetType") == "WEEKLY_BUDGET":
            params.pop("BudgetType")
        wanted = expected.get(channel, {}).get("WbMaximumConversionRate", {})
        if params.get("BidCeiling") is None and "BidCeiling" not in wanted:
            params.pop("BidCeiling", None)
    return value == expected
