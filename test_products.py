"""Offline product/feed contracts, negative readback and immutable policy regressions."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from test_executor import FakeApi, readback_payloads
from test_policy_profiles import source as business_source
from yadirect_mcp import (
    ads,
    approval,
    audit,
    bundle,
    creation_report,
    executor,
    feeds,
    jobs,
    landing,
    policy,
    product_markup,
    products,
    regions,
)

FEED_URL = "https://example.test/products.xml"


def source(website=False, listing=True):
    raw = business_source("ecommerce")
    raw["policy_name"] = "ecommerce_new_v2"
    raw["client_budget"].update(amount=30000, period="monthly")
    raw["weekly_budget"] = {"search": 3500, "product": 3400}
    group = deepcopy(raw["channels"].pop("network")["groups"][0])
    group["keywords"] = []
    group["semantic"]["candidate_ids"] = []
    group["ads"] = [
        {
            "type": "shopping",
            "default_text": "Выберите товары в нашем каталоге.",
            "sitelink_set_id": 10,
            "ad_extension_ids": [11],
            "feed_filter_conditions": [
                {"Operand": "categoryId", "Operator": "EQUALS_ANY", "Arguments": ["1"]}
            ],
            "title_sources": ["name"],
            "text_sources": ["description"],
        }
    ]
    if listing:
        group["ads"].append(
            {
                "type": "listing",
                "default_text": "Каталог товаров с доставкой.",
                "sitelink_set_id": 10,
            }
        )
    src = {
        "type": "feed",
        "url": FEED_URL,
        "feed_id": 900,
        "sample_urls": ["https://example.test/service"],
    }
    if website:
        src.update(
            type="website",
            url="https://example.test/",
            site_feed_verified=True,
            review_reason="Фид создан по сайту и ID сверён в интерфейсе",
        )
    raw["channels"]["product"] = {"groups": [group], "product_source": src}
    return raw


class ProductAPI(FakeApi):
    def __init__(self, *, empty=False, feed_status="DONE", items=10):
        super().__init__()
        self.feeds = (
            []
            if empty
            else [
                {
                    "Id": 900,
                    "Name": "Товары",
                    "BusinessType": "RETAIL",
                    "SourceType": "URL",
                    "Status": feed_status,
                    "NumberOfItems": items,
                    "FilterSchema": "ECOMMERCE",
                    "TitleAndTextSources": {"Items": ["name", "description"]},
                    "UrlFeed": {"Url": FEED_URL, "RemoveUtmTags": "NO"},
                }
            ]
        )

    async def call_v501(self, service, method, params, *, client_login=None):
        if service != "feeds":
            return await super().call_v501(service, method, params, client_login=client_login)
        self.calls.append(("v501", service, method, deepcopy(params), client_login))
        if method == "get":
            ids = params.get("SelectionCriteria", {}).get("Ids")
            return {"Feeds": deepcopy([r for r in self.feeds if ids is None or r["Id"] in ids])}
        assert method == "add"
        self.feeds.append(
            {**deepcopy(params["Feeds"][0]), "Id": 900, "Status": "NEW", "NumberOfItems": None}
        )
        return {"AddResults": [{"Id": 900}]}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    async def pages(rows, **kwargs):
        return [
            {"url": r["url"], "ok": True, "status_code": 200, "product_markup": True} for r in rows
        ]

    async def public(url):
        return url, "8.8.8.8"

    monkeypatch.setattr(landing, "inspect_pages", pages)
    monkeypatch.setattr(landing, "_public_target", public)
    regions.reset_cache()


def product_plan(**kwargs):
    return bundle.compile_bundle(source(**kwargs), "client")


@pytest.mark.parametrize("website", [False, True])
@pytest.mark.parametrize("listing", [False, True])
def test_product_plan_preflight_and_apply(website, listing):
    plan = product_plan(website=website, listing=listing)
    assert plan["ready"]
    assert plan["summary"]["client_budget"]["weekly_net_total_micros"] == 6900_000000
    executor.validate_plan(plan)
    api = ProductAPI()
    check = asyncio.run(executor.preflight(api, plan))
    assert check["product_sources"]["verified"]
    result = asyncio.run(executor.apply(api, plan))
    assert result["api_creation_complete"] and not result["activated"]
    assert not result["setup_complete"]
    assert any(a["rule"] == "product.generated_offers" for a in result["required_manual_actions"])
    writes = [c for c in api.calls if c[2] != "get"]
    assert all(c[2] == "add" for c in writes)
    actual = next(c[3]["Ads"] for c in writes if c[1] == "ads")
    assert sum("ShoppingAd" in a for a in actual) == 1
    assert sum("ListingAd" in a for a in actual) == int(listing)


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "missing_feed",
        "wrong_type",
        "legacy",
        "unknown",
        "bad_filter",
        "foreign_source",
    ],
)
def test_invalid_product_plan_is_rejected(change):
    raw = source()
    channel = raw["channels"]["product"]
    ad = channel["groups"][0]["ads"][0]
    if change == "duplicate":
        channel["groups"][0]["ads"][1] = deepcopy(ad)
    elif change == "missing_feed":
        channel["product_source"].pop("feed_id")
    elif change == "wrong_type":
        ad["type"] = "responsive"
    elif change == "legacy":
        raw["policy_name"] = "ecommerce_new_v1"
    elif change == "unknown":
        ad["Href"] = "https://example.test/"
    elif change == "bad_filter":
        ad["feed_filter_conditions"][0]["Operator"] = "INJECT"
    elif change == "foreign_source":
        raw["channels"]["search"]["product_source"] = channel["product_source"]
    with pytest.raises(ValueError):
        bundle.compile_bundle(raw, "client")


@pytest.mark.parametrize(
    "status,items", [("NEW", None), ("UPDATING", 20), ("ERROR", 5), ("DONE", 0), ("DONE", None)]
)
def test_unready_feed_blocks_preflight_without_writes(status, items):
    api = ProductAPI(feed_status=status, items=items)
    with pytest.raises(ValueError, match="Фид"):
        asyncio.run(executor.preflight(api, product_plan()))
    assert all(c[2] == "get" for c in api.calls if c[0] != "v4")


def test_source_url_fields_and_website_markup_are_checked(monkeypatch):
    api = ProductAPI()
    api.feeds[0]["UrlFeed"]["Url"] += "?changed"
    with pytest.raises(ValueError, match="URL фида"):
        asyncio.run(products.check_sources(api, product_plan()))
    api = ProductAPI()
    api.feeds[0]["TitleAndTextSources"] = {"Items": []}
    with pytest.raises(ValueError, match="TitleAndTextSources"):
        asyncio.run(products.check_sources(api, product_plan()))

    async def no_markup(rows):
        return [{"url": r["url"], "ok": True, "product_markup": False} for r in rows]

    monkeypatch.setattr(landing, "inspect_pages", no_markup)
    with pytest.raises(ValueError, match="разметка"):
        asyncio.run(products.check_sources(ProductAPI(), product_plan(website=True)))


def snapshots(plan, execution):
    shadow = deepcopy(plan)
    for c in shadow["campaigns"]:
        for g in c["groups"]:
            for i, item in enumerate(g["ads"]):
                if products.kind(item):
                    g["ads"][i] = {
                        "ResponsiveAd": {
                            "Titles": [],
                            "Texts": [],
                            "SitelinkSetId": products.payload(item).get("SitelinkSetId"),
                        }
                    }
    settings, groups, actual, keys = readback_payloads(shadow, execution)
    actual_by_id = {r["id"]: r for r in actual["ads"]}
    for c in execution["campaigns"]:
        for g in c["groups"]:
            planned = plan["campaigns"][c["plan_index"]]["groups"][g["plan_index"]]
            for item, action in zip(planned["ads"], g["ads"], strict=True):
                actual_by_id[action["Id"]]["ad_group_id"] = g["id"]
                if products.kind(item):
                    key = products.kind(item)
                    body = deepcopy(item[key])
                    for field in ["FeedFilterConditions", "TitleSources", "TextSources"]:
                        if field in body:
                            body[field] = {"Items": body[field]}
                    body["AdExtensions"] = [
                        {"AdExtensionId": i} for i in body.pop("AdExtensionIds", [])
                    ]
                    body["FeedProcessingStatus"] = "PROCESSED"
                    row = ads._shape(
                        {
                            "Id": action["Id"],
                            "CampaignId": c["id"],
                            "AdGroupId": g["id"],
                            "State": "OFF",
                            "Status": "DRAFT",
                            "Type": products.READ_TYPES[key],
                            key: body,
                        }
                    )
                    actual_by_id[action["Id"]].update(row, sitelink_count=8)
    return settings, groups, actual, keys


def test_independent_readback_and_audit_accept_product_types():
    plan = product_plan()
    result = asyncio.run(executor.apply(ProductAPI(), plan))
    settings, groups, actual, keys = snapshots(plan, result)
    assert executor.compare_readback(plan, result, settings, groups, actual, keys)["verified"]
    target = [a for a in actual["ads"] if a["type"] == "SHOPPING_AD"][0]
    assert all(
        f["status"] != "BLOCK" for f in audit._audit_ads(target["campaign_id"], actual["ads"])
    )
    assert audit.classify_channel(settings["campaigns"][-1])["channel"] == "product"
    report = creation_report._render_ad({**target, "titles": []})
    assert "Товарное объявление" in report and "900" in report


@pytest.mark.parametrize(
    "field,value",
    [
        ("feed_id", 901),
        ("feed_filter_conditions", []),
        ("title_sources", []),
        ("text_sources", []),
        ("ad_extension_ids", []),
        ("texts", ["Другой текст"]),
        ("feed_processing_status", "EMPTY_RESULT"),
        ("feed_processing_status", "UNPROCESSED"),
        ("type", "RESPONSIVE_AD"),
    ],
)
def test_readback_rejects_mismatch_and_processing_failure(field, value):
    plan = product_plan()
    execution = asyncio.run(executor.apply(ProductAPI(), plan))
    settings, groups, actual, keys = snapshots(plan, execution)
    row = next(r for r in actual["ads"] if r["type"] == "SHOPPING_AD")
    row[field] = value
    assert not executor.compare_readback(plan, execution, settings, groups, actual, keys)[
        "verified"
    ]


def test_feed_creation_independent_readback_and_no_repeat():
    api = ProductAPI(empty=True)
    plan = feeds.normalize({"name": "Товары", "url": FEED_URL}, "client")
    result = asyncio.run(feeds.apply(api, plan))
    assert result["verified"] and result["feed_id"] == 900
    assert not result["ready_for_campaign"]  # NEW is not DONE.
    with pytest.raises(ValueError, match="уже есть"):
        asyncio.run(feeds.apply(api, plan))
    assert sum(c[2] == "add" for c in api.calls) == 1


def test_feed_readback_failure_retains_created_id():
    class WrongReadback(ProductAPI):
        async def call_v501(self, service, method, params, **kwargs):
            result = await super().call_v501(service, method, params, **kwargs)
            if method == "get" and result["Feeds"]:
                result["Feeds"][0]["UrlFeed"]["Url"] += "?wrong"
            return result

    result = asyncio.run(
        feeds.apply(
            WrongReadback(empty=True),
            feeds.normalize({"name": "Товары", "url": FEED_URL}, "client"),
        )
    )
    assert result["status"] == "readback_failed" and result["feed_id"] == 900


def test_unknown_feed_add_outcome_is_durable_and_never_retried(tmp_path):
    class Unknown(ProductAPI):
        async def call_v501(self, service, method, params, **kwargs):
            result = await super().call_v501(service, method, params, **kwargs)
            if method == "add":
                raise RuntimeError("connection lost after send")
            return result

    async def run():
        api = Unknown(empty=True)
        plan = feeds.normalize({"name": "Товары", "url": FEED_URL}, "client")

        async def operation(journal):
            return await feeds.apply(jobs.JournalAPI(api, journal), plan)

        job = jobs.start(tmp_path, "client", plan["plan_hash"], "feeds", operation)
        await jobs.wait_briefly(job, seconds=1)
        assert jobs.start(tmp_path, "client", plan["plan_hash"], "feeds", operation) == job
        data = json.loads((tmp_path / "jobs" / f"{job}.json").read_text(encoding="utf-8"))
        assert data["uncertain"] and data["status"] == "needs_reconciliation"
        assert sum(c[2] == "add" for c in api.calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "html,expected",
    [
        ('<script type="application/ld+json">{"@graph":[{"@type":"Product"}]}</script>', True),
        ('<div itemscope itemtype="https://schema.org/Product"></div>', True),
        ('<script type="application/ld+json">{"@type":"Organization"}</script>', False),
        ('<script type="application/ld+json">{broken Product}</script>', False),
        ('<!-- <div itemscope itemtype="https://schema.org/Product"> -->', False),
        ("<p>Product schema.org/Product</p>", False),
    ],
)
def test_product_markup_is_parsed_not_guessed(html, expected):
    assert product_markup.present(html) is expected


def test_all_previous_policy_fingerprints_are_preserved():
    baseline = Path(__file__).parent / "tests/fixtures/policy_profiles_pre_product.json"
    expected = json.loads(baseline.read_text(encoding="utf-8"))
    assert len(expected) == 7
    assert all(policy.fingerprint(policy.get(name)) == value for name, value in expected.items())


def test_feed_confirmation_is_bound_to_url_and_login():
    registry = approval.ApprovalRegistry()
    plan = feeds.normalize({"name": "Товары", "url": FEED_URL}, "client")
    token = registry.issue("client", plan["plan_hash"]).phrase
    changed = feeds.normalize({"name": "Товары", "url": FEED_URL + "?changed"}, "client")
    with pytest.raises(ValueError):
        registry.consume(token, "client", changed["plan_hash"])
    with pytest.raises(ValueError):
        registry.consume(token, "other", plan["plan_hash"])
    registry.consume(token, "client", plan["plan_hash"])
    with pytest.raises(ValueError):
        registry.consume(token, "client", plan["plan_hash"])


def test_mcp_feed_preview_consent_apply_and_replay(tmp_path):
    from test_server import _in_server

    result = _in_server(
        """import asyncio, json
from pathlib import Path
from unittest.mock import AsyncMock
import yadirect_mcp.server as s
from test_products import ProductAPI, FEED_URL
from test_approval_compatibility import host
async def public(url):
    return url, '8.8.8.8'
s.landing._public_target = public
api = ProductAPI(empty=True)
s._api = lambda: api
async def run():
    feed = {'name': 'Test feed', 'url': FEED_URL}
    preview = (await s.direct_feed_create('client', feed)).structuredContent
    before = len([c for c in api.calls if c[2] == 'add'])
    token = preview['confirmation_required']
    changed = (await s.direct_feed_create('client', {**feed, 'url': FEED_URL+'?new'},
                                         token, host())).isError
    declined = (await s.direct_feed_create('client', feed, token, host('decline'))).isError
    after_decline = len([c for c in api.calls if c[2] == 'add'])
    applied = await s.direct_feed_create('client', feed, token, host())
    repeated = (await s.direct_feed_create('client', feed, token, host())).isError
    journal = json.loads(next((Path(s.SETTINGS.out_dir)/'jobs').glob('*.json')).read_text(
        encoding='utf-8'))
    return {'before': before, 'after_decline': after_decline, 'changed': changed,
            'declined': declined, 'applied_error': applied.isError, 'repeated': repeated,
            'adds': len([c for c in api.calls if c[2] == 'add']),
            'job_kind': journal['kind'], 'approval': journal['approval']['decision'],
            'created': journal['result']['feed_id']}
print(json.dumps(asyncio.run(run())))""",
        str(tmp_path),
        YD_MODE="campaign_setup",
        YD_ALLOWED_LOGINS="client",
    )
    assert result == {
        "before": 0,
        "after_decline": 0,
        "changed": True,
        "declined": True,
        "applied_error": False,
        "repeated": True,
        "adds": 1,
        "job_kind": "feeds",
        "approval": "accept",
        "created": "900",
    }


def test_website_source_without_id_returns_explicit_ui_step(tmp_path):
    from test_server import _in_server

    result = _in_server(
        """import asyncio, json
import yadirect_mcp.server as s
async def pages(rows, **kwargs):
    return [{'url':r['url'], 'ok':True, 'product_markup':True} for r in rows]
s.landing.inspect_pages=pages
async def run():
    result = await s.direct_product_source('client', {
        'type':'website', 'url':'https://example.test/',
        'sample_urls':['https://example.test/item']})
    return {'result':result.structuredContent, 'api_created':s._client is not None}
print(json.dumps(asyncio.run(run())))""",
        str(tmp_path),
        YD_MODE="report",
        YD_ALLOWED_LOGINS="client",
    )
    assert result["result"]["ready"] is False
    assert result["result"]["product_markup"] is True
    assert result["result"]["next_action"] == "website_source_in_direct_ui"
    assert result["api_created"] is False


@pytest.mark.parametrize("problem", ["missing", "duplicate", "limited", "unrequested"])
def test_feed_read_fails_closed_on_incomplete_or_wrong_account(problem):
    class Incomplete(ProductAPI):
        async def call_v501(self, service, method, params, **kwargs):
            result = await super().call_v501(service, method, params, **kwargs)
            if problem == "missing":
                return {"Feeds": []}
            if problem == "duplicate":
                result["Feeds"] *= 2
            if problem == "limited":
                result["LimitedBy"] = 0
            if problem == "unrequested":
                result["Feeds"][0]["Id"] = 901
            return result

    with pytest.raises(ValueError):
        asyncio.run(feeds.read(Incomplete(), "client", [900]))


def test_product_history_is_separate_from_search_history():
    from test_policy_profiles import history

    raw = source()
    raw["policy_name"] = "ecommerce_established_v2"
    raw["allow_unverified_goals"] = False
    raw["goal_catalog_campaign_id"] = 55
    raw["profile_context"]["history"] = [history()]
    plan = bundle.compile_bundle(raw, "client")
    assert (
        plan["campaigns"][-1]["campaign"]["UnifiedCampaign"]["BiddingStrategy"]["Search"][
            "BiddingStrategyType"
        ]
        == "WB_MAXIMUM_CLICKS"
    )
    raw["profile_context"]["history"].append(history(channel="product"))
    plan = bundle.compile_bundle(raw, "client")
    assert (
        plan["campaigns"][-1]["campaign"]["UnifiedCampaign"]["BiddingStrategy"]["Search"][
            "BiddingStrategyType"
        ]
        == "WB_MAXIMUM_CONVERSION_RATE"
    )


@pytest.mark.parametrize("website", [False, True])
@pytest.mark.asyncio
async def test_full_product_readback_keeps_real_audit_and_source_checks(monkeypatch, website):
    plan = product_plan(website=website)
    api = ProductAPI()
    execution = await executor.apply(api, plan)
    settings, groups, actual_ads, _ = snapshots(plan, execution)
    keyword_rows, modifiers = [], []
    for executed in execution["campaigns"]:
        planned = plan["campaigns"][executed["plan_index"]]
        for item in planned.get("bid_modifiers", []):
            for row in item.get("DemographicsAdjustments", []):
                modifiers.append(
                    {
                        "campaign_id": executed["id"],
                        "ad_group_id": None,
                        "type": "DEMOGRAPHICS_ADJUSTMENT",
                        "bid_modifier": row["BidModifier"],
                        "details": row,
                    }
                )
        for group in executed["groups"]:
            expected = planned["groups"][group["plan_index"]]
            for raw, action in zip(expected["keywords"], group["keywords"], strict=True):
                keyword_rows.append(
                    executor.keywords._shape(
                        {
                            **raw,
                            "Id": action["Id"],
                            "CampaignId": executed["id"],
                            "AdGroupId": group["id"],
                        }
                    )
                )
    monkeypatch.setattr(executor.campaigns, "read_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(executor.adgroups, "read", AsyncMock(return_value=groups))
    monkeypatch.setattr(executor.ads, "read", AsyncMock(return_value=actual_ads))
    monkeypatch.setattr(
        executor, "_read_created_ids", AsyncMock(return_value={r["id"] for r in actual_ads["ads"]})
    )
    monkeypatch.setattr(
        executor.keywords, "read", AsyncMock(return_value={"keywords": keyword_rows})
    )
    monkeypatch.setattr(
        executor.account, "read", AsyncMock(return_value={"bid_modifiers": {"items": modifiers}})
    )
    result = await executor.readback(api, plan, execution)
    assert result["verified"], (
        [r for r in result["comparison"]["findings"] if r["status"] == "BLOCK"],
        [
            r
            for c in result["policy_audit"]["campaigns"]
            for r in c["findings"]
            if r["status"] == "BLOCK"
        ],
    )
    assert result["launch_checks"]["product_sources"]["verified"]
    assert result["launch_checks"]["client_budget"]["weekly_net_total_micros"] == 6900_000000
    assert any(r.get("product_urls") for r in result["objects"]["ads"])
    api.feeds[0]["Status"] = "ERROR"
    result = await executor.readback(api, plan, execution)
    assert not result["verified"] and not result["launch_checks"]["verified"]


def test_readback_rejects_unexpected_product_placements():
    plan = product_plan()
    execution = asyncio.run(executor.apply(ProductAPI(), plan))
    settings, groups, actual, keys = snapshots(plan, execution)
    settings["campaigns"][-1]["bidding_strategy"]["Search"]["PlacementTypes"]["SearchResults"] = (
        "YES"
    )
    result = executor.compare_readback(plan, execution, settings, groups, actual, keys)
    assert any(
        r["rule"] == "readback.product_placements" and r["status"] == "BLOCK"
        for r in result["findings"]
    )


@pytest.mark.asyncio
async def test_feed_transport_failure_after_add_preserves_id():
    import httpx

    class TransportFailure(ProductAPI):
        async def call_v501(self, service, method, params, **kwargs):
            if service == "feeds" and method == "get" and self.feeds:
                raise httpx.ReadTimeout("readback timed out")
            return await super().call_v501(service, method, params, **kwargs)

    api = TransportFailure(empty=True)
    plan = feeds.normalize({"name": "Товары", "url": FEED_URL}, "client")
    assert plan["summary"]["feed_url"] == FEED_URL
    result = await feeds.apply(api, plan)
    assert result["feed_id"] == 900 and result["status"] == "readback_failed"
    assert not result["verified"]
    assert sum(c[2] == "add" for c in api.calls) == 1


@pytest.mark.asyncio
async def test_ads_reader_requests_and_normalizes_product_fields():
    class ProductReadAPI:
        async def call_v501(self, service, method, params, *, client_login):
            assert (service, method, client_login) == ("ads", "get", "client")
            assert params["ShoppingAdFieldNames"] == products.READ_FIELDS
            assert params["ListingAdFieldNames"] == products.READ_FIELDS
            return {
                "Ads": [
                    {
                        "Id": 1,
                        "CampaignId": 2,
                        "AdGroupId": 3,
                        "State": "OFF",
                        "Type": "SHOPPING_AD",
                        "ShoppingAd": {
                            "FeedId": 900,
                            "FeedProcessingStatus": "PROCESSED",
                            "DefaultTexts": ["Резервный текст"],
                            "FeedFilterConditions": {
                                "Items": [
                                    {
                                        "Operand": "categoryId",
                                        "Operator": "EQUALS_ANY",
                                        "Arguments": ["1"],
                                    }
                                ]
                            },
                            "TitleSources": {"Items": ["name"]},
                            "TextSources": {"Items": ["description"]},
                        },
                    }
                ]
            }

    result = await ads.read(ProductReadAPI(), "client", campaign_ids=[2])
    row = result["ads"][0]
    assert row["feed_id"] == 900 and row["feed_processing_status"] == "PROCESSED"
    assert row["feed_filter_conditions"][0]["Arguments"] == ["1"]
    assert row["texts"] == ["Резервный текст"] and not row["href"]


@pytest.mark.parametrize("field,value", [("NumberOfItems", True), ("BusinessType", "AUTO")])
def test_product_source_rejects_wrong_catalog_type(field, value):
    api = ProductAPI()
    api.feeds[0][field] = value
    with pytest.raises(ValueError):
        asyncio.run(products.check_sources(api, product_plan()))


def test_filter_argument_limit_is_checked_before_writes():
    with pytest.raises(ValueError, match="feed_filter_conditions"):
        products.filters(
            [
                {
                    "Operand": "categoryId",
                    "Operator": "EQUALS_ANY",
                    "Arguments": [str(i) for i in range(11)],
                }
            ]
        )
