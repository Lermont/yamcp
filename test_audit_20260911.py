"""Audit regressions: exact IDs, durable writes, true approval and bounded HTTP."""

import asyncio
import base64
import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from test_bundle import source_bundle
from test_confirmed_audit import PNG
from test_executor import FakeApi, compiled
from test_server import _in_server
from yadirect_mcp import (
    approval,
    assets,
    audience_setup,
    bundle,
    executor,
    identifiers,
    jobs,
    keywords,
    landing,
    phrases,
    preflight_refs,
    repair,
    tracking,
)
from yadirect_mcp.client import DirectClient, DirectError

BIG = 1921019359743476961


def test_big_id_wire_is_lossless_and_non_mutating(tmp_path):
    source = {
        "Id": BIG,
        "ids": [BIG, 1],
        "count": 8,
        "ok": True,
        "CampaignIds": {"Items": [12]},
        "opaque": BIG,
    }
    out = identifiers.wire(source)
    assert out["Id"] == str(BIG) and out["ids"] == [str(BIG), "1"]
    assert out["CampaignIds"]["Items"] == ["12"]
    assert out["count"] == 8 and out["ok"] is True and source["Id"] == BIG
    result = _in_server(
        "import yadirect_mcp.server as s\nprint(s._ok({'Id': " + str(BIG) + "}).model_dump_json())",
        str(tmp_path),
    )
    assert result["structuredContent"]["Id"] == str(BIG)
    assert json.loads(result["content"][0]["text"])["Id"] == str(BIG)


@pytest.mark.parametrize("value", [True, 1.2, float(BIG), "1e3", "-1", "1.0"])
def test_lossy_id_inputs_rejected(value):
    with pytest.raises(ValueError):
        identifiers.parse_id(value)


@pytest.mark.parametrize("phrase", ["б/у", "б,у", "abc@def", "[бесплатно", "! работа", "!!работа"])
def test_invalid_campaign_and_group_negatives_fail_before_api(phrase):
    for scope in ("campaign", "group"):
        source = source_bundle()
        if scope == "campaign":
            source["additional_negative_keywords"] = [phrase]
        else:
            source["channels"]["network"]["groups"][0]["negative_keywords"] = [phrase]
        with pytest.raises(ValueError):
            bundle.compile_bundle(source, "client")


def test_readback_normalization_is_symmetric_but_not_fuzzy():
    assert phrases.equivalent(
        ["!своими руками", "бывший !в употреблении"], ["своими руками", "бывший в употреблении"]
    )
    assert phrases.equivalent(["своими руками"], ["!своими руками"])
    assert not phrases.equivalent(["своими руками", "лишняя фраза"], ["своими руками"])
    assert not phrases.equivalent(["вакцина"], ["вакансия"])
    assert not phrases.equivalent(["!работа"], ["работа"])
    assert not phrases.equivalent(['"работа дома"'], ["работа дома"])


@pytest.mark.asyncio
async def test_keywords_11_campaigns_preserves_all_results_and_global_limit():
    class Api:
        def __init__(self):
            self.calls = []

        async def call_v501(self, service, method, params, **kwargs):
            ids = params["SelectionCriteria"]["CampaignIds"]
            assert len(ids) <= 10
            self.calls.append(ids)
            return {"Keywords": [{"Id": i, "CampaignId": i, "Keyword": "word"} for i in ids]}

    api = Api()
    result = await keywords.read(api, "client", campaign_ids=[str(i) for i in range(1, 12)])
    assert result["count"] == 11 and len(api.calls) == 2 and not result["truncated"]
    api = Api()
    result = await keywords.read(api, "client", campaign_ids=list(range(1, 12)), limit=10)
    assert result["count"] == 10 and result["truncated"] and len(api.calls) == 1


@pytest.mark.asyncio
async def test_repair_batches_keep_partial_successes():
    class Api:
        def __init__(self):
            self.calls = []

        async def call_v501(self, service, method, params, **kwargs):
            if method == "get":
                return {"Campaigns": [{"Id": i, "Type": "UNIFIED_CAMPAIGN", "State": "OFF"}
                                      for i in params["SelectionCriteria"]["Ids"]]}
            rows = params["Campaigns"]
            self.calls.append(rows)
            if len(self.calls) == 2:
                return {"UpdateResults": [{"Errors": [{"Code": 5002}]}]}
            return {"UpdateResults": [{"Id": r["Id"]} for r in rows]}

    api = Api()
    plan = repair.normalize(
        {"campaigns": [{"id": i, "negative_keywords": []} for i in range(1, 12)]}, "client"
    )
    result = await repair.apply(api, plan)
    assert result["status"] == "partial" and [len(x) for x in api.calls] == [10, 1]
    assert result["updates"]["campaigns"][9]["Id"] == 10
    assert result["updates"]["campaigns"][10]["Errors"]


@pytest.mark.asyncio
async def test_client_retries_reads_but_never_writes(monkeypatch):
    settings = SimpleNamespace(
        token="dummy", sandbox=False, max_inflight=1, lang="ru", use_operator_units=True
    )
    api = DirectClient(settings)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    for method, expected in (("get", 3), ("add", 1), ("update", 1)):
        api._call_once = AsyncMock(
            side_effect=[
                DirectError("busy", code=506),
                DirectError("busy", status=503),
                {"ok": True},
            ]
        )
        if method == "get":
            assert await api.call("campaigns", method) == {"ok": True}
        else:
            with pytest.raises(DirectError):
                await api.call("campaigns", method)
        assert api._call_once.await_count == expected
    assert api._headers("client")["Use-Operator-Units"] == "true"
    await api.aclose()


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_cancel_write_and_duplicate_is_not_replayed(tmp_path):
    started, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def operation(journal):
        journal.event(stage="parent_created", Id=BIG)
        calls.append(1)
        started.set()
        await finish.wait()
        return {"status": "complete", "Id": BIG}

    job_id = jobs.start(tmp_path, "client", "hash", "apply", operation)
    await started.wait()
    waiting = asyncio.create_task(jobs.wait_briefly(job_id, seconds=60))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert jobs.read(tmp_path, job_id, "client")["events"][0]["Id"] == str(BIG)
    assert jobs.start(tmp_path, "CLIENT", "hash", "apply", operation) == job_id
    with pytest.raises(ValueError, match="job_id"):
        jobs.start(tmp_path, "client", "other", "repair", operation)
    finish.set()
    await jobs.wait_briefly(job_id, seconds=1)
    assert len(calls) == 1
    assert jobs.read(tmp_path, job_id, "client")["result"]["Id"] == str(BIG)
    with pytest.raises(PermissionError):
        jobs.read(tmp_path, job_id, "other")


@pytest.mark.asyncio
async def test_timeout_journals_request_and_reconciliation_before_exit(tmp_path):
    class Api:
        calls = []

        async def call_v501(self, service, method, params, **kwargs):
            self.calls.append(method)
            if method == "add":
                raise httpx.ReadTimeout("uncertain")
            return {"Campaigns": [{"Id": BIG, "Name": "Campaign"}]}

    api = Api()

    async def operation(journal):
        proxy = jobs.JournalAPI(api, journal)
        await proxy.call_v501(
            "campaigns", "add", {"Campaigns": [{"Name": "Campaign"}]}, client_login="client"
        )

    job_id = jobs.start(tmp_path, "client", "hash", "apply", operation)
    await jobs.wait_briefly(job_id, seconds=1)
    out = jobs.read(tmp_path, job_id, "client")
    assert out["uncertain"] and api.calls == ["add", "get"]
    assert [e["stage"] for e in out["events"]] == ["request", "unknown_outcome", "reconciliation"]
    assert out["events"][-1]["evidence"]["candidates"][0]["Id"] == str(BIG)
    with pytest.raises(ValueError):
        jobs.start(tmp_path, "client", "new", "apply", operation)


@pytest.mark.asyncio
async def test_model_token_alone_or_declined_elicitation_cannot_authorize():
    plan = {"client_login": "client", "plan_hash": "hash"}
    with pytest.raises(PermissionError):
        await approval.elicit(None, plan, "apply")
    host = SimpleNamespace(elicit=AsyncMock(return_value=SimpleNamespace(action="decline")))
    with pytest.raises(PermissionError):
        await approval.elicit(host, plan, "apply")
    host.elicit.return_value = SimpleNamespace(action="accept", data=SimpleNamespace(approve=True))
    receipt = await approval.elicit(host, plan, "apply")
    assert receipt["plan_hash"] == "hash" and receipt["mechanism"] == "mcp_elicitation"


def test_repeated_params_and_existing_url_encoding_are_preserved():
    href = "https://example.test/a%2fb?x=1&x=2&q=a%20b&signed=%2f%2F"
    assert tracking.parse_params(href)["x"] == ["1", "2"]
    result = tracking.render_url(href, "utm_term={keyword}", values={"keyword": "тест &"})
    assert result["url"].startswith(href + "&utm_term=")
    assert "%D1%82" in result["url"] and "%26" in result["url"]


@pytest.mark.asyncio
async def test_gzip_decompression_is_bounded():
    class Response:
        headers = {"content-encoding": "gzip"}

        async def aiter_raw(self, **kwargs):
            yield gzip.compress(b"x" * (landing.MAX_HTML_BYTES * 10))

    raw = await landing._bounded_body(Response())
    assert len(raw) == landing.MAX_HTML_BYTES + 1


@pytest.mark.asyncio
async def test_host_retry_after_prevents_further_requests(monkeypatch):
    monkeypatch.setattr(
        landing,
        "_public_target",
        AsyncMock(return_value=(httpx.URL("https://example.test/"), "93.184.216.34")),
    )
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "780"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(landing.RetryLater) as error:
                await landing._inspect(client, "https://example.test/")
            assert error.value.seconds >= 779
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_preflight_reuses_expensive_checks_for_exact_hash(monkeypatch):
    links = AsyncMock(return_value={"all_ok": True, "status": "PASS", "sitelinks": []})
    research = AsyncMock(return_value=[])
    monkeypatch.setattr(executor.link_checks, "check_plan", links)
    monkeypatch.setattr(executor, "regional_wordstat", research)
    executor._PREFLIGHT_CACHE.clear()
    plan = compiled()
    await executor.preflight(FakeApi(), plan, wordstat_out_dir=__import__("pathlib").Path("."))
    result = await executor.preflight(FakeApi(), plan, reuse_expensive=True)
    assert result["expensive_checks_reused"] and links.await_count == research.await_count == 1
    plan["plan_hash"] = "changed"
    await executor.preflight(FakeApi(), plan, reuse_expensive=True)
    assert links.await_count == 2


def test_price_erir_shared_negatives_and_ips_compile_with_validated_units():
    source = source_bundle()
    source["blocked_ips"] = ["1.2.3.4"]
    group = source["channels"]["search"]["groups"][0]
    group["negative_keyword_shared_set_ids"] = ["123"]
    ad = group["ads"][0]
    ad["price_extension"] = {"price": "123.45", "old_price": 200, "currency": "RUB"}
    ad["erir_ad_description"] = "Рекламируемая услуга"
    plan = bundle.compile_bundle(source, "client")
    campaign = plan["campaigns"][0]
    assert campaign["campaign"]["BlockedIps"] == {"Items": ["1.2.3.4"]}
    assert campaign["groups"][0]["ad_group"]["NegativeKeywordSharedSetIds"] == {"Items": [123]}
    assert campaign["groups"][0]["ads"][0]["ResponsiveAd"]["PriceExtension"]["Price"] == 123450000


def test_retargeting_goal_days_are_retained_and_parallel_conditions_rejected():
    source = {
        "name": "Visitors",
        "retargeting_rules": [{"operator": "ANY", "goals": [{"goal_id": "123", "days": 30}]}],
    }
    spec = audience_setup.compile_interest(source, "network")
    assert spec["retargeting_list"]["Rules"][0]["Arguments"] == [
        {"ExternalId": 123, "MembershipLifeSpan": 30}
    ]
    with pytest.raises(ValueError):
        audience_setup.compile_interest({**source, "keywords": ["phrase"]}, "network")


@pytest.mark.asyncio
async def test_currency_minimum_and_missing_business_block_before_writes():
    class Api(FakeApi):
        async def call(self, service, method, params, **kwargs):
            if service == "clients":
                return {"Clients": [{"Login": "client", "Currency": "USD"}]}
            return {
                "Currencies": [
                    {
                        "Currency": "USD",
                        "Properties": [{"Name": "MinimumWeeklySpendLimit", "Value": str(10**13)}],
                    }
                ]
            }

    with pytest.raises(ValueError, match="USD"):
        await preflight_refs.currency(Api(), compiled())
    with pytest.raises(ValueError, match="businesses"):
        await preflight_refs.assets(
            SimpleNamespace(call_v501=AsyncMock(return_value={})), "client", [{"BusinessId": BIG}]
        )


@pytest.mark.asyncio
async def test_images_use_api_batch_capacity():
    rows = [{"Name": str(i), "ImageData": base64.b64encode(PNG).decode()} for i in range(25)]
    api = SimpleNamespace(
        call_v501=AsyncMock(
            side_effect=[
                {"AddResults": [{"AdImageHash": str(i)} for i in range(25)]},
                {"AdImages": [{"AdImageHash": str(i)} for i in range(25)]},
            ]
        )
    )
    plan = {
        "client_login": "client",
        "plan_hash": "hash",
        "sitelink_sets": [],
        "callouts": [],
        "images": rows,
    }
    result = await assets.apply(api, plan)
    assert result["status"] == "complete" and api.call_v501.await_count == 2
    assert len(api.call_v501.call_args_list[0].args[2]["AdImages"]) == 25


@pytest.mark.parametrize(
    "value", ["купить (насос|компрессор)", "билеты [из москвы в париж]", "купить насос -!ремонт"]
)
def test_supported_keyword_operators(value):
    phrases.validate(value, "Keyword")


@pytest.mark.parametrize(
    "value", ["купить (насос|)", "купить (|насос)", "купить (насос]", "насос|ремонт"]
)
def test_malformed_keyword_operators(value):
    with pytest.raises(ValueError):
        phrases.validate(value, "Keyword")


def test_encoded_macros_preserve_unrelated_url_bytes():
    result = tracking.render_url(
        "https://example.com/%7Bkeyword%7D?sig=%2f%2F&k=%7bkeyword%7d",
        None,
        values={"keyword": "a b"},
    )
    assert "/a%20b?sig=%2f%2F&k=a%20b&yclid=" in result["url"]
    assert not result["unknown_macros"]
    assert tracking.render_url("https://example.com/?x=%7Bunknown%7D", None, values={})[
        "unknown_macros"
    ] == ["unknown"]


@pytest.mark.asyncio
async def test_preflight_rejected_callouts_fail_with_supported_fields():
    api = SimpleNamespace(
        call_v501=AsyncMock(
            return_value={"AdExtensions": [{"Id": 12, "Type": "CALLOUT", "Status": "REJECTED"}]}
        )
    )
    with pytest.raises(ValueError, match="отклонено"):
        await preflight_refs.assets(api, "client", [{"AdExtensionIds": [12]}])
    params = api.call_v501.call_args.args[2]
    assert "State" not in params["FieldNames"]
    assert params["SelectionCriteria"]["States"] == ["ON"]


@pytest.mark.asyncio
async def test_retry_blocked_preflight_only_before_first_write(tmp_path):
    blocked = AsyncMock(return_value={"status": "blocked"})
    job = jobs.start(tmp_path, "client", "retry-hash", "apply", blocked)
    await jobs.wait_briefly(job, 1)
    retried = AsyncMock(return_value={"status": "complete"})
    assert jobs.start(tmp_path, "client", "retry-hash", "apply", retried) == job
    await jobs.wait_briefly(job, 1)
    assert retried.await_count == 1
    assert list((tmp_path / "jobs").glob("*.attempt-*.json"))
    assert jobs.start(tmp_path, "client", "retry-hash", "apply", retried) == job
    assert retried.await_count == 1


def test_cross_minusing_reports_only_uncovered_literal_overlap():
    from yadirect_mcp import semantics

    campaign = {
        "channel": "search",
        "campaign": {"Name": "Search"},
        "groups": [
            {"ad_group": {"Name": "Broad"}, "keywords": [{"Keyword": "купить насос"}]},
            {"ad_group": {"Name": "Narrow"}, "keywords": [{"Keyword": "купить насос бочковой"}]},
        ],
    }
    result = semantics.cross_group_checks(campaign)
    assert result[0]["rule"] == "semantics.cross_minusing_review"
    assert result[0]["evidence"]["overlaps"][0]["review_negative_words"] == ["бочковой"]
    campaign["groups"][0]["ad_group"]["NegativeKeywords"] = {"Items": ["бочковой"]}
    assert semantics.cross_group_checks(campaign) == []
    campaign["groups"][1]["keywords"] = [{"Keyword": "купить насос"}]
    assert semantics.cross_group_checks(campaign)[0]["rule"] == "semantics.cross_group_duplicates"


@pytest.mark.asyncio
async def test_optional_wordstat_and_metrika_use_separate_tokens(tmp_path):
    from yadirect_mcp import wordstat

    settings = SimpleNamespace(
        token="direct-dummy",
        sandbox=False,
        max_inflight=1,
        lang="ru",
        wordstat_token="wordstat-dummy",
        metrika_token="metrika-dummy",
    )
    api = DirectClient(settings)
    await api._http.aclose()
    calls = []

    def response(request):
        calls.append(request)
        if request.url.host == "api.wordstat.yandex.net":
            assert request.method == "POST"
            assert request.headers["Authorization"] == "Bearer wordstat-dummy"
            assert json.loads(request.content) == {"phrase": "насос", "regions": [213]}
            return httpx.Response(200, json={"topRequests": [{"phrase": "насос", "count": 25}]})
        assert request.url.host == "api-metrika.yandex.net" and request.method == "GET"
        assert request.headers["Authorization"] == "OAuth metrika-dummy"
        assert request.url.path.endswith("/counter/123")
        return httpx.Response(
            200, json={"counter": {"id": 123, "status": "Active", "permission": "view"}}
        )

    api._http = httpx.AsyncClient(transport=httpx.MockTransport(response))
    result = await wordstat.lookup(
        api, phrases=["насос"], geo_ids=[213], out_dir=tmp_path, deadline_seconds=10
    )
    assert result["source"] == "wordstat_v1"
    assert result["phrases"][0]["shows"] == 25
    assert (await api.metrika_counter("123"))["id"] == 123
    assert len(calls) == 2
    await api.aclose()


@pytest.mark.asyncio
async def test_failed_wordstat_is_not_cached_for_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(
        executor.link_checks,
        "check_plan",
        AsyncMock(return_value={"all_ok": True, "status": "PASS", "sitelinks": []}),
    )
    research = AsyncMock(return_value=[{"status": "MANUAL", "error": "quota"}])
    monkeypatch.setattr(executor, "regional_wordstat", research)
    executor._PREFLIGHT_CACHE.clear()
    for _ in range(2):
        await executor.preflight(
            FakeApi(), compiled(), wordstat_out_dir=tmp_path, reuse_expensive=True
        )
    assert research.await_count == 2


@pytest.mark.asyncio
async def test_login_lock_and_saved_job_are_visible_to_another_process(tmp_path):
    gate = asyncio.Event()

    async def operation(journal):
        await gate.wait()
        return {"status": "complete"}

    job = jobs.start(tmp_path, "client", "cross-process", "apply", operation)
    code = """
import asyncio, json
import yadirect_mcp.server as s
from yadirect_mcp import jobs
async def operation(journal):
    raise AssertionError("Another process must never execute a duplicate")
async def run():
    existing = jobs.start(s.SETTINGS.out_dir, "client", "cross-process", "apply", operation)
    state = s._job_payload(existing, "client")
    try:
        jobs.start(s.SETTINGS.out_dir, "client", "different", "repair", operation)
    except ValueError as error:
        return {"job_id": existing, "state": state["status"], "locked": existing in str(error)}
    raise AssertionError("Cross-process login lock was bypassed")
print(json.dumps(asyncio.run(run())))
"""
    try:
        result = await asyncio.to_thread(_in_server, code, str(tmp_path), YD_MODE="campaign_setup")
        assert result == {"job_id": job, "state": "attention_required", "locked": True}
    finally:
        gate.set()
        await jobs.wait_briefly(job, 1)
