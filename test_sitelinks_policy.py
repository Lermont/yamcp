"""Shared sitelinks: compile once, validate before writes, audit real counts."""

import asyncio
from copy import deepcopy

import pytest

from test_assets import source
from test_bundle import source_bundle
from test_executor import FakeApi, compiled, readback_payloads
from yadirect_mcp import ads, assets, audit, bundle, executor, policy, regions, repair


@pytest.mark.parametrize("count", [0, 1, 3, 9])
def test_creation_rejects_counts_outside_agency_range(count):
    value = source()
    value["sitelink_sets"][0]["sitelinks"] = [
        {"title": f"Page {i}", "href": f"https://example.test/{i}"} for i in range(count)
    ]
    with pytest.raises(ValueError, match="от 4 до 8"):
        assets.normalize(value, "client")


@pytest.mark.parametrize("count", [4, 7, 8])
def test_creation_count_and_recommendation(count):
    value = source()
    value["sitelink_sets"][0]["sitelinks"] = [
        {"title": f"Page {i}", "href": f"https://example.test/{i}"} for i in range(count)
    ]
    result = assets.normalize(value, "client")
    assert result["summary"]["sitelink_counts"] == [count]
    assert bool(result["recommendations"]) == (count < 8)


def test_duplicate_titles_cannot_pad_the_minimum():
    value = source()
    value["sitelink_sets"][0]["sitelinks"][1] = value["sitelink_sets"][0]["sitelinks"][0]
    with pytest.raises(ValueError, match="дубликаты"):
        assets.normalize(value, "client")


def test_shared_defaults_precedence_without_mutating_source():
    value = source_bundle()
    value["sitelink_set_id"] = 11
    for channel in value["channels"].values():
        for group in channel["groups"]:
            for ad in group["ads"]:
                ad.pop("sitelink_set_id")
    value["channels"]["search"]["sitelink_set_id"] = 12
    group = value["channels"]["search"]["groups"][0]
    group["sitelink_set_id"] = 13
    group["ads"][0]["sitelink_set_id"] = 14
    original = deepcopy(value)
    plan = bundle.compile_bundle(value, "client")
    sets = {
        c["channel"]: [a["ResponsiveAd"]["SitelinkSetId"] for g in c["groups"] for a in g["ads"]]
        for c in plan["campaigns"]
    }
    assert sets == {"search": [14, 13], "network": [11] * 3}
    assert value == original
    group.pop("sitelink_set_id")
    plan = bundle.compile_bundle(value, "client")
    assert plan["campaigns"][0]["groups"][0]["ads"][1]["ResponsiveAd"]["SitelinkSetId"] == 12


def test_template_default_is_expanded_into_each_variant():
    value = source_bundle()
    value["campaign_template"] = value.pop("channels")["search"]
    value["campaign_template"]["sitelink_set_id"] = 55
    value["campaign_template"]["weekly_budget"] = 2500
    for ad in value["campaign_template"]["groups"][0]["ads"]:
        ad.pop("sitelink_set_id")
    value["campaign_variants"] = [
        {"channel": "search", "name_suffix": "A"},
        {"channel": "search", "name_suffix": "B", "sitelink_set_id": 66},
    ]
    plan = bundle.compile_bundle(value, "client")
    assert [
        c["groups"][0]["ads"][0]["ResponsiveAd"]["SitelinkSetId"] for c in plan["campaigns"]
    ] == [55, 66]


def test_missing_set_blocks_new_site_ad_even_when_warning_acknowledged():
    value = source_bundle()
    value["channels"]["search"]["groups"][0]["ads"][0].pop("sitelink_set_id")
    value["manual_checks"]["acknowledged_warning_rules"] = ["ads.sitelinks"]
    assert bundle.compile_bundle(value, "client")["ready"] is False


class SetApi(FakeApi):
    def __init__(self, count=8, missing=False, truncated=False):
        super().__init__()
        self.count, self.missing, self.truncated = count, missing, truncated

    async def call_v501(self, service, method, params, *, client_login=None):
        if service == "sitelinks":
            self.calls.append((service, method, params))
            return {
                "SitelinksSets": []
                if self.missing
                else [
                    {"Id": i, "Sitelinks": [{}] * self.count}
                    for i in params["SelectionCriteria"]["Ids"]
                ],
                **({"LimitedBy": 1} if self.truncated else {}),
            }
        return await super().call_v501(service, method, params, client_login=client_login)


def test_audit_fetches_shared_set_only_once_and_preserves_id():
    api = SetApi(4)
    rows = [ads._shape({"Id": i, "ResponsiveAd": {"SitelinkSetId": 55}}) for i in range(12)]
    asyncio.run(assets.enrich_ads(api, "client", rows))
    assert len(api.calls) == 1
    assert api.calls[0][2]["SelectionCriteria"] == {"Ids": [55]}
    assert all(r["sitelink_count"] == 4 and r["sitelink_set_id"] == 55 for r in rows)


@pytest.mark.parametrize(
    "count,status",
    [
        (0, policy.BLOCK),
        (3, policy.BLOCK),
        (4, policy.WARNING),
        (7, policy.WARNING),
        (8, policy.PASS),
        (9, policy.BLOCK),
        (None, policy.MANUAL),
    ],
)
def test_audit_count_status(count, status):
    findings = audit._audit_ads(
        1,
        [
            {
                "id": 2,
                "campaign_id": 1,
                "sitelinks": True,
                "sitelink_set_id": 55,
                "sitelink_count": count,
            }
        ],
    )
    assert next(f for f in findings if f["rule"] == "ads.sitelink_count")["status"] == status


@pytest.mark.parametrize("kwargs", [{"count": 3}, {"missing": True}, {"truncated": True}])
def test_preflight_rejects_small_or_unverified_set_before_writes(kwargs):
    regions.reset_cache()
    api = SetApi(**kwargs)
    with pytest.raises(ValueError):
        asyncio.run(executor.preflight(api, compiled()))
    assert not any("add" in c[:3] or "update" in c[:3] for c in api.calls)


def test_failed_get_is_unknown_not_empty_or_pass():
    api = SetApi(missing=True)
    rows = [{"sitelink_set_id": 55}]
    asyncio.run(assets.enrich_ads(api, "client", rows))
    assert rows[0]["sitelink_count"] is None
    assert rows[0]["sitelink_check_error"]


@pytest.mark.parametrize("set_id,count", [(11, 8), (10, 3), (10, None)])
def test_readback_rejects_wrong_set_small_or_unknown_count(set_id, count):
    plan = compiled()
    execution = asyncio.run(executor.apply(FakeApi(), plan))
    settings, groups, rows, keyword_ids = readback_payloads(plan, execution)
    rows["ads"][0].update(sitelink_set_id=set_id, sitelink_count=count)
    result = executor.compare_readback(plan, execution, settings, groups, rows, keyword_ids)
    assert result["verified"] is False


def test_repair_blocks_invalid_set_before_any_write():
    from test_repair import FakeApi as RepairApi
    from test_repair import source as repair_source

    class Api(RepairApi):
        async def call_v501(self, service, method, params, **kwargs):
            result = await super().call_v501(service, method, params, **kwargs)
            if service == "sitelinks":
                result["SitelinksSets"][0]["Sitelinks"] = [{}] * 3
            return result

    api = Api()
    with pytest.raises(ValueError, match="от 4 до 8"):
        asyncio.run(repair.apply(api, repair.normalize(repair_source(), "client")))
    assert api.calls == []
