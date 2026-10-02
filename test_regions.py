"""Тесты справочника регионов: ранжирование, путь до корня, проверка кодов."""

from __future__ import annotations

import asyncio

import pytest

from yadirect_mcp import regions

GEO = [
    {"GeoRegionId": 10000, "GeoRegionName": "Весь мир", "GeoRegionType": "World",
     "ParentId": None},
    {"GeoRegionId": 225, "GeoRegionName": "Россия", "GeoRegionType": "Country",
     "ParentId": 10000},
    {"GeoRegionId": 1, "GeoRegionName": "Москва и область", "GeoRegionType": "Region",
     "ParentId": 225},
    {"GeoRegionId": 213, "GeoRegionName": "Москва", "GeoRegionType": "City",
     "ParentId": 1},
    {"GeoRegionId": 20574, "GeoRegionName": "Москворечье-Сабурово",
     "GeoRegionType": "District", "ParentId": 213},
    {"GeoRegionId": 10645, "GeoRegionName": "Орёл", "GeoRegionType": "City",
     "ParentId": 225},
    {"GeoRegionId": 39, "GeoRegionName": "Ростов-на-Дону", "GeoRegionType": "City",
     "ParentId": 225},
    {"GeoRegionId": 10877, "GeoRegionName": "Ростов", "GeoRegionType": "City",
     "ParentId": 225},
]


class FakeApi:
    def __init__(self, items=GEO):
        self.items = items
        self.calls = []

    async def call(self, service, method, params, *, client_login=None):
        self.calls.append((service, method, params, client_login))
        return {"GeoRegions": self.items}


@pytest.fixture(autouse=True)
def _clean_cache():
    """Кеш справочника живёт на уровне модуля и течёт между тестами."""
    regions.reset_cache()
    yield
    regions.reset_cache()


def lookup(api, **kwargs):
    return asyncio.run(regions.lookup(api, **kwargs))


def test_exact_name_wins_over_longer_matches():
    """«Москва» — это и город, и область. Модель возьмёт первый вариант,
    поэтому первым обязан идти точный.
    """
    out = lookup(FakeApi(), query="москва")
    assert [m["id"] for m in out["matches"]] == [213, 1]


def test_shorter_name_wins_inside_the_same_rank():
    """По префиксу «москв» подходят три региона; нужный почти всегда короткий."""
    out = lookup(FakeApi(), query="москв")
    assert [m["id"] for m in out["matches"]] == [213, 1, 20574]


def test_substring_match_ranks_below_prefix():
    out = lookup(FakeApi(), query="ростов")
    assert [m["id"] for m in out["matches"]] == [10877, 39]


def test_yo_and_case_do_not_block_the_search():
    """В справочнике «Орёл», в запросе почти всегда «Орел»."""
    out = lookup(FakeApi(), query="ОРЕЛ")
    assert [m["id"] for m in out["matches"]] == [10645]


def test_path_disambiguates_same_named_regions():
    out = lookup(FakeApi(), query="москва")
    city, oblast = out["matches"][0], out["matches"][1]
    assert city["path"] == ["Весь мир", "Россия", "Москва и область"]
    assert oblast["path"] == ["Весь мир", "Россия"]


def test_unknown_ids_are_reported_explicitly():
    """Единственное место, где неверный код вообще можно заметить: Директ его
    принимает молча и отдаёт данные не по тому региону.
    """
    out = lookup(FakeApi(), ids=[213, 999999])
    assert [r["id"] for r in out["resolved"]] == [213]
    assert out["unknown_ids"] == [999999]
    assert "999999" in out["warning"]


def test_ids_and_query_work_together():
    out = lookup(FakeApi(), ids=[225], query="орел")
    assert out["resolved"][0]["name"] == "Россия"
    assert out["matches"][0]["id"] == 10645
    assert "unknown_ids" not in out


def test_limit_truncates_but_reports_the_total():
    out = lookup(FakeApi(), query="москв", limit=1)
    assert len(out["matches"]) == 1
    assert out["total_matches"] == 3
    assert out["truncated"] is True


def test_dictionary_is_fetched_once_per_process():
    """Справочник одинаков для всех кабинетов и не меняется в течение сессии,
    а каждый его вызов — это баллы.
    """
    api = FakeApi()
    lookup(api, query="москва")
    lookup(api, query="ростов")
    assert len(api.calls) == 1
    assert api.calls[0][0] == "dictionaries"
    assert api.calls[0][2] == {"DictionaryNames": ["GeoRegions"]}


def test_empty_request_is_rejected():
    with pytest.raises(ValueError, match="query"):
        lookup(FakeApi())


@pytest.mark.parametrize("limit", [0, regions.MAX_LIMIT + 1])
def test_limit_bounds_are_enforced(limit):
    with pytest.raises(ValueError, match="limit"):
        lookup(FakeApi(), query="москва", limit=limit)


def test_broken_parent_chain_does_not_hang():
    """ParentId приходит снаружи: цикл и ссылка в никуда не должны вешать поиск."""
    broken = [
        {"GeoRegionId": 1, "GeoRegionName": "Кольцо А", "ParentId": 2},
        {"GeoRegionId": 2, "GeoRegionName": "Кольцо Б", "ParentId": 1},
        {"GeoRegionId": 3, "GeoRegionName": "Сирота", "ParentId": 777},
    ]
    out = lookup(FakeApi(broken), query="кольцо а")
    assert out["matches"][0]["path"] == ["Кольцо Б"]
    orphan = lookup(FakeApi(broken), query="сирота")
    assert orphan["matches"][0]["path"] == []


def test_client_login_reaches_the_api_call():
    """Агентскому токену Директ не отдаёт клиентские методы без Client-Login."""
    api = FakeApi()
    lookup(api, query="москва", client_login="client1")
    assert api.calls[0][3] == "client1"
