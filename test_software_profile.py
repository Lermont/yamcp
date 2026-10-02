"""Download objectives without relabelling them as leads; offline only."""

import pytest

from test_policy_profiles import strategy
from test_start_defaults import current
from yadirect_mcp import bundle, executor, policy


def test_download_profile_uses_verified_goal_and_preserves_old_profiles():
    raw = current()
    raw["policy_name"] = "software_new_v3"
    raw["profile_context"]["goals"] = [{"goal_id": 77, "kind": "download"}]
    plan = bundle.compile_bundle(raw, "client")
    assert strategy(plan)["WbMaximumConversionRate"]["GoalId"] == 77
    assert plan["profile_context"]["goals"][0]["kind"] == "download"
    executor.validate_plan(plan)
    assert "download" not in policy.get("services_b2b_new_v3")["profile"]["goal_kinds"]


@pytest.mark.parametrize("kind", ["lead", "purchase", "qualified_lead"])
def test_download_profile_rejects_other_outcomes(kind):
    raw = current()
    raw["policy_name"] = "software_new_v3"
    raw["profile_context"]["goals"] = [{"goal_id": 77, "kind": kind}]
    with pytest.raises(ValueError, match="kind"):
        bundle.compile_bundle(raw, "client")
