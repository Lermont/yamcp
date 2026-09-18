"""Тесты preview/confirmation и связки ID при создании кампании с нуля."""

from __future__ import annotations

from datetime import timedelta

import pytest

from yadirect_mcp import campaign_setup, policy


def plan():
    required_negatives = sorted(
        {
            value.casefold()
            for value in policy.load_snapshot("search_negative_keywords")
        }
        - policy.RISKY_NEGATIVES
    )
    campaign = {
        "Name": "Поиск | Москва",
        # Дата берётся тем же способом, что и проверка внутри модуля: иначе на
        # машине восточнее Москвы «завтра» окажется сегодняшним днём Директа.
        "StartDate": (campaign_setup.moscow_today() + timedelta(days=1)).isoformat(),
        "NegativeKeywords": {"Items": required_negatives},
        "TextCampaign": {
            "CounterIds": {"Items": [12345]},
            "PriorityGoals": {"Items": [{"GoalId": 77, "Value": 1_000_000}]},
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": "HIGHEST_POSITION"},
                "Network": {"BiddingStrategyType": "SERVING_OFF"},
            }
        },
    }
    groups = [
        {
            "Name": "Услуга А",
            "RegionIds": [213],
            "Ads": [
                {
                    "ResponsiveAd": {
                        "Titles": [
                            "Услуга А для производственных компаний Москвы",
                            "Комплексное решение для развития вашего бизнеса",
                            "Получите консультацию и расчёт по вашему проекту",
                        ],
                        "Texts": [
                            "Оставьте заявку на сайте",
                            "Рассчитаем стоимость проекта",
                            "Ответим на вопросы по услуге",
                        ],
                        "Href": "https://example.test/a",
                    }
                }
            ],
            "Keywords": ["заказать услугу а", {"Keyword": "услуга а цена"}],
        },
        {
            "Name": "Услуга Б",
            "RegionIds": [213],
            "Ads": [
                {
                    "ResponsiveAd": {
                        "Titles": [
                            "Услуга Б для компаний и производственных предприятий",
                            "Стоимость услуги Б и условия для вашего бизнес-проекта",
                            "Закажите услугу Б с консультацией специалиста",
                        ],
                        "Texts": [
                            "Узнайте стоимость онлайн",
                            "Получите персональный расчёт",
                            "Оставьте заявку на консультацию",
                        ],
                        "Href": "https://example.test/b",
                    }
                }
            ],
            "Keywords": [],
        },
    ]
    return campaign, groups


class FakeApi:
    def __init__(self):
        self.calls = []
        self.v501_services = []

    async def call(self, service, method, params, *, client_login):
        self.calls.append((service, method, params, client_login))
        ids = {
            "campaigns": [101],
            "adgroups": [201, 202],
            "ads": [301, 302],
            "keywords": [401, 402],
        }[service]
        return {"AddResults": [{"Id": value} for value in ids]}

    async def call_v501(self, service, method, params, *, client_login):
        self.v501_services.append(service)
        return await self.call(
            service, method, params, client_login=client_login
        )




def test_raw_plan_hash_binds_login_and_payload():
    campaign, groups = plan()
    first = campaign_setup.plan_hash("client", campaign, groups)
    second = campaign_setup.plan_hash("other", campaign, groups)
    changed = dict(campaign)
    changed["Name"] = "Другое имя"
    third = campaign_setup.plan_hash("client", changed, groups)
    assert first != second
    assert first != third




def test_native_responsive_ad_is_preserved():
    campaign, groups = plan()
    groups[0]["Ads"][0] = {
        "ResponsiveAd": {
            "Titles": [
                "Профессиональная услуга для развития вашего бизнеса",
                "Комплексное решение задачи для вашей компании",
                "Консультация и расчёт проекта для вашего бизнеса",
            ],
            "Texts": [
                "Оставьте заявку на сайте",
                "Узнайте условия сотрудничества",
                "Получите консультацию",
            ],
            "Href": "https://example.test/a",
        }
    }
    _, normalized = campaign_setup.normalize_plan(campaign, groups)
    assert normalized[0]["Ads"][0] == groups[0]["Ads"][0]


def test_legacy_text_ad_is_rejected():
    campaign, groups = plan()
    groups[0]["Ads"][0] = {
        "TextAd": {
            "Title": "Услуга А в Москве",
            "Text": "Оставьте заявку на сайте",
            "Href": "https://example.test/a",
            "Mobile": "NO",
        }
    }
    with pytest.raises(ValueError, match="TextAd больше не поддерживается"):
        campaign_setup.normalize_plan(campaign, groups)


def test_raw_responsive_ad_requires_three_titles_near_the_character_limit():
    campaign, groups = plan()
    groups[0]["Ads"][0]["ResponsiveAd"]["Titles"] = [
        "Короткий заголовок один",
        "Короткий заголовок два",
        "Короткий заголовок три",
    ]
    with pytest.raises(ValueError, match="45–56 символов"):
        campaign_setup.normalize_plan(campaign, groups)


@pytest.mark.parametrize("count", [0, 1, 2, 8])
def test_raw_responsive_ad_requires_three_to_seven_titles(count):
    campaign, groups = plan()
    groups[0]["Ads"][0]["ResponsiveAd"]["Titles"] = [
        f"Вариант заголовка {index}" for index in range(count)
    ]
    with pytest.raises(ValueError, match="от 3 до 7 заголовков"):
        campaign_setup.normalize_plan(campaign, groups)


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_raw_responsive_ad_requires_exactly_three_texts(count):
    campaign, groups = plan()
    groups[0]["Ads"][0]["ResponsiveAd"]["Texts"] = [
        f"Вариант текста {index}" for index in range(count)
    ]
    with pytest.raises(ValueError, match="ровно 3 текста"):
        campaign_setup.normalize_plan(campaign, groups)


@pytest.mark.parametrize("field", ["Titles", "Texts"])
def test_raw_responsive_ad_variants_must_be_unique(field):
    campaign, groups = plan()
    values = groups[0]["Ads"][0]["ResponsiveAd"][field]
    values[1] = f" {values[0].upper()} "
    with pytest.raises(ValueError, match="уникальными"):
        campaign_setup.normalize_plan(campaign, groups)


@pytest.mark.parametrize(
    "ad",
    [
        {},
        {"TextAd": {}, "ResponsiveAd": {}},
        {"ResponsiveAd": "не объект"},
    ],
)
def test_ad_requires_exactly_one_valid_supported_format(ad):
    campaign, groups = plan()
    groups[0]["Ads"][0] = ad
    with pytest.raises(ValueError):
        campaign_setup.normalize_plan(campaign, groups)








@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c, g: c.__setitem__("StartDate", "2020-01-01"), "в прошлом"),
        (lambda c, g: g[0].__setitem__("CampaignId", 9), "CampaignId"),
        (lambda c, g: g[0]["Ads"][0].__setitem__("AdGroupId", 9), "AdGroupId"),
    ],
)
def test_validation_blocks_unsafe_parent_ids_and_past_start(mutate, message):
    campaign, groups = plan()
    mutate(campaign, groups)
    with pytest.raises(ValueError, match=message):
        campaign_setup.normalize_plan(campaign, groups)


def test_legacy_write_and_preview_entrypoints_are_removed():
    assert not hasattr(campaign_setup, "apply")
    assert not hasattr(campaign_setup, "preview")
