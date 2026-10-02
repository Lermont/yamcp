"""Typed regional coefficients for newly created campaigns only."""
from .identifiers import parse_id


def compile_rows(rows):
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("regional_adjustments: требуется массив до 1000 регионов")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"region_id", "bid_modifier"}:
            raise ValueError("regional_adjustments: только region_id и bid_modifier")
        region = parse_id(row["region_id"], "region_id")
        coefficient = row["bid_modifier"]
        if region <= 0 or region in seen:
            raise ValueError("regional_adjustments: положительные уникальные region_id")
        if type(coefficient) is not int or not 10 <= coefficient <= 1300:
            raise ValueError("regional_adjustments: bid_modifier от 10 до 1300, 150 = +50%")
        seen.add(region)
        result.append({"RegionId": region, "BidModifier": coefficient})
    return [{"RegionalAdjustments": result}] if result else []


def validate_actions(actions):
    rows = [r for action in actions for r in action.get("RegionalAdjustments", [])]
    compile_rows([{"region_id": r.get("RegionId"), "bid_modifier": r.get("BidModifier")}
                  for r in rows])
    if any(set(r) != {"RegionId", "BidModifier"} for r in rows):
        raise ValueError("regional_adjustments: неизвестные поля")


def matches(actions, actual, campaign_id):
    expected = sorted((r["RegionId"], r["BidModifier"]) for a in actions
                      for r in a.get("RegionalAdjustments", []))
    rows = [r for r in actual if r.get("campaign_id") == campaign_id
            and r.get("type") == "REGIONAL_ADJUSTMENT"]
    if any(r.get("ad_group_id") is not None or r.get("enabled") != "YES" for r in rows):
        return False
    observed = sorted(((r.get("details") or {}).get("RegionId"), r.get("bid_modifier"))
                      for r in rows)
    return expected == observed
