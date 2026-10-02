"""Regressions for approval items 01/02: limits and business-specific negatives."""

from copy import deepcopy

import pytest

from test_bundle import ad, source_bundle
from test_campaign_setup import plan as legacy_plan
from test_policy_audit import campaign as audit_campaign
from yadirect_mcp import audit, bundle, campaign_setup, executor, negatives, policy


@pytest.mark.parametrize("channel", ["search", "network", "maps"])
@pytest.mark.parametrize("count", [1, 3, 4])
def test_new_group_limit_on_each_channel(channel, count):
    source = source_bundle(office=channel == "maps")
    group = source["channels"][channel]["groups"][0]
    template = group["ads"][0]
    group["ads"] = [dict(deepcopy(template), hypothesis=f"Отдельная гипотеза {index}")
                    for index in range(count)]
    if count > 3:
        with pytest.raises(ValueError, match="3"):
            bundle.compile_bundle(source, "client")
    else:
        plan = bundle.compile_bundle(source, "client")
        assert plan["ready"]
        assert not any(row["rule"] == "ads.variants" for row in plan["findings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [executor.preflight, executor.apply])
async def test_saved_old_plan_cannot_write_four_ads(operation):
    plan = bundle.compile_bundle(source_bundle(), "client")
    group = plan["campaigns"][0]["groups"][0]
    group["ads"] *= 2
    # object has no network methods: failure must precede even the first read.
    with pytest.raises(ValueError, match="1 до 3"):
        await operation(object(), plan)


def test_legacy_creation_does_not_require_snapshot_and_has_ad_limit():
    source = source_bundle()
    compiled = bundle.compile_bundle(source, "client")
    campaign, _ = legacy_plan()
    campaign.pop("NegativeKeywords", None)
    group = compiled["campaigns"][0]["groups"][0]
    raw = {**group["ad_group"], "Ads": group["ads"], "Keywords": ["услуга"]}
    campaign_setup.normalize_plan(campaign, [raw])
    raw["Ads"] *= 2
    with pytest.raises(ValueError, match="не более 3"):
        campaign_setup.normalize_plan(campaign, [raw])


def test_existing_migrated_group_not_blocked_for_ad_count():
    rows = [
        {"id": index, "campaign_id": 1, "ad_group_id": 2, "type": "RESPONSIVE_AD"}
        for index in range(5)
    ]
    finding = next(
        row for row in audit._audit_ads(1, rows) if row["rule"] == "ads.responsive_group_limit"
    )
    assert finding["status"] == policy.MANUAL
    assert finding["evidence"]["groups_over_limit"] == {"2": 5}


@pytest.mark.parametrize(
    "profile,product", [("rental", "прокат"), ("education", "урок"), ("toys", "игрушка")]
)
def test_profiles_never_include_product_as_universal_negative(profile, product):
    selected = negatives.select(
        {"negative_keyword_policy": {"profile": profile, "business_terms": [product]}}
    )
    assert product not in {row["phrase"] for row in selected["selected"]}
    assert all(row["reason"] for row in selected["selected"])


def test_explicit_legacy_snapshot_protects_business_terms_and_client_exceptions():
    selected = negatives.select(
        {
            "negative_keyword_policy": {
                "profile": "legacy_reviewed",
                "business_terms": ["прокат инструмента", "бесплатная доставка"],
                "exclusions": [{"phrase": "урок", "reason": "Есть платные уроки"}],
            }
        }
    )
    phrases = {row["phrase"] for row in selected["selected"]}
    assert not {"прокат", "бесплатная", "бесплатный", "урок"} & phrases
    assert any(
        row["phrase"] == "урок" and row["reason"] == "Есть платные уроки"
        for row in selected["excluded"]
    )


def test_addition_conflict_is_reviewable_and_changes_plan_hash():
    source = source_bundle()
    original = bundle.compile_bundle(source, "client")
    source["negative_keyword_policy"] = {
        "business_terms": ["прокат инструмента"],
        "additions": [{"phrase": "прокат", "reason": "Гипотеза специалиста"}],
    }
    conflicted = bundle.compile_bundle(source, "client")
    assert not conflicted["ready"]
    assert any(row["rule"] == "search.negative_context_conflict" for row in conflicted["findings"])
    assert conflicted["plan_hash"] != original["plan_hash"]
    source["negative_keyword_policy"]["exclusions"] = [
        {"phrase": "прокат", "reason": "Целевая услуга"}
    ]
    resolved = bundle.compile_bundle(source, "client")
    assert resolved["ready"]
    assert resolved["negative_keyword_selection"]["selected"] == []


@pytest.mark.parametrize(
    "raw",
    [
        {"profile": "typo"},
        {"exclusions": ["урок"]},
        {"additions": [{"phrase": "урок", "reason": ""}]},
        {"business_terms": "игрушка"},
        {"unused": True},
    ],
)
def test_invalid_negative_policy_cannot_silently_drop_intent(raw):
    with pytest.raises(ValueError):
        negatives.select({"negative_keyword_policy": raw})


def test_form_operator_and_phrase_review_candidates():
    context = [{"kind": "business_term", "text": "Бесплатная доставка игрушек"}]
    assert negatives.conflicts(["бесплатный"], context)
    assert not negatives.conflicts(["!бесплатный", "доставка мебели"], context)
    assert negatives.conflicts(["!бесплатная"], context)


def test_empty_negatives_are_manual_and_landing_conflict_is_detected():
    campaign = audit_campaign(negative_keywords=[])
    result = audit.audit_campaign(campaign, selected_policy=policy.get())
    found = next(row for row in result["findings"] if row["rule"] == "search.negative_keywords")
    assert found["status"] == policy.MANUAL
    assert result["ready"]
    campaign["negative_keywords"] = ["прокат"]
    result = audit.audit_campaign(
        campaign,
        selected_policy=policy.get(),
        landing_pages=[
            {
                "url": "https://example.test",
                "campaigns": [campaign["id"]],
                "site_check": {"title": "Прокат инструмента"},
            }
        ],
    )
    found = next(
        row for row in result["findings"] if row["rule"] == "search.negative_context_conflict"
    )
    assert found["status"] == policy.WARNING
    assert found["evidence"]["conflicts"][0]["context"]["kind"] == "landing"


def test_group_negative_checked_only_against_its_own_offer():
    source = source_bundle()
    group = source["channels"]["search"]["groups"][0]
    group["negative_keywords"] = ["консультация"]
    group["ads"] = [ad(1)]
    plan = bundle.compile_bundle(source, "client")
    assert not plan["ready"]
    selected = plan["negative_keyword_selection"]["group_selections"][0]["selected"]
    assert selected[0]["phrase"] == "консультация"
    assert selected[0]["business_reason_requires_review"]


@pytest.mark.parametrize(
    "field,kind", [("search_queries", "search_query"), ("landing_texts", "landing")]
)
def test_supplied_query_and_landing_context_is_checked_and_labelled(field, kind):
    source = source_bundle()
    source["negative_keyword_policy"] = {
        "additions": [{"phrase": "прокат", "reason": "Кандидат специалиста"}],
        field: ["Прокат инструмента"],
    }
    plan = bundle.compile_bundle(source, "client")
    assert not plan["ready"]
    finding = next(
        row for row in plan["findings"] if row["rule"] == "search.negative_context_conflict"
    )
    context = finding["evidence"]["conflicts"][0]["context"]
    assert context == {"kind": kind, "text": "Прокат инструмента", "source": "provided_in_bundle"}
