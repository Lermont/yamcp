"""Тесты чтения объявлений: посадочные страницы, UTM, архив, нетекстовые типы."""

from __future__ import annotations

import asyncio

import pytest

from yadirect_mcp import ads


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


def ad(ad_id, campaign_id=100, href="https://shop.ru/", state="ON", **text):
    """Ответ ads.get в том виде, в каком его отдаёт Директ."""
    text_ad = {"Title": f"Заголовок {ad_id}", "Text": "Текст", **text}
    if href is not None:
        text_ad["Href"] = href
    return {
        "Id": ad_id,
        "CampaignId": campaign_id,
        "AdGroupId": campaign_id + 1,
        "Type": "TEXT_AD",
        "State": state,
        "Status": "ACCEPTED",
        "TextAd": text_ad,
    }


def fake(ad_list=(), campaigns=(100,), **extra):
    """Клиент с кампаниями: без них критерий ads.get невалиден."""
    return FakeApi({
        "campaigns": {"Campaigns": [{"Id": c} for c in campaigns]},
        "ads": {"Ads": list(ad_list), **extra},
    })


def read(api, **kwargs):
    return asyncio.run(ads.read(api, "client1", **kwargs))


# ── формирование запроса ─────────────────────────────────────────────────


def test_text_ad_fields_are_requested():
    """Без TextAdFieldNames Директ вернёт объявления вообще без Href — и это
    молчаливый провал: ответ выглядит валидным, ссылок просто нет.
    """
    api = fake()
    read(api)
    params = api.params_for("ads")[0]
    assert "Href" in params["TextAdFieldNames"]
    assert "Id" in params["FieldNames"]


def test_campaigns_are_fetched_when_selection_is_not_narrowed():
    """Пустой SelectionCriteria метод отвергает: нужен хотя бы один из Ids,
    AdGroupIds, CampaignIds (error_code=4001).
    """
    api = fake(campaigns=(7, 8))
    read(api)
    assert api.params_for("ads")[0]["SelectionCriteria"] == {"CampaignIds": [7, 8]}


def test_campaign_ids_are_chunked_by_the_api_limit():
    """ads.get принимает не более 10 CampaignIds за вызов."""
    api = fake(campaigns=tuple(range(1, 26)))
    read(api)

    sent = [p["SelectionCriteria"]["CampaignIds"] for p in api.params_for("ads")]
    assert [len(chunk) for chunk in sent] == [10, 10, 5]
    assert sorted(c for chunk in sent for c in chunk) == list(range(1, 26))


def test_selection_criteria_carries_only_given_filters():
    """States в критерий не уходит: неверное значение перечисления — это
    ошибка вызова и 20 баллов, а State приходит в каждом объекте и так.
    """
    api = fake()
    read(api, campaign_ids=[7, 8])
    assert api.params_for("ads")[0]["SelectionCriteria"] == {"CampaignIds": [7, 8]}
    assert api.params_for("campaigns") == []


def test_ad_group_ids_skip_the_campaign_lookup():
    """Группы уже задают выборку — лишний вызов campaigns.get это лишние баллы."""
    api = fake()
    read(api, ad_group_ids=[70, 71])
    assert api.params_for("campaigns") == []
    assert api.params_for("ads")[0]["SelectionCriteria"] == {"AdGroupIds": [70, 71]}


def test_client_without_campaigns_does_not_call_ads():
    api = fake(campaigns=())
    out = read(api)
    assert api.params_for("ads") == []
    assert out["count"] == 0
    assert "нет кампаний" in out["note"]


def test_auto_campaign_cap_is_reported_not_silent():
    """Молча урезанная выборка читается как «это все объявления аккаунта»."""
    total = ads.MAX_AUTO_CAMPAIGNS + 5
    api = fake(campaigns=tuple(range(total)))
    out = read(api)

    assert out["campaigns_scanned"] == ads.MAX_AUTO_CAMPAIGNS
    assert out["campaigns_total"] == total
    assert out["truncated"] is True


@pytest.mark.parametrize("limit", [0, -1, ads.MAX_LIMIT + 1])
def test_limit_is_validated_before_the_call(limit):
    api = fake()
    with pytest.raises(ValueError):
        read(api, limit=limit)
    assert api.calls == []


# ── разбор ответа ────────────────────────────────────────────────────────


def test_landing_pages_group_ads_by_url():
    api = fake([
        ad(1, campaign_id=100, href="https://shop.ru/"),
        ad(2, campaign_id=200, href="https://shop.ru/"),
        ad(3, campaign_id=200, href="https://shop.ru/bukety"),
    ])
    out = read(api)

    pages = out["landing_pages"]
    assert [p["url"] for p in pages] == ["https://shop.ru/", "https://shop.ru/bukety"]
    assert pages[0]["ads"] == 2
    assert pages[0]["campaigns"] == [100, 200]
    assert out["domains"] == ["shop.ru"]


def test_utm_is_parsed_and_missing_marks_are_counted():
    """«Метки есть» и «метки работают» — разные вещи: динамический параметр
    Директа должен доехать до модели шаблоном, а не подставленным значением.
    """
    api = fake([
        ad(1, href="https://shop.ru/?utm_source=yandex&utm_campaign={campaign_id}"),
        ad(2, href="https://shop.ru/bukety"),
    ])
    out = read(api)

    tagged = next(p for p in out["landing_pages"] if "utm_source" in p["utm"])
    assert tagged["utm"] == {"utm_source": "yandex", "utm_campaign": "{campaign_id}"}
    assert out["ads_without_utm"] == 1


def test_archived_ads_are_excluded_but_counted():
    api = fake([ad(1), ad(2, state="ARCHIVED", href="https://old.ru/")])
    out = read(api)

    assert out["count"] == 1
    assert out["archived_skipped"] == 1
    assert out["domains"] == ["shop.ru"]


def test_archived_ads_are_returned_on_demand():
    api = fake([ad(1), ad(2, state="ARCHIVED", href="https://old.ru/")])
    out = read(api, include_archived=True)

    assert out["count"] == 2
    assert "archived_skipped" not in out


def test_non_text_ad_survives_without_href():
    """У графических и видеообъявлений блока TextAd нет. Это не повод уронить
    разбор и не повод отдать «ссылок нет» — тип должен остаться видимым.
    """
    api = fake([
        {"Id": 9, "CampaignId": 100, "AdGroupId": 101,
         "Type": "IMAGE_AD", "State": "ON", "Status": "ACCEPTED"},
        ad(1),
    ])
    out = read(api)

    image = next(a for a in out["ads"] if a["id"] == 9)
    assert image["type"] == "IMAGE_AD"
    assert image["href"] is None
    assert out["ads_without_href"] == 1
    assert out["landing_pages"][0]["ads"] == 1


def test_extensions_are_reported_as_presence_not_ids():
    api = fake([ad(1, SitelinkSetId=555, VCardId=777)])
    item = read(api)["ads"][0]

    assert item["sitelinks"] is True
    assert item["vcard"] is True
    assert item["image"] is False


def test_long_lists_are_trimmed_but_landing_pages_stay_complete():
    """Полсотни объявлений с текстами — это страница контекста. Сводка по
    посадочным при этом обязана остаться посчитанной по всем.
    """
    total = ads.ADS_PREVIEW + 10
    api = fake([ad(i, href=f"https://shop.ru/{i}") for i in range(total)])
    out = read(api)

    assert out["count"] == total
    assert len(out["ads"]) == ads.ADS_PREVIEW
    assert out["ads_truncated"] is True
    assert len(out["landing_pages"]) == total


def test_limited_by_is_surfaced():
    """Молча обрезанная выдача читается как полная, и вывод о посадочных
    делается по половине аккаунта.
    """
    api = fake([ad(1)], LimitedBy=1)
    out = read(api)

    assert out["truncated"] is True
    assert out["limited_by"] == 1
