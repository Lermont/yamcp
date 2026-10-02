"""Тесты чтения настроек кабинета: корректировки, ретаргетинг, минус-наборы."""

from __future__ import annotations

import asyncio

import pytest

from yadirect_mcp import account


class FakeApi:
    """Ответы задаются по имени сервиса; значение-исключение будет брошено."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def call(self, service, method, params, *, client_login=None):
        self.calls.append((service, params))
        value = self.responses.get(service, {})
        if isinstance(value, Exception):
            raise value
        return value(params) if callable(value) else value

    def params_for(self, service):
        return [params for name, params in self.calls if name == service]


def read(api, **kwargs):
    return asyncio.run(account.read(api, "client1", **kwargs))


def test_campaign_ids_are_chunked_by_the_api_limit():
    """BidModifiers.get принимает не более 10 CampaignIds за вызов: 25 кампаний
    одним списком — это ошибка вызова и 20 баллов в никуда.
    """
    api = FakeApi({"bidmodifiers": {"BidModifiers": []}})
    read(api, sections=["bid_modifiers"], campaign_ids=list(range(1, 26)))

    sent = [p["SelectionCriteria"]["CampaignIds"] for p in api.params_for("bidmodifiers")]
    assert [len(chunk) for chunk in sent] == [10, 10, 5]
    assert sorted(c for chunk in sent for c in chunk) == list(range(1, 26))


def test_levels_are_always_set():
    """SelectionCriteria.Levels — обязательное поле метода."""
    api = FakeApi({"bidmodifiers": {"BidModifiers": []}})
    read(api, sections=["bid_modifiers"], campaign_ids=[1])
    criteria = api.params_for("bidmodifiers")[0]["SelectionCriteria"]
    assert criteria["Levels"] == ["CAMPAIGN", "AD_GROUP"]


def test_campaigns_are_fetched_when_ids_are_not_given():
    api = FakeApi({
        "campaigns": {"Campaigns": [{"Id": 7}, {"Id": 8}]},
        "bidmodifiers": {"BidModifiers": []},
    })
    out = read(api, sections=["bid_modifiers"])
    assert api.params_for("bidmodifiers")[0]["SelectionCriteria"]["CampaignIds"] == [7, 8]
    assert out["bid_modifiers"]["campaigns_scanned"] == 2


def test_auto_campaign_cap_is_reported_not_silent():
    """Молча урезанная выборка читается как «корректировок нет»."""
    total = account.MAX_AUTO_CAMPAIGNS + 5
    api = FakeApi({
        "campaigns": {"Campaigns": [{"Id": i} for i in range(total)]},
        "bidmodifiers": {"BidModifiers": []},
    })
    section = read(api, sections=["bid_modifiers"])["bid_modifiers"]
    assert section["campaigns_scanned"] == account.MAX_AUTO_CAMPAIGNS
    assert section["campaigns_total"] == total
    assert section["truncated"] is True


def test_nested_adjustment_is_flattened():
    api = FakeApi({"bidmodifiers": {"BidModifiers": [
        {"Id": 1, "CampaignId": 101, "Level": "CAMPAIGN", "Type": "MOBILE_ADJUSTMENT",
         "MobileAdjustment": {"BidModifier": 130, "OperatingSystemType": "IOS"}},
        {"Id": 2, "CampaignId": 101, "Level": "CAMPAIGN",
         "Type": "DEMOGRAPHICS_ADJUSTMENT",
         "DemographicsAdjustment": {"BidModifier": 0, "Enabled": "YES",
                                    "Gender": "GENDER_MALE", "Age": "AGE_18_24"}},
    ]}})
    items = read(api, sections=["bid_modifiers"], campaign_ids=[101])["bid_modifiers"]["items"]

    assert items[0]["percent"] == 130
    assert items[0]["details"] == {"OperatingSystemType": "IOS"}
    # Ноль — это «−100%», а не отсутствие корректировки: обязан доехать как есть.
    assert items[1]["percent"] == 0
    assert items[1]["enabled"] == "YES"
    assert items[1]["details"] == {"Gender": "GENDER_MALE", "Age": "AGE_18_24"}


def test_unknown_adjustment_type_still_carries_its_value():
    """Директ добавляет типы корректировок регулярно. Новый тип должен доехать
    до модели, а не превратиться в null из-за списка известных имён.
    """
    api = FakeApi({"bidmodifiers": {"BidModifiers": [
        {"Id": 9, "CampaignId": 101, "Level": "CAMPAIGN", "Type": "WEATHER_ADJUSTMENT",
         "WeatherAdjustment": {"BidModifier": 90, "Enabled": "YES",
                               "Condition": "RAIN"}},
    ]}})
    item = read(api, sections=["bid_modifiers"], campaign_ids=[101])["bid_modifiers"]["items"][0]
    assert item["percent"] == 90
    assert item["details"] == {"Condition": "RAIN"}


def test_a_failing_section_does_not_take_down_the_others():
    """Нет доступа к ретаргетингу — не повод потерять уже прочитанное."""
    api = FakeApi({
        "bidmodifiers": {"BidModifiers": []},
        "retargetinglists": RuntimeError("Нет прав на использование метода"),
        "negativekeywordsharedsets": {"NegativeKeywordSharedSets": [
            {"Id": 5, "Name": "Общий минус-набор", "NegativeKeywords": ["бесплатно"],
             "Associated": "YES"},
        ]},
    })
    out = read(api, campaign_ids=[101])

    assert "Нет прав" in out["retargeting_lists"]["error"]
    assert out["bid_modifiers"]["count"] == 0
    assert out["negative_keyword_sets"]["items"][0]["name"] == "Общий минус-набор"


def test_long_negative_set_is_previewed_with_the_full_count():
    keywords = [f"слово{i}" for i in range(account.KEYWORDS_PREVIEW + 20)]
    api = FakeApi({"negativekeywordsharedsets": {"NegativeKeywordSharedSets": [
        {"Id": 5, "Name": "Большой", "NegativeKeywords": keywords, "Associated": "NO"},
    ]}})
    item = read(api, sections=["negative_keyword_sets"])["negative_keyword_sets"]["items"][0]

    assert item["keywords_count"] == len(keywords)
    assert len(item["keywords"]) == account.KEYWORDS_PREVIEW
    assert item["keywords_truncated"] is True


def test_retargeting_lists_are_shaped_for_the_model():
    api = FakeApi({"retargetinglists": {"RetargetingLists": [
        {"Id": 3, "Name": "Были на сайте", "Type": "RETARGETING",
         "IsAvailable": "YES", "Scope": "FOR_TARGETS_AND_ADJUSTMENTS"},
    ]}})
    item = read(api, sections=["retargeting_lists"])["retargeting_lists"]["items"][0]
    assert item == {"id": 3, "name": "Были на сайте", "type": "RETARGETING",
                    "available": "YES", "scope": "FOR_TARGETS_AND_ADJUSTMENTS"}


def test_api_side_truncation_is_surfaced():
    """Директ обрезает выдачу сам и сообщает об этом полем LimitedBy. Молча
    обрезанный список читается как полный.
    """
    api = FakeApi({"retargetinglists": {
        "RetargetingLists": [{"Id": 1, "Name": "Список", "Type": "AUDIENCE"}],
        "LimitedBy": 1,
    }})
    section = read(api, sections=["retargeting_lists"])["retargeting_lists"]
    assert section["truncated"] is True
    assert section["limited_by"] == 1


def test_only_requested_sections_are_called():
    api = FakeApi({"retargetinglists": {"RetargetingLists": []}})
    out = read(api, sections=["retargeting_lists"])
    assert [name for name, _ in api.calls] == ["retargetinglists"]
    assert set(out) == {"client_login", "retargeting_lists"}


def test_unknown_section_is_rejected():
    with pytest.raises(ValueError, match="bid_modifiers"):
        read(FakeApi(), sections=["bids"])
