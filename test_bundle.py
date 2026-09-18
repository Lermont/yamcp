"""Типизированный план отдельных кампаний Search/РСЯ/Карт."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from yadirect_mcp import bundle, campaign_setup, policy, tracking


def ad(index, *, network=False):
    value = {
        "titles": [
            f"Профессиональная услуга для развития вашего бизнеса {index}",
            f"Комплексное решение задачи для вашей компании {index}",
            f"Консультация и расчёт проекта для вашего бизнеса {index}",
        ],
        "texts": [
            "Оставьте заявку на сайте",
            f"Получите консультацию по услуге {index}",
            f"Узнайте условия сотрудничества {index}",
        ],
        "href": "https://example.test/service",
        "action_button": {"text": "Узнать больше", "reason": "Условия услуги на посадочной"},
        "hypothesis": f"Отдельная гипотеза объявления {index}",
        "sitelink_set_id": 10,
        "ad_extension_ids": [20],
    }
    if network:
        value["ad_image_hashes"] = [f"hash-{index}-{n}" for n in range(3)]
    return value


def source_bundle(*, office=False):
    value = {
        "semantic_plan": {
            "profile": "small_business",
            "business_directions": [{"id": "service", "name": "Услуга"}],
            "sources": [
                {"id": "site", "type": "website", "reference": "https://example.test/service",
                 "collected_on": campaign_setup.moscow_today().isoformat(), "region_ids": []},
                {"id": "ws", "type": "wordstat", "reference": "fixture://wordstat.tsv",
                 "collected_on": campaign_setup.moscow_today().isoformat(), "region_ids": [213]},
            ],
            "history": {"status": "new_account", "reason": "Тестовый новый кабинет"},
            "candidates": [{"id": "service", "phrase": "заказать услугу",
                            "source_ids": ["ws"], "decision": "selected",
                            "reason": "Целевой запрос по услуге"}],
        },
        "name": "Клиент | Услуга",
        "start_date": (campaign_setup.moscow_today() + timedelta(days=1)).isoformat(),
        "age_min": 18,
        "weekly_budget": {"search": 3000, "network": 4000, "maps": 2000},
        "region_ids": [213],
        "counter_ids": [12345],
        "priority_goals": [{"goal_id": 77, "value": 1500}],
        "allow_unverified_goals": True,
        "office": office,
        "manual_checks": {
            "goals_reviewed": True,
            "regions_verified": True,
            "landing_pages_verified": True,
        },
        "channels": {
            "search": {
                "groups": [{
                    "name": "Услуга поиск",
                    "keywords": ["заказать услугу"],
                    "ads": [ad(1), ad(2)],
                }]
            },
            "network": {
                "groups": [{
                    "name": "Услуга РСЯ",
                    "keywords": ["заказать услугу"],
                    "ads": [ad(i, network=True) for i in range(3, 6)],
                }]
            },
        },
    }
    if office:
        value["business_profiles"] = [{
            "business_id": 999, "phone": "+7 (495) 123-45-67",
            "address": "Москва, Тестовая 1", "has_office": True,
        }]
        maps_ads = [ad(7), ad(8)]
        for item in maps_ads:
            item["business_id"] = 999
        value["channels"]["maps"] = {
            "groups": [{
                "name": "Услуга Карты",
                "keywords": ["заказать услугу"],
                "ads": maps_ads,
            }]
        }
    for channel in value["channels"].values():
        channel["group_count_reason"] = "В тестовом бизнесе одно направление"
        for group in channel["groups"]:
            group["semantic"] = {
                "direction_id": "service", "intent": "Заказать услугу", "offer": "Услуга",
                "landing_url": "https://example.test/service",
                "split_reason": "Единое предложение и покупательское намерение",
                "brand_segment": "generic", "business_source_ids": ["site"],
                "candidate_ids": ["service"], "keyword_count_reason": "Узкая тестовая семантика",
                "source_geo_reason": "Географические варианты требуют отдельного исследования",
            }
    return value


def by_channel(plan):
    return {item["channel"]: item for item in plan["campaigns"]}


def test_compiler_creates_separate_search_and_network_campaigns():
    plan = bundle.compile_bundle(source_bundle(), "client")
    items = by_channel(plan)
    assert list(items) == ["search", "network"]
    assert plan["client_login"] == "client"
    assert plan["ready"] is True

    search = items["search"]["campaign"]
    assert search["UnifiedCampaign"]["BiddingStrategy"]["Network"][
        "BiddingStrategyType"
    ] == "SERVING_OFF"
    assert search["UnifiedCampaign"]["BiddingStrategy"]["Search"][
        "PlacementTypes"
    ]["Maps"] == "NO"
    assert "NegativeKeywords" not in search
    assert plan["negative_keyword_selection"]["profile"] == "custom"
    assert plan["negative_keyword_selection"]["selected"] == []

    network = items["network"]["campaign"]
    assert network["UnifiedCampaign"]["BiddingStrategy"]["Search"][
        "BiddingStrategyType"
    ] == "SERVING_OFF"
    assert len(network["ExcludedSites"]["Items"]) == 250


def test_compiler_adds_autotargeting_and_exact_tracking_profile():
    plan = bundle.compile_bundle(source_bundle(), "client")
    search = by_channel(plan)["search"]
    autotargeting = search["groups"][0]["keywords"][-1]
    assert autotargeting == {
        "Keyword": "---autotargeting",
        "AutotargetingSettings": {
            "Categories": {
                "Exact": "YES",
                "Narrow": "YES",
                "Alternative": "NO",
                "Accessory": "NO",
                "Broader": "NO",
            },
            "BrandOptions": {
                "WithoutBrands": "YES",
                "WithAdvertiserBrand": "NO",
                "WithCompetitorsBrand": "NO",
            },
        },
    }
    actual = search["campaign"]["UnifiedCampaign"]["TrackingParams"]
    assert tracking.compare_profile(actual, policy.TRACKING_PARAMS_V1)["matches"] is True


def test_search_only_bundle_is_supported():
    source = source_bundle()
    source["channels"].pop("network")
    plan = bundle.compile_bundle(source, "client")
    assert [item["channel"] for item in plan["campaigns"]] == ["search"]
    assert plan["summary"]["campaigns"] == 1


@pytest.mark.parametrize("source_schedule", [None, "always_on", {}, {
    "days": list(range(1, 8)), "hours": list(range(24)), "bid_percent": 100,
}])
def test_compiler_uses_native_24_7_without_manual_targeting(source_schedule):
    source = source_bundle(office=True)
    if source_schedule is not None:
        source["schedule"] = source_schedule
    plan = bundle.compile_bundle(source, "client")
    assert all("TimeTargeting" not in item["campaign"] for item in plan["campaigns"])
    assert by_channel(plan)["search"]["bid_modifiers"] == [{
        "DemographicsAdjustments": [{"Age": "AGE_0_17", "BidModifier": 0}]
    }]


def test_custom_schedule_uses_api_strings_and_disables_unselected_days():
    source = source_bundle()
    source["schedule"] = {"days": [1, 2, 3, 4, 5], "hours": list(range(9, 19))}
    plan = bundle.compile_bundle(source, "client")
    targeting = by_channel(plan)["search"]["campaign"]["TimeTargeting"]
    rows = [[int(value) for value in row.split(",")] for row in targeting["Schedule"]["Items"]]
    assert len(rows) == 7
    assert rows[0] == [1] + [0] * 9 + [100] * 10 + [0] * 5
    assert rows[5] == [6] + [0] * 24
    assert rows[6] == [7] + [0] * 24
    assert "HolidaysSchedule" not in targeting


def test_all_hours_with_bid_adjustment_still_uses_custom_schedule():
    targeting = bundle._time_targeting({"bid_percent": 120})
    assert targeting["Schedule"]["Items"][0] == ",".join(map(str, [1] + [120] * 24))


def test_custom_schedule_rejects_bid_percent_not_supported_by_api():
    with pytest.raises(ValueError, match="кратно 10"):
        bundle._time_targeting({"bid_percent": 105})


def test_multiple_search_campaigns_can_split_geo_and_budget():
    source = source_bundle()
    search = source["channels"]["search"]
    source["channels"] = {
        "search": [
            {
                **search,
                "name": "Клиент | Поиск | РФ",
                "region_ids": [225],
                "weekly_budget": 10000,
                "budget_approved": True,
            },
            {
                **search,
                "name": "Клиент | Поиск | СНГ",
                "region_ids": [149, 168, 159],
                "weekly_budget": 10000,
                "budget_approved": True,
            },
        ]
    }
    source.pop("region_ids")
    plan = bundle.compile_bundle(source, "client")
    assert [item["campaign"]["Name"] for item in plan["campaigns"]] == [
        "Клиент | Поиск | РФ",
        "Клиент | Поиск | СНГ",
    ]
    assert [
        item["groups"][0]["ad_group"]["RegionIds"] for item in plan["campaigns"]
    ] == [[225], [149, 168, 159]]
    assert plan["ready"] is True
    assert sum(
        finding["rule"] == "budget.explicit_override"
        for finding in plan["findings"]
    ) == 2


def compact_geo_bundle():
    source = source_bundle()
    template = deepcopy(source["channels"]["search"])
    source.pop("channels")
    source.pop("region_ids")
    source["campaign_template"] = template
    source["campaign_variants"] = [
        {
            "channel": "search",
            "name": "Клиент | Поиск | РФ",
            "region_ids": [225],
            "weekly_budget": 10_000,
            "budget_approved": True,
        },
        {
            "channel": "search",
            "name": "Клиент | Поиск | СНГ",
            "region_ids": [149, 168, 159],
            "weekly_budget": 10_000,
            "budget_approved": True,
        },
    ]
    return source


def test_campaign_template_matches_explicit_geo_variants_and_hash():
    compact = compact_geo_bundle()
    explicit = source_bundle()
    search = deepcopy(explicit["channels"]["search"])
    explicit["channels"] = {
        "search": [
            {
                **deepcopy(search),
                "name": "Клиент | Поиск | РФ",
                "region_ids": [225],
                "weekly_budget": 10_000,
                "budget_approved": True,
            },
            {
                **deepcopy(search),
                "name": "Клиент | Поиск | СНГ",
                "region_ids": [149, 168, 159],
                "weekly_budget": 10_000,
                "budget_approved": True,
            },
        ]
    }
    explicit.pop("region_ids")

    compact_plan = bundle.compile_bundle(compact, "client")
    explicit_plan = bundle.compile_bundle(explicit, "client")

    assert compact_plan == explicit_plan
    assert compact_plan["summary"] == {
        "client_budget": {"status": "not_configured", "scope": "planned_campaigns",
                          "weekly_net_total_micros": 20_000_000_000},
        "tracking_profile": "utm_v1",
        "alternative_texts_enabled": False,
        "business_profiles_declared": 0,
        "time_zone": "Europe/Moscow",
        "weekly_budget_by_channel": {"search": 20000},
        "weekly_budget_total": 20000,
        "campaigns": 2,
        "ad_groups": 2,
        "ads": 4,
        "keywords": 2,
        "autotargeting": 2,
        "criteria": 4,
        "bid_modifiers": 2,
    }


def test_campaign_template_can_expand_across_search_and_network():
    source = source_bundle()
    source["campaign_template"] = deepcopy(source["channels"]["network"])
    source["campaign_variants"] = [
        {"channel": "search", "name": "Клиент | Поиск"},
        {"channel": "network", "name": "Клиент | РСЯ"},
    ]
    source.pop("channels")
    source["manual_checks"]["acknowledged_warning_rules"] = ["ads.variants"]

    plan = bundle.compile_bundle(source, "client")

    assert [item["channel"] for item in plan["campaigns"]] == [
        "search",
        "network",
    ]
    assert plan["campaigns"][0]["groups"][0]["ads"] == plan["campaigns"][1][
        "groups"
    ][0]["ads"]


def test_campaign_variant_cannot_override_shared_content():
    source = compact_geo_bundle()
    source["campaign_variants"][0]["groups"] = []
    with pytest.raises(ValueError, match="неизвестные поля"):
        bundle.compile_bundle(source, "client")


def test_campaign_template_rejects_duplicate_expanded_names():
    source = compact_geo_bundle()
    for variant in source["campaign_variants"]:
        variant.pop("name")
    with pytest.raises(ValueError, match="уникальные названия"):
        bundle.compile_bundle(source, "client")


def test_campaign_template_is_mutually_exclusive_with_channels():
    source = compact_geo_bundle()
    source["channels"] = {"search": deepcopy(source["campaign_template"])}
    with pytest.raises(ValueError, match="нельзя сочетать"):
        bundle.compile_bundle(source, "client")


def test_plan_estimates_successful_add_units():
    estimate = bundle.compile_bundle(source_bundle(), "client")[
        "api_units_estimate"
    ]
    assert estimate["scope"] == "successful_add_calls_only"
    assert estimate["estimated_requests"] == 5
    assert estimate["estimated_units"] == 243
    assert {row["service"]: row["estimated_units"] for row in estimate["services"]} == {
        "Campaigns.add": 20,
        "BidModifiers.add": 17,
        "AdGroups.add": 60,
        "Ads.add": 120,
        "Keywords.add": 26,
    }


def test_expanded_autotargeting_requires_warning_acknowledgement():
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["autotargeting"] = {
        "categories": {"broader": True},
        "brand_options": {"with_competitors_brand": True},
    }
    plan = bundle.compile_bundle(source, "client")
    assert plan["ready"] is False
    block = next(
        item for item in plan["findings"]
        if item["rule"] == "warnings.acknowledgement"
    )
    assert set(block["evidence"]["unacknowledged_rules"]) >= {
        "autotargeting.expanded_categories",
        "autotargeting.competitor_brands",
    }


def test_office_true_requires_and_compiles_a_separate_maps_campaign():
    source = source_bundle(office=True)
    plan = bundle.compile_bundle(source, "client")
    maps = by_channel(plan)["maps"]["campaign"]["UnifiedCampaign"][
        "BiddingStrategy"
    ]
    assert maps["Search"]["PlacementTypes"]["SearchResults"] == "NO"
    assert maps["Search"]["PlacementTypes"]["Maps"] == "YES"
    assert maps["Search"]["PlacementTypes"]["SearchOrganizationList"] == "YES"
    assert maps["Network"]["BiddingStrategyType"] == "SERVING_OFF"


def test_plan_hash_binds_login_and_payload():
    source = source_bundle()
    first = bundle.compile_bundle(source, "client-a")
    second = bundle.compile_bundle(source, "client-b")
    changed = source_bundle()
    changed["weekly_budget"]["search"] = 3500
    third = bundle.compile_bundle(changed, "client-a")
    assert len(first["plan_hash"]) == 64
    assert first["plan_hash"] != second["plan_hash"]
    assert first["plan_hash"] != third["plan_hash"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.__setitem__("office", None), "office"),
        (
            lambda value: value["manual_checks"].__setitem__("goals_reviewed", False),
            "goals_reviewed",
        ),
        (lambda value: value.__setitem__("region_ids", [-213]), "только"),
    ],
)
def test_required_business_invariants_block_compilation(mutate, message):
    source = source_bundle()
    mutate(source)
    with pytest.raises(ValueError, match=message):
        bundle.compile_bundle(source, "client")


def test_network_image_is_a_blocking_finding():
    source = source_bundle()
    del source["channels"]["network"]["groups"][0]["ads"][0]["ad_image_hashes"]
    plan = bundle.compile_bundle(source, "client")
    assert plan["ready"] is False
    assert any(
        finding["rule"] == "ads.network_image"
        and finding["status"] == policy.BLOCK
        for finding in plan["findings"]
    )


def test_budget_outside_regulation_range_is_nonblocking_notice():
    source = source_bundle()
    source["weekly_budget"]["search"] = 9000
    plan = bundle.compile_bundle(source, "client")
    assert any(f["rule"] == "budget.weekly_range" and f["status"] == policy.WARNING
               for f in plan["findings"])
    assert not any(f["rule"] == "warnings.acknowledgement" for f in plan["findings"])
    assert plan["ready"] is True
    search = by_channel(plan)["search"]["campaign"]
    assert search["UnifiedCampaign"]["BiddingStrategy"]["Search"][
        "WbMaximumClicks"
    ]["WeeklySpendLimit"] == 9_000_000_000


def test_server_default_budget_is_used_when_bundle_omits_budget():
    source = source_bundle()
    del source["weekly_budget"]
    plan = bundle.compile_bundle(source, "client", default_weekly_budget=3500)
    assert plan["ready"] is True
    assert sum(
        finding["rule"] == "budget.default_applied"
        for finding in plan["findings"]
    ) == 2


def test_maximum_conversion_rate_uses_explicit_priority_goal_without_cpa():
    source = source_bundle()
    source["channels"]["search"]["strategy"] = {
        "type": "maximum_conversion_rate",
        "goal_id": 77,
    }
    plan = bundle.compile_bundle(source, "client")
    active = by_channel(plan)["search"]["campaign"]["UnifiedCampaign"][
        "BiddingStrategy"
    ]["Search"]
    assert active["BiddingStrategyType"] == "WB_MAXIMUM_CONVERSION_RATE"
    assert active["WbMaximumConversionRate"] == {
        "WeeklySpendLimit": 3_000_000_000,
        "GoalId": 77,
    }
    assert "AverageCpa" not in active


def test_maximum_conversion_rate_rejects_goal_outside_priority_goals():
    source = source_bundle()
    source["channels"]["search"]["strategy"] = {
        "type": "maximum_conversion_rate",
        "goal_id": 999,
    }
    with pytest.raises(ValueError, match="priority_goals"):
        bundle.compile_bundle(source, "client")


@pytest.mark.parametrize("allow", [None, False, True])
@pytest.mark.parametrize("verified", [None, False, True])
def test_retired_extended_geo_inputs_do_not_block_or_change_plan(allow, verified):
    source = source_bundle()
    expected = bundle.compile_bundle(source, "client")
    if allow is not None:
        source["allow_extended_geo"] = allow
    if verified is not None:
        source["manual_checks"]["extended_geo_verified"] = verified
    plan = bundle.compile_bundle(source, "client")
    assert plan == expected
    assert plan["ready"] is True
    assert "allow_extended_geo" not in plan
    assert "extended_geo_verified" not in plan["manual_checks"]
    assert not any(row["rule"].startswith("geo.area_of_interest") for row in plan["findings"])
    assert all(
        row["Option"] != "ENABLE_AREA_OF_INTEREST_TARGETING"
        for item in plan["campaigns"]
        for row in item["campaign"]["UnifiedCampaign"]["Settings"]
    )


def test_region_confirmation_is_still_required():
    source = source_bundle()
    source["manual_checks"].pop("regions_verified")
    with pytest.raises(ValueError, match="regions_verified"):
        bundle.compile_bundle(source, "client")


def test_warning_requires_explicit_rule_acknowledgement():
    source = source_bundle()
    source["additional_negative_keywords"] = ["услуга"]
    blocked = bundle.compile_bundle(source, "client")
    assert blocked["ready"] is False
    source["manual_checks"]["acknowledged_warning_rules"] = ["search.negative_context_conflict"]
    acknowledged = bundle.compile_bundle(source, "client")
    assert acknowledged["ready"] is True


def test_priority_goal_value_is_converted_to_micros():
    plan = bundle.compile_bundle(source_bundle(), "client")
    goal = by_channel(plan)["search"]["campaign"]["UnifiedCampaign"][
        "PriorityGoals"
    ]["Items"][0]
    assert goal == {"GoalId": 77, "Value": 1_500_000_000}


def test_ad_extensions_use_api_array_shape():
    plan = bundle.compile_bundle(source_bundle(), "client")
    responsive_ad = by_channel(plan)["search"]["groups"][0]["ads"][0][
        "ResponsiveAd"
    ]
    assert responsive_ad["AdExtensionIds"] == [20]


def test_compiler_uses_responsive_ads_required_by_current_unified_campaign_api():
    plan = bundle.compile_bundle(source_bundle(), "client")
    responsive_ad = by_channel(plan)["network"]["groups"][0]["ads"][0]
    assert "TextAd" not in responsive_ad
    payload = responsive_ad["ResponsiveAd"]
    assert payload["Titles"] == [
        "Профессиональная услуга для развития вашего бизнеса 3",
        "Комплексное решение задачи для вашей компании 3",
        "Консультация и расчёт проекта для вашего бизнеса 3",
    ]
    assert payload["Texts"] == [
        "Оставьте заявку на сайте",
        "Получите консультацию по услуге 3",
        "Узнайте условия сотрудничества 3",
    ]
    assert payload["AdImageHashes"] == ["hash-3-0", "hash-3-1", "hash-3-2"]


def test_compiler_accepts_native_responsive_ad_arrays():
    source = source_bundle()
    item = source["channels"]["search"]["groups"][0]["ads"][0]
    item["titles"] = [
        "Профессиональная услуга для развития вашего бизнеса",
        "Комплексное решение задачи для вашей компании",
        "Консультация и расчёт проекта для вашего бизнеса",
        "Короткий дополнительный вариант",
    ]
    item["texts"] = ["Первый текст", "Второй текст", "Третий текст"]
    payload = by_channel(bundle.compile_bundle(source, "client"))["search"][
        "groups"
    ][0]["ads"][0]["ResponsiveAd"]
    assert payload["Titles"] == item["titles"]
    assert payload["Texts"] == item["texts"]


def test_responsive_ad_requires_three_titles_near_the_character_limit():
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["ads"][0]["titles"] = [
        "Короткий заголовок один",
        "Короткий заголовок два",
        "Короткий заголовок три",
        "Короткий заголовок четыре",
        "Короткий заголовок пять",
    ]
    with pytest.raises(ValueError, match="45–56 символов"):
        bundle.compile_bundle(source, "client")


@pytest.mark.parametrize("count", [0, 1, 2, 8])
def test_responsive_ad_requires_three_to_seven_titles(count):
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["ads"][0]["titles"] = [
        f"Вариант заголовка {index}" for index in range(count)
    ]
    with pytest.raises(ValueError, match="от 3 до 7 заголовков"):
        bundle.compile_bundle(source, "client")


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_responsive_ad_requires_exactly_three_texts(count):
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["ads"][0]["texts"] = [
        f"Вариант текста {index}" for index in range(count)
    ]
    with pytest.raises(ValueError, match="ровно 3 текста"):
        bundle.compile_bundle(source, "client")


@pytest.mark.parametrize("field", ["titles", "texts"])
def test_responsive_ad_variants_must_be_unique(field):
    source = source_bundle()
    item = source["channels"]["search"]["groups"][0]["ads"][0]
    item[field][1] = f" {item[field][0].upper()} "
    with pytest.raises(ValueError, match="уникальными"):
        bundle.compile_bundle(source, "client")


def test_tracking_params_do_not_include_url_question_mark():
    plan = bundle.compile_bundle(source_bundle(), "client")
    value = by_channel(plan)["search"]["campaign"]["UnifiedCampaign"][
        "TrackingParams"
    ]
    assert "=" in value
    assert not value.startswith("?")


def test_additional_negatives_and_sites_extend_versioned_snapshots():
    source = source_bundle()
    source["additional_negative_keywords"] = ["не наша услуга"]
    source["additional_excluded_sites"] = ["custom.example"]
    plan = bundle.compile_bundle(source, "client")
    items = by_channel(plan)
    assert "не наша услуга" in items["search"]["campaign"][
        "NegativeKeywords"
    ]["Items"]
    assert "custom.example" in items["network"]["campaign"][
        "ExcludedSites"
    ]["Items"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("weeky_budget", 3000),
        lambda value: value["channels"]["search"].__setitem__("stratgey", {}),
        lambda value: value["channels"]["search"]["groups"][0].__setitem__(
            "regoin_ids", [213]
        ),
        lambda value: value["channels"]["search"]["groups"][0]["ads"][0].__setitem__(
            "titel", "Опечатка"
        ),
    ],
)
def test_unknown_fields_are_rejected_instead_of_silently_ignored(mutate):
    source = source_bundle()
    mutate(source)
    with pytest.raises(ValueError, match="неизвестные поля"):
        bundle.compile_bundle(source, "client")


def test_ad_href_must_be_absolute_http_url():
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["ads"][0]["href"] = "/relative"
    with pytest.raises(ValueError, match="HTTP"):
        bundle.compile_bundle(source, "client")


def test_responsive_ad_may_use_business_id_without_href():
    source = source_bundle()
    item = source["channels"]["search"]["groups"][0]["ads"][0]
    item.pop("href")
    item.pop("action_button")
    item.pop("sitelink_set_id")
    item["business_id"] = 123
    source["business_profiles"] = [{"business_id": 123, "phone": "+74951234567",
                                    "address": None, "has_office": False}]
    payload = by_channel(bundle.compile_bundle(source, "client"))["search"][
        "groups"
    ][0]["ads"][0]["ResponsiveAd"]
    assert payload["BusinessId"] == 123
    assert "Href" not in payload


@pytest.mark.parametrize("value", ["с пробелом", "с_подчеркиванием", "два--дефиса", "два//слеша"])
def test_display_url_path_rejects_api_forbidden_values(value):
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["ads"][0][
        "display_url_path"
    ] = value
    with pytest.raises(ValueError, match="запрещённые"):
        bundle.compile_bundle(source, "client")


def test_campaign_and_group_names_respect_api_limit():
    source = source_bundle()
    source["name"] = "К" * 250
    with pytest.raises(ValueError, match="255"):
        bundle.compile_bundle(source, "client")
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["name"] = "Г" * 256
    with pytest.raises(ValueError, match="255"):
        bundle.compile_bundle(source, "client")


def test_negative_phrases_respect_direct_limits():
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["negative_keywords"] = [
        "раз два три четыре пять шесть семь восемь"
    ]
    with pytest.raises(ValueError, match="7 слов"):
        bundle.compile_bundle(source, "client")


def test_responsive_text_allows_documented_narrow_character_allowance():
    source = source_bundle()
    item = source["channels"]["search"]["groups"][0]["ads"][0]
    item["texts"][0] = "аааа! " * 14 + "а" * 11 + "!"
    payload = by_channel(bundle.compile_bundle(source, "client"))["search"][
        "groups"
    ][0]["ads"][0]["ResponsiveAd"]
    assert payload["Texts"] == item["texts"]
