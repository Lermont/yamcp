import pytest

import test_audience_group
from yadirect_mcp import audience_group as mod
from yadirect_mcp import repair

RULES = [{"operator": "ALL", "goals": [{"goal_id": 4113308859, "days": 30}]},
         {"operator": "NONE", "goals": [{"goal_id": 667025923, "days": 90},
                                        {"goal_id": 667027341, "days": 90}]}]


def test_goal_rules_plan_and_hash():
    raw = {"campaign_id": 1, "source_ad_id": 2, "name": "Ретаргетинг", "goal_rules": RULES}
    plan = repair.normalize({"audience_group": raw}, "client")
    item = plan["audience_group"]
    assert "segment_id" not in item
    assert item["goal_rules"][1] == {"Operator": "NONE", "Arguments": [
        {"ExternalId": 667025923, "MembershipLifeSpan": 90},
        {"ExternalId": 667027341, "MembershipLifeSpan": 90}]}
    longer = [{**RULES[0], "goals": [{"goal_id": 4113308859, "days": 60}]}]
    other = mod.normalize({**raw, "goal_rules": longer}, "client")
    assert other["plan_hash"] != plan["plan_hash"]


@pytest.mark.parametrize("raw", [
    {"segment_id": 4, "goal_rules": RULES},
    {"goal_rules": [{"operator": "NONE", "goals": [{"goal_id": 1, "days": 30}]}]},
    {"goal_rules": [{"operator": "ALL", "goals": [{"goal_id": 1, "days": 541}]}]},
    {"goal_rules": [{"operator": "ALL",
                     "goals": [{"goal_id": 1, "days": 30}, {"goal_id": 1, "days": 9}]}]},
    {"goal_rules": [{"operator": "OR", "goals": [{"goal_id": 1, "days": 30}]}]},
    {},
])
def test_goal_rules_validation(raw):
    with pytest.raises(ValueError):
        mod.normalize({"campaign_id": 1, "source_ad_id": 2, "name": "x", **raw}, "client")


def test_matching_lists_compares_lifespan_and_ignores_order():
    item = mod.normalize({"campaign_id": 1, "source_ad_id": 2, "name": "x", "goal_rules": RULES},
                         "client")["audience_group"]
    same = {"Id": 7, "Type": "RETARGETING", "IsAvailable": "YES", "Scope": "FOR_TARGETS_ONLY",
            "Rules": list(reversed(item["goal_rules"]))}
    other = {**same, "Id": 8, "Rules": [{"Operator": "ALL", "Arguments": [
        {"ExternalId": 4113308859, "MembershipLifeSpan": 7}]}]}
    assert [r["Id"] for r in mod.matching_lists({"lists": [same, other]}, item)] == [7]


def test_rich_clone_keeps_business_and_video_only_in_goal_mode():
    source = {"titles": ["a"], "business_id": 5, "video_extension_ids": [9]}
    with pytest.raises(ValueError):
        mod.clone_ad(source)
    ad = {k: v for k, v in test_audience_group.Api().ad.items()
          if k not in {"id", "campaign_id", "ad_group_id", "type", "state", "status"}}
    ad.update(business_id=5, video_extension_ids=[9])
    compiled = mod.clone_ad(ad, rich=True)
    assert compiled["BusinessId"] == 5 and compiled["VideoExtensionIds"] == [9]
