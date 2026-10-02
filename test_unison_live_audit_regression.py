"""Live API normalizations and optional analytics must not create false audit blocks."""

import pytest

from test_policy_audit import campaign, findings
from yadirect_mcp import audit, policy


@pytest.mark.parametrize(
    "counter_ids,goals,expected",
    [
        ([], [], "PASS"),
        ([123], [], "MANUAL"),
        ([], [{"GoalId": 7}], "MANUAL"),
    ],
)
def test_landing_counter_optional_only_when_no_counter_or_goal_is_selected(
    counter_ids, goals, expected
):
    item = campaign(counter_ids=counter_ids, priority_goals=goals)
    pages = [
        {
            "campaigns": [10],
            "site_check": {"url": "https://example.test/", "ok": True, "counter_ids": []},
        }
    ]
    result = audit.audit_campaign(item, selected_policy=policy.get(), landing_pages=pages)
    assert findings(result)["landing.metrika_counter"]["status"] == expected


def test_landing_counter_still_required_for_conversion_strategy():
    item = campaign(counter_ids=[], priority_goals=[])
    item["bidding_strategy"]["Search"]["BiddingStrategyType"] = "WB_MAXIMUM_CONVERSION_RATE"
    pages = [
        {
            "campaigns": [10],
            "site_check": {"url": "https://example.test/", "ok": True, "counter_ids": []},
        }
    ]
    result = audit.audit_campaign(item, selected_policy=policy.get(), landing_pages=pages)
    assert findings(result)["landing.metrika_counter"]["status"] == "MANUAL"


def test_network_exclusion_case_normalization_keeps_missing_site_detection():
    item = campaign(counter_ids=[], priority_goals=[])
    item["bidding_strategy"]["Network"] = item["bidding_strategy"]["Search"]
    item["bidding_strategy"]["Search"] = {"BiddingStrategyType": "SERVING_OFF"}
    item["excluded_sites"] = [
        v.lower()
        for v in (*policy.load_snapshot("network_excluded_sites"), "AdsNative", "BidSwitch")
    ]
    result = audit.audit_campaign(item, selected_policy=policy.get())
    assert findings(result)["network.excluded_sites"]["status"] == "PASS"
    item["excluded_sites"].pop()
    result = audit.audit_campaign(item, selected_policy=policy.get())
    assert findings(result)["network.excluded_sites"]["status"] == "BLOCK"
