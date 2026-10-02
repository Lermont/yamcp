"""Regional coefficient compilation and exact readback, no live requests."""
from copy import deepcopy

import pytest

from test_start_defaults import current
from yadirect_mcp import bundle, executor, regional_modifiers


def test_creation_includes_coefficient_and_dictionary_scope():
    raw = current()
    raw["regional_adjustments"] = [{"region_id": 25, "bid_modifier": 150}]
    p = bundle.compile_bundle(raw, "client")
    executor.validate_plan(p)
    assert 25 in executor._planned_region_ids(p)
    for c in p["campaigns"]:
        assert c["bid_modifiers"][-1] == {
            "RegionalAdjustments": [{"RegionId": 25, "BidModifier": 150}]}
    broken = deepcopy(p)
    broken["campaigns"][0]["bid_modifiers"][-1]["RegionalAdjustments"][0]["BidModifier"] = 0
    with pytest.raises(ValueError):
        executor.validate_plan(broken)


@pytest.mark.parametrize("rows", [[{"region_id": -25, "bid_modifier": 150}],
    [{"region_id": 25, "bid_modifier": 1301}], [{"region_id": 25, "bid_modifier": True}],
    [{"region_id": 25, "bid_modifier": 150}] * 2,
    [{"region_id": 25, "bid_modifier": 150, "Enabled": "NO"}]])
def test_rejects_invalid(rows):
    with pytest.raises(ValueError):
        regional_modifiers.compile_rows(rows)


def test_exact_readback_detects_disabled_wrong_group_and_duplicates():
    actions = regional_modifiers.compile_rows([{"region_id": 25, "bid_modifier": 150}])
    row = {"campaign_id": 10, "ad_group_id": None, "type": "REGIONAL_ADJUSTMENT",
           "enabled": "YES", "details": {"RegionId": 25}, "bid_modifier": 150}
    assert regional_modifiers.matches(actions, [row], 10)
    for patch in [{"enabled": "NO"}, {"bid_modifier": 50}, {"ad_group_id": 8},
                  {"details": {"RegionId": 26}}]:
        assert not regional_modifiers.matches(actions, [{**row, **patch}], 10)
    assert not regional_modifiers.matches(actions, [row, row], 10)
    assert not regional_modifiers.matches(actions, [], 10)
