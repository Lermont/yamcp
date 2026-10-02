"""Age exclusions and native short-term interest criteria, including partial writes."""

from copy import deepcopy

import pytest

from test_clicks_without_metrika import without_metrika
from test_executor import FakeApi, readback_payloads
from yadirect_mcp import audience_setup, bundle, executor

INTEREST = 100000000001  # Synthetic catalog ID; no client-specific audience in fixtures.


def source():
    value = without_metrika()
    value.update(age_min=25, age_max=54)
    group = value["channels"]["network"]["groups"][0]
    group.update(keywords=[], audience_interest_ids=[INTEREST], audience_priority="HIGH")
    group["semantic"]["candidate_ids"] = []
    group["semantic"]["keyword_count_reason"] = "Отдельная группа по интересам без ключей"
    return value


class Api(FakeApi):
    def __init__(self, *, fail=None):
        super().__init__()
        self.stored = {"retargetinglists": [], "audiencetargets": []}
        self.fail = fail
        self.catalog = [{"Id": INTEREST, "Name": "Тестовый интерес", "InterestType": "SHORT_TERM"}]
        self.extra_keywords = []
        self.truncated = None

    async def call_v501(self, service, method, params, *, client_login=None):
        if service == "dictionaries":
            self.calls.append(("v501", service, method, params, client_login))
            return {"AudienceInterests": self.catalog}
        if service == "keywords" and method == "get":
            return {"Keywords": self.extra_keywords}
        if service not in {*self.stored, "bidmodifiers"}:
            return await super().call_v501(service, method, params, client_login=client_login)
        self.calls.append(("v501", service, method, params, client_login))
        collection = {"retargetinglists": "RetargetingLists", "audiencetargets": "AudienceTargets",
                      "bidmodifiers": "BidModifiers"}[service]
        if method == "get":
            return {collection: deepcopy(self.stored[service]),
                    **({"LimitedBy": 10000} if self.truncated == service else {})}
        if self.fail == service:
            raise RuntimeError("Injected write failure")
        actions = []
        for item in params[collection]:
            if service == "bidmodifiers":
                ids = []
                for _ in item["DemographicsAdjustments"]:
                    self.next_id += 1
                    ids.append(self.next_id)
                actions.append({"Ids": ids})
            else:
                self.next_id += 1
                actual = {"Id": self.next_id, **deepcopy(item)}
                actual.update({"IsAvailable": "YES"} if service == "retargetinglists"
                              else {"State": "ON"})
                self.stored[service].append(actual)
                actions.append({"Id": self.next_id})
        return {"AddResults": actions}


def test_age_25_54_excludes_exactly_three_ranges():
    plan = bundle.compile_bundle(source(), "client")
    assert plan["ready"]
    for campaign in plan["campaigns"]:
        assert campaign["bid_modifiers"] == [{"DemographicsAdjustments": [
            {"Age": "AGE_0_17", "BidModifier": 0},
            {"Age": "AGE_18_24", "BidModifier": 0},
            {"Age": "AGE_55", "BidModifier": 0},
        ]}]
    assert plan["summary"]["bid_modifiers"] == 6
    assert plan["summary"]["audience_targets"] == 1
    network = plan["campaigns"][1]["groups"][0]
    assert network["keywords"] == []
    assert network["ad_group"]["UnifiedAdGroup"]["OfferRetargeting"] == "NO"
    assert network["audience"]["retargeting_list"]["Rules"] == [
        {"Operator": "ANY", "Arguments": [{"ExternalId": INTEREST}]},
    ]
    rates = {row["service"]: row for row in plan["api_units_estimate"]["services"]}
    assert rates["AudienceTargets.add"]["estimated_units"] == 12
    assert rates["RetargetingLists.add"]["estimated_units"] == 12
    assert rates["Keywords.add"]["objects"] == 2


@pytest.mark.parametrize("minimum,maximum", [(24, 54), (25, 53), (55, 54), (25.0, 54), (True, 54)])
def test_age_bounds_must_match_api_ranges(minimum, maximum):
    with pytest.raises(ValueError, match="age_"):
        audience_setup.age_modifiers(minimum, maximum)


@pytest.mark.parametrize("bad", [[], [200000000001], [INTEREST, INTEREST], [True]])
def test_long_term_or_invalid_interest_selection_is_rejected(bad):
    value = source()
    value["channels"]["network"]["groups"][0]["audience_interest_ids"] = bad
    with pytest.raises(ValueError, match="audience_interest_ids"):
        bundle.compile_bundle(value, "client")


def test_interest_group_cannot_silently_broaden_to_keywords():
    value = source()
    value["channels"]["network"]["groups"][0]["keywords"] = ["заказать услугу"]
    with pytest.raises(ValueError, match="без ключей"):
        bundle.compile_bundle(value, "client")


def test_plan_hash_binds_age_and_interest():
    value = source()
    baseline = bundle.compile_bundle(value, "client")["plan_hash"]
    value["age_max"] = 44
    assert bundle.compile_bundle(value, "client")["plan_hash"] != baseline
    value["age_max"] = 54
    value["channels"]["network"]["groups"][0]["audience_interest_ids"] = [INTEREST + 1]
    assert bundle.compile_bundle(value, "client")["plan_hash"] != baseline


@pytest.mark.asyncio
async def test_live_catalog_must_confirm_short_term_interest():
    plan = bundle.compile_bundle(source(), "client")
    api = Api()
    result = await audience_setup.preflight(api, plan)
    assert result["interests"][0]["Name"] == "Тестовый интерес"
    api.catalog = []
    with pytest.raises(ValueError, match="живом каталоге"):
        await audience_setup.preflight(api, plan)


@pytest.mark.asyncio
async def test_native_ids_array_is_counted_and_audiences_are_verified():
    plan = bundle.compile_bundle(source(), "client")
    api = Api()
    result = await executor.apply(api, plan)
    assert result["status"] == "complete"
    assert result["summary"]["bid_modifiers"] == {"created": 6, "requested": 6}
    assert result["summary"]["criteria"] == {"created": 3, "requested": 3}
    assert result["activated"] is False
    assert all(call[2] == "add" for call in api.calls)
    check = await audience_setup.readback(api, plan, result)
    assert check["verified"]
    api.stored["retargetinglists"][0]["Rules"][0]["Arguments"][0]["ExternalId"] += 1
    assert not (await audience_setup.readback(api, plan, result))["verified"]


@pytest.mark.asyncio
async def test_partial_target_failure_preserves_created_list_id_without_retry():
    plan = bundle.compile_bundle(source(), "client")
    api = Api(fail="audiencetargets")
    result = await executor.apply(api, plan)
    assert result["status"] == "partial"
    assert result["audiences"]["rows"][0]["retargeting_list"]["Id"]
    assert result["summary"]["retargeting_lists"]["created"] == 1
    assert result["summary"]["audience_targets"]["created"] == 0
    assert len([call for call in api.calls if call[1] == "audiencetargets"]) == 1


@pytest.mark.asyncio
async def test_readback_rejects_unexpected_parallel_keywords_and_truncation():
    plan = bundle.compile_bundle(source(), "client")
    api = Api()
    result = await executor.apply(api, plan)
    group_id = result["audiences"]["rows"][0]["ad_group_id"]
    api.extra_keywords = [{"Id": 9999, "AdGroupId": group_id}]
    assert not (await audience_setup.readback(api, plan, result))["verified"]
    api.truncated = "retargetinglists"
    with pytest.raises(RuntimeError, match="усекает"):
        await audience_setup.readback(api, plan, result)


@pytest.mark.asyncio
async def test_all_age_exclusions_are_required_in_readback():
    plan = bundle.compile_bundle(source(), "client")
    execution = await executor.apply(Api(), plan)
    payloads = readback_payloads(plan, execution)
    modifiers = [{"campaign_id": executed["id"], "type": "DEMOGRAPHICS_ADJUSTMENT",
                  "bid_modifier": 0, "details": {"Age": age}}
                 for executed in execution["campaigns"]
                 for age in ["AGE_0_17", "AGE_18_24", "AGE_55"]]
    result = executor.compare_readback(plan, execution, *payloads,
                                      modifiers_payload={"bid_modifiers": {"items": modifiers}})
    assert result["verified"]
    modifiers.pop()
    result = executor.compare_readback(plan, execution, *payloads,
                                      modifiers_payload={"bid_modifiers": {"items": modifiers}})
    assert not result["verified"]
    assert any(row["rule"] == "readback.age_min" and row["status"] == "BLOCK"
               for row in result["findings"])
