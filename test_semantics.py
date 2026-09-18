"""Regression checks for the planning contract, API boundaries and truthful counts."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import timedelta

import pytest

from test_bundle import source_bundle
from test_executor import FakeApi
from yadirect_mcp import bundle, campaign_setup, creation_report, executor, policy, semantics


def compile_source(value=None):
    return bundle.compile_bundle(source_bundle() if value is None else value, "client")


def rules(plan):
    return {row["rule"] for row in plan["findings"] if row["status"] == policy.BLOCK}


def test_missing_research_is_a_draft_and_cannot_reach_write_api():
    source = source_bundle()
    source.pop("semantic_plan")
    plan = compile_source(source)
    assert not plan["ready"]
    assert "semantics.research_required" in rules(plan)
    plan["ready"] = True  # A forged ready flag must not bypass the source contract.
    api = FakeApi()
    with pytest.raises(ValueError, match="semantic_plan"):
        asyncio.run(executor.apply(api, plan))
    assert api.calls == []


def test_counts_and_api_cost_include_autotargeting_in_the_correct_place():
    plan = compile_source()
    assert plan["summary"]["keywords"] == 2
    assert plan["summary"]["autotargeting"] == 1
    assert plan["summary"]["criteria"] == 3
    assert plan["semantic_review"]["counts"]["selected_unique_phrases"] == 1
    cost = next(row for row in plan["api_units_estimate"]["services"]
                if row["service"] == "Keywords.add")
    assert cost["objects"] == 3
    result = asyncio.run(executor.apply(FakeApi(), plan))
    assert result["status"] == "complete"
    assert result["summary"]["keywords"] == {"requested": 2, "created": 2}
    assert result["summary"]["autotargeting"] == {"requested": 1, "created": 1}
    model = creation_report.build_model(plan, result)
    document = creation_report.render(model)
    assert '<span>Ключевые фразы</span><strong>2</strong>' in document


def test_source_metadata_does_not_leak_into_api_payloads():
    api = FakeApi()
    asyncio.run(executor.apply(api, compile_source()))
    payloads = json.dumps(api.calls, ensure_ascii=False)
    for field in ("semantic_plan", "business_source_ids", "hypothesis", "fixture://"):
        assert field not in payloads


def test_partial_keyword_creation_counts_successful_autotargeting_separately():
    class PartialApi(FakeApi):
        async def call_v501(self, service, method, params, *, client_login=None):
            if service == "keywords" and method == "add":
                return {"AddResults": [{"Errors": [{"Code": 1}]}, {"Id": 999}, {"Id": 1000}]}
            return await super().call_v501(service, method, params, client_login=client_login)

    result = asyncio.run(executor.apply(PartialApi(), compile_source()))
    assert result["status"] == "partial"
    assert result["summary"]["keywords"] == {"requested": 2, "created": 1}
    assert result["summary"]["autotargeting"] == {"requested": 1, "created": 1}


def test_limit_allows_200_keywords_plus_separate_autotargeting():
    rows = [{"Keyword": f"услуга {i}"} for i in range(200)]
    semantics.validate_keywords(rows + [{"Keyword": semantics.AUTOTARGETING}], "group")
    with pytest.raises(ValueError, match="200"):
        semantics.validate_keywords(rows + [{"Keyword": "услуга последняя"}], "group")


def test_compiler_rejects_1001_groups_before_building_payloads():
    source = source_bundle()
    group = source["channels"]["search"]["groups"][0]
    source["channels"]["search"]["groups"] = [group] * 1001
    with pytest.raises(ValueError, match="1000"):
        compile_source(source)


@pytest.mark.parametrize("phrase", [
    "один два три четыре пять шесть семь восемь",
    "один два три четыре пять шесть (семь восемь|девять)",
])
def test_positive_word_limit(phrase):
    with pytest.raises(ValueError, match="7 слов"):
        semantics.validate_keywords([{"Keyword": phrase}], "group")


@pytest.mark.parametrize("phrase", [
    "один два три четыре пять шесть семь -минус -!исключение",
    "один два три четыре пять шесть (семь|восемь|девять)",
    "один два три четыре пять шесть +для семи",
    '"!один !два !три !четыре !пять !шесть !семь"',
])
def test_word_limit_preserves_operators_stopwords_and_inline_negatives(phrase):
    semantics.validate_keywords([{"Keyword": phrase}], "group")


def test_character_limit_counts_minus_exclamation_as_one_character():
    phrase = "товар" + " -!минус" * 550
    assert len(phrase) > 4096 > len(phrase.replace("-!", "-"))
    semantics.validate_keywords([{"Keyword": phrase}], "group")
    with pytest.raises(ValueError, match="4096"):
        semantics.validate_keywords([{"Keyword": "товар" + " -минус" * 600}], "group")


def test_duplicates_are_rejected_but_different_operators_are_preserved():
    with pytest.raises(ValueError, match="дубликат"):
        semantics.validate_keywords([{"Keyword": "заказать услугу"},
                                     {"Keyword": "  ЗАКАЗАТЬ   УСЛУГУ "}], "group")
    semantics.validate_keywords([{"Keyword": '"заказать услугу"'},
                                 {"Keyword": "заказать услугу"}], "group")


def test_technical_limits_are_rechecked_by_executor_before_api_calls():
    plan = compile_source()
    plan["campaigns"][0]["groups"][0]["keywords"] = [
        {"Keyword": f"услуга {i}"} for i in range(201)
    ]
    api = FakeApi()
    with pytest.raises(ValueError, match="200"):
        asyncio.run(executor.apply(api, plan))
    assert api.calls == []


def test_explained_small_group_is_allowed_without_warning_acknowledgement():
    source = source_bundle()
    source["channels"].pop("network")
    source["channels"]["search"]["groups"][0]["ads"] = [
        source["channels"]["search"]["groups"][0]["ads"][0]
    ]
    assert compile_source(source)["ready"]
    source["channels"]["search"].pop("group_count_reason")
    plan = compile_source(source)
    assert not plan["ready"]
    assert any(row["rule"] == "structure.group_count" and row["status"] == "WARNING"
               for row in plan["findings"])


@pytest.mark.parametrize("mutation, expected_rule", [
    (lambda s: s["channels"]["search"]["groups"][0].pop("semantic"),
     "structure.group_rationale"),
    (lambda s: s["channels"]["search"]["groups"][0]["semantic"].update(candidate_ids=[]),
     "semantics.keyword_evidence"),
    (lambda s: s["semantic_plan"]["candidates"][0].update(decision="excluded"),
     "semantics.keyword_evidence"),
    (lambda s: s["channels"]["search"]["groups"][0]["ads"][1].pop("hypothesis"),
     "ads.distinct_hypotheses"),
    (lambda s: s["channels"]["search"]["groups"][0]["semantic"].update(business_source_ids=["ws"]),
     "semantics.business_source"),
])
def test_missing_business_or_keyword_evidence_and_ad_rationale_block(mutation, expected_rule):
    source = source_bundle()
    mutation(source)
    assert expected_rule in rules(compile_source(source))


def test_landing_comparison_ignores_tracking_but_preserves_product_parameters():
    source = source_bundle()
    ads = source["channels"]["search"]["groups"][0]["ads"]
    ads[1]["href"] += "?utm_source=yandex#section"
    assert compile_source(source)["ready"]
    ads[1]["href"] = "https://example.test/service?product=other"
    assert "structure.landing" in rules(compile_source(source))


def test_all_business_directions_must_be_accounted_for():
    source = source_bundle()
    direction = {"id": "repair", "name": "Ремонт"}
    source["semantic_plan"]["business_directions"].append(direction)
    assert "structure.coverage" in rules(compile_source(source))
    direction["excluded_reason"] = "Клиент не рекламирует ремонт на старте"
    assert compile_source(source)["ready"]


def test_history_cannot_be_declared_reviewed_without_a_query_report():
    source = source_bundle()
    source["semantic_plan"]["history"] = {
        "status": "available", "source_ids": ["ws"], "reason": "Разобраны обращения",
    }
    assert "semantics.history" in rules(compile_source(source))
    source["semantic_plan"]["sources"].append({
        "id": "queries", "type": "search_query_report", "reference": "fixture://queries.tsv",
        "collected_on": campaign_setup.moscow_today().isoformat(), "region_ids": [213],
    })
    source["semantic_plan"]["history"]["source_ids"] = ["queries"]
    assert compile_source(source)["ready"]


def test_llm_phrase_is_never_presented_as_observed_demand():
    source = source_bundle()
    source["semantic_plan"]["sources"][1]["type"] = "llm"
    candidate = source["semantic_plan"]["candidates"][0]
    with pytest.raises(ValueError, match="hypothesis_reason"):
        compile_source(source)
    candidate["hypothesis_reason"] = "Новая услуга, ограниченный тест релевантной формулировки"
    plan = compile_source(source)
    assert plan["semantic_review"]["counts"]["selected_hypotheses"] == 1
    assert plan["semantic_review"]["candidates"][0]["evidence_kind"] == "hypothesis"
    assert all(row["evidence_status"] == "provided_in_bundle"
               for row in plan["semantic_review"]["sources"])


def test_unknown_sources_future_dates_and_source_changes_are_detected():
    source = source_bundle()
    original_hash = compile_source(source)["plan_hash"]
    source["semantic_plan"]["sources"][0]["reference"] += "?revision=2"
    assert compile_source(source)["plan_hash"] != original_hash
    source["semantic_plan"]["sources"][0]["collected_on"] = (
        campaign_setup.moscow_today() + timedelta(days=1)
    ).isoformat()
    with pytest.raises(ValueError, match="будущем"):
        compile_source(source)
    source = source_bundle()
    source["semantic_plan"]["candidates"][0]["source_ids"] = ["invented"]
    with pytest.raises(ValueError, match="неизвестные ссылки"):
        compile_source(source)


def test_brand_segments_cannot_be_mixed_by_autotargeting():
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["semantic"]["brand_segment"] = "own_brand"
    assert "structure.brand_segment" in rules(compile_source(source))


def test_catalogue_profile_has_no_arbitrary_group_quota_but_requires_reason():
    source = source_bundle()
    source["semantic_plan"]["profile"] = "catalogue"
    with pytest.raises(ValueError, match="profile_reason"):
        compile_source(source)
    source["semantic_plan"]["profile_reason"] = "Структура определяется ассортиментом"
    source["channels"]["search"].pop("group_count_reason")
    assert compile_source(source)["ready"]


def test_regional_wordstat_is_not_silently_replaced_by_campaign_union(monkeypatch, tmp_path):
    plan = compile_source()
    campaign = plan["campaigns"][0]
    second = deepcopy(campaign["groups"][0])
    second["ad_group"].update(Name="Другой регион", RegionIds=[2])
    campaign["groups"].append(second)
    campaign["groups"].append(deepcopy(campaign["groups"][0]))
    calls = []

    async def lookup(api, **kwargs):
        calls.append(kwargs)
        return {"path": str(tmp_path / f"{kwargs['geo_ids'][0]}.tsv"), "phrases": [
            {"phrase": phrase, "shows": 12, "data_status": "observed"}
            for phrase in kwargs["phrases"]
        ]}

    monkeypatch.setattr(executor.wordstat, "lookup", lookup)
    rows = asyncio.run(executor.regional_wordstat(None, plan, tmp_path, 10))
    assert [call["geo_ids"] for call in calls] == [[213], [2]]
    assert [row["geo_ids"] for row in rows] == [[213], [2], [213]]
    assert all(row["status"] == "PASS" for row in rows)


def test_wordstat_zero_no_results_and_missing_are_separate(monkeypatch, tmp_path):
    plan = compile_source()
    group = plan["campaigns"][0]["groups"][0]
    group["keywords"] = [{"Keyword": key} for key in ("zero", "empty", "missing")]

    async def lookup(api, **kwargs):
        return {"path": str(tmp_path / "ws.tsv"), "phrases": [
            {"phrase": "zero", "shows": 0, "data_status": "zero"},
            {"phrase": "empty", "shows": None, "data_status": "no_results"},
        ]}

    monkeypatch.setattr(executor.wordstat, "lookup", lookup)
    row = asyncio.run(executor.regional_wordstat(None, plan, tmp_path, 10))[0]
    assert row["status"] == "MANUAL"
    assert (row["zero_count"], row["no_results_count"], row["missing_count"]) == (1, 1, 1)
    assert row["missing_preview"] == ["missing"]
    assert len(group["keywords"]) == 3  # No automatic deletion of low-frequency phrases.


def test_wordstat_failure_and_excluded_geo_do_not_claim_verified_demand(monkeypatch, tmp_path):
    plan = compile_source()

    async def fail(api, **kwargs):
        raise RuntimeError("Wordstat unavailable")

    monkeypatch.setattr(executor.wordstat, "lookup", fail)
    row = asyncio.run(executor.regional_wordstat(None, plan, tmp_path, 10))[0]
    assert row["status"] == "MANUAL" and "unavailable" in row["error"]
    plan["campaigns"][0]["groups"][0]["ad_group"]["RegionIds"] = [225, -213]
    row = asyncio.run(executor.regional_wordstat(None, plan, tmp_path, 10))[0]
    assert row["status"] == "MANUAL" and row["geo_ids"] == [-213, 225]
    assert "error" not in row  # Never sent an inexact positive-only region list.
