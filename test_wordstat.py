"""Тесты Вордстата: транспорт v4, уборка очереди и разбор ответа.

Ловим ровно то, на чём эта ветка ломается молча: ошибка v4 приезжает с HTTP 200,
отчёты остаются в очереди аккаунта до явного удаления, а частотность самой фразы
живёт первой строкой среди вложенных запросов.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from yadirect_mcp import wordstat
from yadirect_mcp.client import DirectClient, DirectError
from yadirect_mcp.config import Settings

V4 = "https://api.direct.yandex.ru/v4/json/"


async def _no_sleep(_seconds: float) -> None:
    return None


def settings(tmp_path) -> Settings:
    return Settings(
        token="t0k3n",
        agency_login="agency",
        allowed_logins=frozenset(),
        out_dir=tmp_path,
        sandbox=False,
        max_inflight=4,
        inline_rows=30,
        report_deadline=30.0,
        lang="ru",
    )


ITEM = {
    "Phrase": "окна пвх",
    "GeoID": [225],
    "SearchedWith": [
        {"Phrase": "окна пвх", "Shows": 165395},
        {"Phrase": "купить окна пвх", "Shows": 18252},
        {"Phrase": "окна пвх бу", "Shows": 7},
    ],
    "SearchedAlso": [
        {"Phrase": "стеклопакет", "Shows": 353156},
        {"Phrase": "остекление", "Shows": 12},
    ],
}


class FakeV4:
    """Заглушка эндпоинта v4: роутинг по методу из тела, как у настоящего.

    Все методы приходят на один URL, поэтому side_effect списком ответов тут не
    работает — нужен разбор тела.
    """

    def __init__(self, *, pending_rounds: int = 0, items: list[dict] | None = None):
        self.calls: list[tuple[str, object]] = []
        self.created: list[int] = []
        self.deleted: list[int] = []
        self.pending_rounds = pending_rounds
        self.items = ITEM if items is None else items

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, param = body["method"], body.get("param")
        self.calls.append((method, param))

        if method == "CreateNewWordstatReport":
            report_id = 1000 + len(self.created)
            self.created.append(report_id)
            return httpx.Response(200, json={"data": report_id})

        if method == "GetWordstatReportList":
            done = self.pending_rounds <= 0
            self.pending_rounds -= 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"ReportID": rid, "StatusReport": "Done" if done else "Pending"}
                        for rid in self.created
                        if rid not in self.deleted
                    ]
                },
            )

        if method == "GetWordstatReport":
            items = self.items if isinstance(self.items, list) else [self.items]
            return httpx.Response(200, json={"data": items})

        if method == "DeleteWordstatReport":
            self.deleted.append(int(param))
            return httpx.Response(200, json={"data": 1})

        raise AssertionError(f"неожиданный метод {method}")

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)


@pytest.mark.asyncio
@respx.mock
async def test_full_cycle_batches_polls_and_cleans_up(tmp_path, monkeypatch):
    """12 фраз не влезают в один отчёт: Phrases принимает максимум 10."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    fake = FakeV4(pending_rounds=1)
    respx.post(V4).mock(side_effect=fake)

    phrases = [f"фраза {i}" for i in range(12)]
    async with DirectClient(settings(tmp_path)) as api:
        out = await wordstat.lookup(
            api,
            phrases=phrases,
            geo_ids=[225],
            out_dir=tmp_path,
            deadline_seconds=30,
        )

    assert fake.count("CreateNewWordstatReport") == 2
    sizes = [len(p["Phrases"]) for name, p in fake.calls if name == "CreateNewWordstatReport"]
    assert sizes == [10, 2]
    # Список статусов опрашивается разом на все отчёты, а не по одному на каждый.
    assert fake.count("GetWordstatReportList") == 2
    assert fake.count("GetWordstatReport") == 2
    # Уборка обязательна: отчёты копятся в очереди аккаунта до удаления.
    assert sorted(fake.deleted) == fake.created
    assert Path(out["path"]).exists()


@pytest.mark.asyncio
@respx.mock
async def test_reports_are_deleted_when_waiting_gives_up(tmp_path, monkeypatch):
    """Сломавшийся вызов не должен оставлять мусор следующему."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    fake = FakeV4(pending_rounds=99)
    respx.post(V4).mock(side_effect=fake)

    async with DirectClient(settings(tmp_path)) as api:
        with pytest.raises(DirectError) as exc:
            await wordstat.lookup(
                api,
                phrases=["окна пвх"],
                geo_ids=None,
                out_dir=tmp_path,
                deadline_seconds=-1,
            )

    assert "не посчитал" in str(exc.value)
    assert fake.deleted == fake.created != []


@pytest.mark.asyncio
@respx.mock
async def test_v4_error_arrives_with_http_200(tmp_path):
    """Плоский error_code в теле при HTTP 200 — главная ловушка v4."""
    respx.post(V4).mock(
        return_value=httpx.Response(
            200,
            json={
                "error_code": 241,
                "error_str": "Превышен допустимый размер массива",
                "error_detail": "Массив Phrases должен содержать не более 10 элементов",
            },
        )
    )
    async with DirectClient(settings(tmp_path)) as api:
        with pytest.raises(DirectError) as exc:
            await wordstat.lookup(
                api,
                phrases=["окна пвх"],
                geo_ids=None,
                out_dir=tmp_path,
                deadline_seconds=30,
            )
    assert exc.value.code == 241
    assert "не более 10" in str(exc.value)


@pytest.mark.asyncio
@respx.mock
async def test_token_travels_in_body_not_in_header(tmp_path):
    """Заголовок Authorization v4 не понимает и отвечает ошибкой 53."""
    fake = FakeV4()
    respx.post(V4).mock(side_effect=fake)
    async with DirectClient(settings(tmp_path)) as api:
        await wordstat.lookup(
            api,
            phrases=["окна пвх"],
            geo_ids=[225],
            out_dir=tmp_path,
            deadline_seconds=30,
        )

    request = respx.calls[0].request
    assert "Authorization" not in request.headers
    body = json.loads(request.content)
    assert body["token"] == "t0k3n"
    assert body["locale"] == "ru"


@pytest.mark.asyncio
@respx.mock
async def test_geo_ids_are_omitted_when_not_asked(tmp_path):
    """Пустой GeoID и отсутствующий — разные запросы, слать пустой незачем."""
    fake = FakeV4()
    respx.post(V4).mock(side_effect=fake)
    async with DirectClient(settings(tmp_path)) as api:
        await wordstat.lookup(
            api, phrases=["окна пвх"], geo_ids=None, out_dir=tmp_path, deadline_seconds=30
        )
    param = fake.calls[0][1]
    assert "GeoID" not in param


@pytest.mark.asyncio
@respx.mock
async def test_summary_separates_phrase_shows_from_nested(tmp_path):
    """Частотность фразы — первая строка вложенных, и её легко принять за топ."""
    respx.post(V4).mock(side_effect=FakeV4())
    async with DirectClient(settings(tmp_path)) as api:
        out = await wordstat.lookup(
            api,
            phrases=["Окна   ПВХ"],  # регистр и лишние пробелы приходят от модели
            geo_ids=[225],
            out_dir=tmp_path,
            deadline_seconds=30,
            top=5,
        )

    summary = out["phrases"][0]
    assert summary["shows"] == 165395
    assert [row["phrase"] for row in summary["top_nested"]] == ["купить окна пвх", "окна пвх бу"]
    assert summary["top_similar"][0]["phrase"] == "стеклопакет"
    assert summary["nested_total"] == 3

    saved = Path(out["path"]).read_text(encoding="utf-8")
    assert saved.startswith("Phrase\tKind\tSuggestion\tShows\n")
    assert "SearchedAlso\tстеклопакет\t353156" in saved
    assert out["rows"] == 5


@pytest.mark.asyncio
@respx.mock
async def test_min_shows_cuts_the_tail(tmp_path):
    respx.post(V4).mock(side_effect=FakeV4())
    async with DirectClient(settings(tmp_path)) as api:
        out = await wordstat.lookup(
            api,
            phrases=["окна пвх"],
            geo_ids=[225],
            out_dir=tmp_path,
            deadline_seconds=30,
            min_shows=100,
        )
    assert out["rows"] == 3
    assert out["phrases"][0]["nested_total"] == 2
    assert out["phrases"][0]["similar_total"] == 1


@pytest.mark.asyncio
@respx.mock
async def test_min_shows_does_not_masquerade_as_missing_demand(tmp_path):
    """Порог отсечения — это выбор спрашивающего, а не приговор спросу."""
    respx.post(V4).mock(side_effect=FakeV4())
    async with DirectClient(settings(tmp_path)) as api:
        out = await wordstat.lookup(
            api,
            phrases=["окна пвх"],
            geo_ids=None,
            out_dir=tmp_path,
            deadline_seconds=30,
            min_shows=10**9,
        )
    assert out["rows"] == 0
    assert "note" not in out["phrases"][0]


@pytest.mark.asyncio
@respx.mock
async def test_no_demand_and_no_answer_are_told_apart(tmp_path):
    """Оба случая дают shows: null, но значат разное — пустая выдача против молчания."""
    empty = {"Phrase": "чушь ыыы", "GeoID": [225], "SearchedWith": [], "SearchedAlso": []}
    respx.post(V4).mock(side_effect=FakeV4(items=[ITEM, empty]))
    async with DirectClient(settings(tmp_path)) as api:
        out = await wordstat.lookup(
            api,
            phrases=["окна пвх", "чушь ыыы", "чего вордстат не вернул"],
            geo_ids=None,
            out_dir=tmp_path,
            deadline_seconds=30,
        )

    assert "note" not in out["phrases"][0]
    assert out["phrases"][1]["shows"] is None
    assert "спроса нет" in out["phrases"][1]["note"]
    assert out["phrases"][2]["shows"] is None
    assert "не вернул данных" in out["phrases"][2]["note"]


@pytest.mark.asyncio
@respx.mock
async def test_lost_report_is_an_error_not_empty_data(tmp_path, monkeypatch):
    """Пропавший из очереди отчёт отдал бы пустоту, похожую на «нет спроса»."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    def vanish(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "CreateNewWordstatReport":
            return httpx.Response(200, json={"data": 777})
        if method == "GetWordstatReportList":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": 1})

    respx.post(V4).mock(side_effect=vanish)
    async with DirectClient(settings(tmp_path)) as api:
        with pytest.raises(DirectError) as exc:
            await wordstat.lookup(
                api,
                phrases=["окна пвх"],
                geo_ids=None,
                out_dir=tmp_path,
                deadline_seconds=30,
            )
    assert "потерял" in str(exc.value)


@pytest.mark.parametrize(
    "phrases, expected",
    [
        (["окна", "ОКНА", " окна "], ["окна"]),
        (["окна", "", "  ", "двери"], ["окна", "двери"]),
        (["окна   пвх"], ["окна пвх"]),
    ],
)
def test_prepare_cleans_the_list(phrases, expected):
    assert wordstat.prepare(phrases) == expected


@pytest.mark.parametrize("phrases", [[], ["", "   "]])
def test_prepare_rejects_empty_input(phrases):
    with pytest.raises(ValueError, match="пустой"):
        wordstat.prepare(phrases)


def test_prepare_caps_the_batch():
    with pytest.raises(ValueError, match="не более"):
        wordstat.prepare([f"фраза {i}" for i in range(wordstat.MAX_PHRASES + 1)])
