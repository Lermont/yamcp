"""Offline regression tests: no client credentials or network calls."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from test_bundle import source_bundle
from yadirect_mcp import bundle, group_append, repair


def source():
    base = source_bundle()
    group = deepcopy(base["channels"]["search"]["groups"][0])
    for ad in group["ads"]:
        ad.pop("action_button", None)
    group.update(campaign_id=10, region_ids=[213],
                 autotargeting=deepcopy(bundle.SAFE_AUTOTARGETING))
    return {"new_groups": [group], "semantic_plan": base["semantic_plan"]}


class FakeAPI:
    def __init__(self):
        self.writes = []
        self.fail = None
        self.truncate = None
        self.next_id = 1921551946425924090
        self.rows = {
            "campaigns": [{"Id": 10, "Name": "Old name", "Type": "UNIFIED_CAMPAIGN",
                           "State": "OFF", "Status": "DRAFT", "UnifiedCampaign": {
                               "BiddingStrategy": {
                                   "Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                                              "PlacementTypes": {"SearchResults": "YES"},
                                              "WbMaximumClicks": {"WeeklySpendLimit": 5000000000}},
                                   "Network": {"BiddingStrategyType": "SERVING_OFF"}},
                               "Settings": [{"Option": "ALTERNATIVE_TEXTS_ENABLED",
                                             "Value": "YES"}],
                               "TrackingParams": "utm_campaign={campaign_id}&ad={ad_id}"}}],
            "adgroups": [{"Id": 99, "CampaignId": 10, "Name": "Existing", "RegionIds": [213],
                          "UnifiedAdGroup": {"OfferRetargeting": "NO"}}],
            "ads": [],
            "keywords": [{"Id": 90, "CampaignId": 10, "AdGroupId": 99,
                          "Keyword": "старый запрос", "State": "OFF"}],
        }

    async def call_v501(self, service, method, params, *, client_login=None):
        assert client_login == "client"
        collection = {"campaigns": "Campaigns", "adgroups": "AdGroups", "ads": "Ads",
                      "keywords": "Keywords"}.get(service)
        if method == "get":
            if service == "sitelinks":
                return {"SitelinksSets": [{"Id": i, "Sitelinks": [
                    {"Title": str(n), "Href": "https://example.test/info"} for n in range(4)]}
                    for i in params["SelectionCriteria"]["Ids"]]}
            if service == "adextensions":
                return {"AdExtensions": [{"Id": i, "Type": "CALLOUT", "Status": "ACCEPTED"}
                                        for i in params["SelectionCriteria"]["Ids"]]}
            rows = deepcopy(self.rows[service])
            criteria = params.get("SelectionCriteria", {})
            for field, key in (("Ids", "Id"), ("CampaignIds", "CampaignId"),
                               ("AdGroupIds", "AdGroupId")):
                if criteria.get(field):
                    rows = [r for r in rows if r.get(key) in criteria[field]]
            response = {collection: rows}
            if service == self.truncate:
                response["LimitedBy"] = 1
            return response
        self.writes.append((service, method, deepcopy(params)))
        assert method in {"update", "add"}
        if self.fail == service:
            raise TimeoutError("unknown outcome")
        if method == "update":
            actions = []
            for row in params[collection]:
                target = next(r for r in self.rows[service] if r["Id"] == row["Id"])
                target.update(deepcopy(row))
                actions.append({"Id": row["Id"]})
            return {"UpdateResults": actions}
        actions = []
        for raw in params[collection]:
            self.next_id += 1
            row = {**deepcopy(raw), "Id": self.next_id, "State": "OFF", "Status": "DRAFT"}
            if service in {"ads", "keywords"}:
                owner = next(g for g in self.rows["adgroups"] if g["Id"] == row["AdGroupId"])
                row["CampaignId"] = owner["CampaignId"]
            if service == "ads":
                row["Type"] = "RESPONSIVE_AD"
                value = row["ResponsiveAd"]
                value["AdExtensions"] = [{"AdExtensionId": i}
                                         for i in value.pop("AdExtensionIds", [])]
            self.rows[service].append(row)
            actions.append({"Id": row["Id"]})
        return {"AddResults": actions}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def links(pages):
        for page in pages:
            page["site_check"] = {"ok": True, "checked": True}
        return pages

    async def geo(*args, **kwargs):
        return {"resolved": [{"id": i} for i in kwargs["ids"]]}

    monkeypatch.setattr(repair.link_checks, "check_pages", links)
    monkeypatch.setattr(group_append.regions, "lookup", geo)


def run(coro):
    return asyncio.run(coro)


def test_append_roundtrip_preserves_campaign_and_existing_objects():
    api = FakeAPI()
    saved = deepcopy(api.rows)
    plan = repair.normalize(source(), "client")
    assert plan["summary"]["new_groups"] == 1
    assert plan["summary"]["new_keywords"] == 1
    preflight = run(repair.preflight(api, plan))
    assert api.writes == []
    result = run(repair.apply(api, plan, expected_preflight=preflight))
    assert result["status"] == "complete"
    assert not result["activated"]
    assert [(s, m) for s, m, _ in api.writes] == [
        ("adgroups", "add"), ("ads", "add"), ("keywords", "add")]
    assert api.rows["campaigns"] == saved["campaigns"]
    check = run(repair.readback(api, plan, before=result["preflight"]["before"],
                               added=result["added"]))
    assert check["verified"], check
    assert check["added"]["counts"] == {"groups": 1, "ads": 2, "keywords": 2}
    assert result["added"]["groups"][0]["result"]["Id"] > 2**53


def test_rename_and_append_in_one_plan():
    api, raw = FakeAPI(), source()
    raw["campaigns"] = [{"id": "10", "name": "New name"}]
    plan = repair.normalize(raw, "client")
    result = run(repair.apply(api, plan))
    check = run(repair.readback(api, plan, before=result["preflight"]["before"],
                               added=result["added"]))
    assert check["verified"], check
    assert api.rows["campaigns"][0]["Name"] == "New name"


def test_active_campaign_append_is_explicit_draft_only_and_preserves_settings():
    api, raw = FakeAPI(), source()
    api.rows["campaigns"][0].update(State="ON", Status="ACCEPTED")
    saved = deepcopy(api.rows["campaigns"])
    original_hash = repair.normalize(raw, "client")["plan_hash"]
    raw["append_drafts_only"] = True
    plan = repair.normalize(raw, "client")
    assert plan["plan_hash"] != original_hash
    preflight = run(repair.preflight(api, plan))
    result = run(repair.apply(api, plan, expected_preflight=preflight))
    assert result["status"] == "complete" and not result["activated"]
    assert api.rows["campaigns"] == saved
    assert all(method == "add" for _, method, _ in api.writes)
    check = run(repair.readback(api, plan, before=preflight["before"], added=result["added"]))
    assert check["verified"], check
    api.rows["ads"][0]["Status"] = "MODERATION"
    check = run(repair.readback(api, plan, before=preflight["before"], added=result["added"]))
    assert any(m["reason"] == "ad_not_draft" for m in check["mismatches"])
    api.rows["ads"][0].update(Status="ACCEPTED", State="ON")
    check = run(repair.readback(api, plan, before=preflight["before"], added=result["added"]))
    assert any(m["reason"] == "ad_not_off" for m in check["mismatches"])


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_draft_append_rejects_non_true_flag(value):
    with pytest.raises(ValueError, match="append_drafts_only"):
        repair.normalize({**source(), "append_drafts_only": value}, "client")


@pytest.mark.parametrize("key", ["campaigns", "ads", "autotargetings"])
def test_draft_append_rejects_mixed_existing_object_updates(key):
    with pytest.raises(ValueError, match="несовместим"):
        repair.normalize({**source(), "append_drafts_only": True, key: []}, "client")


def test_active_draft_append_rechecks_snapshot_and_rejects_archived_campaign():
    api, raw = FakeAPI(), {**source(), "append_drafts_only": True}
    api.rows["campaigns"][0].update(State="ON", Status="ACCEPTED")
    plan = repair.normalize(raw, "client")
    preflight = run(repair.preflight(api, plan))
    api.rows["campaigns"][0]["State"] = "OFF"
    with pytest.raises(ValueError, match="Состояние изменилось"):
        run(repair.apply(api, plan, expected_preflight=preflight))
    api.rows["campaigns"][0]["State"] = "ARCHIVED"
    with pytest.raises(ValueError):
        run(repair.apply(api, plan))
    assert api.writes == []


@pytest.mark.parametrize("field,value", [("name", ""), ("name", "x" * 256),
                                         ("budget", 100), ("state", "ON")])
def test_campaign_update_rejects_invalid_or_unrelated_fields(field, value):
    with pytest.raises(ValueError):
        repair.normalize({"campaigns": [{"id": 10, field: value}]}, "client")


def test_rename_conflict_blocks_write():
    api = FakeAPI()
    api.rows["campaigns"].append({**api.rows["campaigns"][0], "Id": 20, "Name": "Taken"})
    plan = repair.normalize({"campaigns": [{"id": 10, "name": "taken"}]}, "client")
    with pytest.raises(ValueError, match="таким именем"):
        run(repair.apply(api, plan))
    assert api.writes == []


@pytest.mark.parametrize("field", ["semantic_plan", "region_ids", "autotargeting"])
def test_new_groups_require_evidence_and_explicit_targeting(field):
    raw = source()
    if field == "semantic_plan":
        raw.pop(field)
    else:
        raw["new_groups"][0].pop(field)
    with pytest.raises(ValueError):
        repair.normalize(raw, "client")


@pytest.mark.parametrize("changed", ["ON", "ARCHIVED", "network", "owner", "duplicate_name",
                                      "duplicate_keyword", "truncated"])
def test_preflight_rejects_unsafe_or_ambiguous_target(changed):
    api, raw = FakeAPI(), source()
    c = api.rows["campaigns"][0]
    if changed in {"ON", "ARCHIVED"}:
        c["State"] = changed
    elif changed == "network":
        c["UnifiedCampaign"]["BiddingStrategy"]["Network"] = {
            "BiddingStrategyType": "WB_MAXIMUM_CLICKS"}
    elif changed == "owner":
        c["Id"] = 20
    elif changed == "duplicate_name":
        api.rows["adgroups"][0]["Name"] = raw["new_groups"][0]["name"]
    elif changed == "duplicate_keyword":
        api.rows["keywords"][0]["Keyword"] = raw["new_groups"][0]["keywords"][0]
    else:
        api.truncate = "adgroups"
    with pytest.raises(ValueError):
        run(repair.apply(api, repair.normalize(raw, "client")))
    assert api.writes == []


def test_drift_between_preview_and_apply_has_no_writes():
    api, plan = FakeAPI(), repair.normalize(source(), "client")
    preflight = run(repair.preflight(api, plan))
    api.rows["keywords"][0]["Keyword"] = "изменённый запрос"
    with pytest.raises(ValueError, match="Состояние изменилось"):
        run(repair.apply(api, plan, expected_preflight=preflight))
    assert api.writes == []


def test_partial_add_keeps_group_ids_and_does_not_retry_or_add_keywords():
    api, plan = FakeAPI(), repair.normalize(source(), "client")
    api.fail = "ads"
    result = run(repair.apply(api, plan))
    assert result["status"] == "partial"
    assert result["added"]["groups"][0]["result"]["Id"]
    assert [(s, m) for s, m, _ in api.writes] == [("adgroups", "add"), ("ads", "add")]
    check = run(repair.readback(api, plan, before=result["preflight"]["before"],
                               added=result["added"]))
    assert not check["verified"]


@pytest.mark.parametrize("change", ["budget", "keyword", "group", "ad", "existing", "missing_ids"])
def test_readback_detects_incorrect_or_unproven_result(change):
    api, plan = FakeAPI(), repair.normalize(source(), "client")
    result = run(repair.apply(api, plan))
    added = result["added"]
    if change == "budget":
        api.rows["campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]["Search"][
            "WbMaximumClicks"]["WeeklySpendLimit"] = 9000000000
    elif change == "keyword":
        api.rows["keywords"][-1]["AutotargetingSettings"]["Categories"]["Broader"] = "YES"
    elif change == "group":
        api.rows["adgroups"][-1]["RegionIds"] = [2]
    elif change == "ad":
        api.rows["ads"][0]["ResponsiveAd"]["Titles"][0] = "Wrong"
    elif change == "existing":
        api.rows["adgroups"][0]["Name"] = "Changed"
    else:
        added = None
    check = run(repair.readback(api, plan, before=result["preflight"]["before"], added=added))
    assert not check["verified"]


@pytest.mark.parametrize("bad", ["name", "keyword", "ads", "regions", "research"])
def test_hash_covers_all_append_fields(bad):
    raw = source()
    original = repair.normalize(raw, "client")["plan_hash"]
    g = raw["new_groups"][0]
    if bad == "name":
        g["name"] += " вариант"
    elif bad == "keyword":
        g["keywords"][0] = "заказать новую услугу"
        raw["semantic_plan"]["candidates"][0]["phrase"] = g["keywords"][0]
    elif bad == "ads":
        g["ads"][0]["texts"][0] = "Новый текст для проверки"
    elif bad == "regions":
        g["region_ids"] = [2]
    else:
        raw["semantic_plan"]["sources"][0]["reference"] = "https://example.test/evidence"
    assert repair.normalize(raw, "client")["plan_hash"] != original


@pytest.mark.parametrize("kind", ["few", "many", "duplicate", "error"])
def test_ambiguous_add_response_stops_before_creating_children(kind):
    class Broken(FakeAPI):
        async def call_v501(self, service, method, params, **kwargs):
            result = await super().call_v501(service, method, params, **kwargs)
            if service == "adgroups" and method == "add":
                if kind == "few":
                    result["AddResults"] = []
                elif kind in {"many", "duplicate"}:
                    result["AddResults"] *= 2
                else:
                    result["AddResults"][0]["Errors"] = [{"Code": 1}]
            return result

    api = Broken()
    result = run(repair.apply(api, repair.normalize(source(), "client")))
    assert result["status"] == "partial"
    assert len(api.writes) == 1


@pytest.mark.asyncio
async def test_mcp_append_preview_consent_job_and_readonly_reverification(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from test_approval_compatibility import host
    from yadirect_mcp import approval, verification

    monkeypatch.setenv("YD_TOKEN", "dummy")
    monkeypatch.setenv("YD_OUT_DIR", str(tmp_path))
    from yadirect_mcp import server

    api = FakeAPI()
    api.last_units = None
    monkeypatch.setattr(server, "SETTINGS", SimpleNamespace(
        mode="campaign_setup", approval_mode="elicitation", out_dir=tmp_path,
        check_login=lambda login: None,
    ))
    monkeypatch.setattr(server, "_client", api)
    monkeypatch.setattr(approval, "REGISTRY", approval.ApprovalRegistry())
    raw = source()
    preview = await server.direct_campaign_repair("client", raw)
    assert not preview.is_error, preview
    summary = preview.structured_content
    assert summary["detail_omitted"] and summary["summary"]["new_groups"] == 1
    assert "new_groups" not in summary
    token = summary["confirmation_required"]
    changed = deepcopy(raw)
    changed["new_groups"][0]["name"] += " changed"
    for login, value in [("other", raw), ("client", changed)]:
        assert (await server.direct_campaign_repair(login, value, token, ctx=host())).is_error
    assert (await server.direct_campaign_repair(
        "client", raw, token, ctx=host("decline"))).is_error
    assert api.writes == []
    result = await server.direct_campaign_repair("client", raw, token, ctx=host())
    assert not result.is_error, result
    saved = result.structured_content
    assert saved["readback"]["verified"], saved
    assert (await server.direct_campaign_repair("client", raw, token, ctx=host())).is_error
    count = len(api.writes)
    check = await verification.run(api, tmp_path, "client", saved["job_id"])
    assert check["readback"]["verified"], check
    assert len(api.writes) == count


def test_keyword_readback_does_not_ignore_positive_stopword_operators():
    api, raw = FakeAPI(), source()
    phrase = "заказать услугу +в москве"
    raw["new_groups"][0]["keywords"] = [phrase]
    raw["semantic_plan"]["candidates"][0]["phrase"] = phrase
    plan = repair.normalize(raw, "client")
    result = run(repair.apply(api, plan))
    api.rows["keywords"][-2]["Keyword"] = phrase.replace("+в", "в")
    check = run(repair.readback(api, plan, before=result["preflight"]["before"],
                               added=result["added"]))
    assert not check["verified"]
    assert any(r["reason"] == "keyword" for r in check["mismatches"])
