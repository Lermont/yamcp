"""Частотность Вордстата: сбор, уборка за собой и упаковка в отчёт.

Единственное место, где сервер уходит с v5 на v4, — аналога Вордстата в v5 нет.
Транспортные особенности старой ветки описаны в `DirectClient.call_v4`, здесь —
всё остальное, что о ней нужно знать:

1. Отчёт асинхронный, как и в Reports API, но устроен иначе. Готовность
   опрашивается ОДНИМ вызовом `GetWordstatReportList` на все заказанные отчёты,
   а не поштучно: список отдаёт статусы всех отчётов аккаунта разом.

2. Забрать данные — половина дела. Неудалённые отчёты копятся в очереди
   аккаунта и однажды упираются в её лимит, причём сломается не тот вызов,
   который намусорил, а следующий. Поэтому удаление живёт в `finally` и
   срабатывает даже когда ждать надоело или пришла ошибка.

3. `Phrases` принимает не более 10 фраз (error_code 241), поэтому длинный
   список семантики режется на батчи и заказывается несколькими отчётами.

4. Регионы НЕ валидируются: `GeoID: [999999]` спокойно создаёт отчёт, просто
   данные будут не те, что ожидал спрашивающий. Проверить за Директ мы не
   можем — остаётся не выдумывать регион молча.

Объём ответа — причина, по которой результат уезжает на диск: Вордстат отдаёт
около 200 подсказок на фразу, то есть полный батч из 10 фраз — это пара тысяч
строк, весь смысл которых в первых двух десятках.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from .client import DirectClient, DirectError
from .store import safe_stem

log = logging.getLogger("yadirect-mcp")

# Лимит массива Phrases в CreateNewWordstatReport.
BATCH = 10
# Пять отчётов за вызов: столько же, сколько офлайн-очередь v5 держит на логин.
# Больше — это уже не «уточнить частотность», а выкачивание Вордстата.
MAX_PHRASES = BATCH * 5
# Отчёт обычно готов за секунды, но опрашивать чаще смысла нет.
POLL_SECONDS = 2.0

# Что искали ВМЕСТЕ с фразой (левая колонка Вордстата) и что искали ПОХОЖЕГО
# (правая колонка). Имена — как в ответе API, чтобы их можно было сверить с
# документацией не догадываясь.
SEARCHED_WITH = "SearchedWith"
SEARCHED_ALSO = "SearchedAlso"

COLUMNS = ("Phrase", "Kind", "Suggestion", "Shows")


def prepare(phrases: list[str]) -> list[str]:
    """Чистка списка фраз до вызова API: пустые строки и дубли стоят баллов."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in phrases:
        phrase = " ".join(str(raw).split())
        if not phrase:
            continue
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(phrase)

    if not cleaned:
        raise ValueError("phrases пустой — укажите хотя бы одну фразу")
    if len(cleaned) > MAX_PHRASES:
        raise ValueError(
            f"За один вызов можно спросить не более {MAX_PHRASES} фраз, "
            f"получено {len(cleaned)}. Разбейте список на части."
        )
    return cleaned


def _batches(phrases: list[str]) -> list[list[str]]:
    return [phrases[i : i + BATCH] for i in range(0, len(phrases), BATCH)]


async def _create(
    api: DirectClient, phrases: list[str], geo_ids: list[int] | None
) -> list[int]:
    """Заказать отчёты. Возвращает ID даже при частичном провале — через них
    вызывающий уберёт за собой то, что успело создаться."""
    report_ids: list[int] = []
    for batch in _batches(phrases):
        param: dict[str, Any] = {"Phrases": batch}
        if geo_ids:
            param["GeoID"] = list(geo_ids)
        try:
            report_ids.append(int(await api.call_v4("CreateNewWordstatReport", param)))
        except (TypeError, ValueError) as exc:
            raise DirectError(
                "CreateNewWordstatReport вернул не идентификатор отчёта"
            ) from exc
    return report_ids


async def _await_ready(api: DirectClient, report_ids: list[int], deadline: float) -> None:
    """Ждать, пока все заказанные отчёты станут Done.

    Статус отчёта, которого нет в списке, узнать неоткуда, а `GetWordstatReport`
    по нему отдаст пустоту, которую легко принять за «нет спроса» — поэтому
    пропажа считается ошибкой, а не поводом идти дальше.
    """
    pending = set(report_ids)
    while True:
        listing = await api.call_v4("GetWordstatReportList") or []
        statuses = {
            int(item["ReportID"]): str(item.get("StatusReport", ""))
            for item in listing
            if item.get("ReportID") is not None
        }

        lost = pending - statuses.keys()
        if lost:
            raise DirectError(
                f"Вордстат потерял отчёты {sorted(lost)}: их нет в очереди аккаунта"
            )

        failed = {
            rid: status
            for rid, status in statuses.items()
            if rid in pending and status not in ("Done", "Pending")
        }
        if failed:
            raise DirectError(f"Вордстат вернул статус отчёта {failed}")

        pending = {rid for rid in pending if statuses[rid] != "Done"}
        if not pending:
            return
        if time.monotonic() > deadline:
            raise DirectError(
                f"Вордстат не посчитал отчёты {sorted(pending)} за отведённое время. "
                f"Спросите меньше фраз за раз или увеличьте YD_REPORT_DEADLINE."
            )
        log.info("wordstat: ждём %s отчётов", len(pending))
        await asyncio.sleep(POLL_SECONDS)


async def _fetch(api: DirectClient, report_ids: list[int]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for report_id in report_ids:
        items.extend(await api.call_v4("GetWordstatReport", report_id) or [])
    return items


async def _drop(api: DirectClient, report_ids: list[int]) -> None:
    """Уборка не должна прятать исходную ошибку, ради которой сюда пришли."""
    for report_id in report_ids:
        try:
            await api.call_v4("DeleteWordstatReport", report_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("wordstat: не удалось удалить отчёт %s: %s", report_id, exc)


def _by_phrase(
    items: list[dict[str, Any]], phrases: list[str]
) -> dict[str, dict[str, Any]]:
    """Разложить ответы по запрошенным фразам.

    Сопоставляем по тексту, а не по порядку: фразы уехали в разных отчётах и
    вернуться могут как угодно. Регистр Вордстат не хранит, поэтому ключ —
    casefold.
    """
    index = {phrase.casefold(): phrase for phrase in phrases}
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        phrase = index.get(str(item.get("Phrase", "")).casefold())
        if phrase is not None:
            out[phrase] = item
    return out


def _suggestions(item: dict[str, Any] | None, kind: str, min_shows: int) -> list[tuple[str, int]]:
    """Подсказки одного вида, отсортированные по убыванию показов."""
    rows: list[tuple[str, int]] = []
    for entry in (item or {}).get(kind) or []:
        text = str(entry.get("Phrase", "")).strip()
        try:
            shows = int(entry.get("Shows", 0))
        except (TypeError, ValueError):
            continue
        if text and shows >= min_shows:
            rows.append((text, shows))
    rows.sort(key=lambda row: (-row[1], row[0]))
    return rows


def to_tsv(
    phrases: list[str], by_phrase: dict[str, dict[str, Any]], min_shows: int
) -> str:
    """Тот же TSV, что и у отчётов: его читает direct_read_report."""
    lines = ["\t".join(COLUMNS)]
    for phrase in phrases:
        item = by_phrase.get(phrase)
        for kind in (SEARCHED_WITH, SEARCHED_ALSO):
            for text, shows in _suggestions(item, kind, min_shows):
                # Таб внутри фразы порвал бы колонки; переносов Вордстат не шлёт.
                lines.append(f"{phrase}\t{kind}\t{text.replace(chr(9), ' ')}\t{shows}")
    return "\n".join(lines) + "\n"


def summarize(
    phrases: list[str], by_phrase: dict[str, dict[str, Any]], min_shows: int, top: int
) -> list[dict[str, Any]]:
    """Сводка на фразу — то, ради чего Вордстат обычно и спрашивают.

    Частотность самой фразы вынесена отдельным полем: в подсказках она всегда
    первой строкой, и без этого модель принимает за спрос по фразе показы
    самого жирного вложенного запроса.
    """
    summary: list[dict[str, Any]] = []
    for phrase in phrases:
        item = by_phrase.get(phrase)
        with_rows = _suggestions(item, SEARCHED_WITH, min_shows)
        also_rows = _suggestions(item, SEARCHED_ALSO, min_shows)

        # Пустая выдача и отсутствие ответа выглядят одинаково — обе дают
        # shows: null, — но значат разное: в первом случае спроса нет, во втором
        # мы просто ничего не знаем. Смотрим на сырой ответ, а не на строки
        # после min_shows: иначе порог отсечения выдаётся за отсутствие спроса.
        note = None
        if item is None:
            note = "Вордстат не вернул данных по фразе"
        elif not ((item.get(SEARCHED_WITH) or []) or (item.get(SEARCHED_ALSO) or [])):
            note = "Вордстат не нашёл запросов: спроса нет либо фраза с опечаткой"

        key = phrase.casefold()
        exact = next((shows for text, shows in with_rows if text.casefold() == key), None)
        nested = [
            {"phrase": text, "shows": shows}
            for text, shows in with_rows
            if text.casefold() != key
        ][:top]
        summary.append(
            {
                "phrase": phrase,
                "shows": exact,
                "nested_total": len(with_rows),
                "similar_total": len(also_rows),
                "top_nested": nested,
                "top_similar": [
                    {"phrase": text, "shows": shows} for text, shows in also_rows[:top]
                ],
                **({"note": note} if note else {}),
            }
        )
    return summary


def _stem(phrases: list[str], geo_ids: list[int] | None) -> str:
    """Имя файла: узнаваемое начало + хеш, чтобы разные запросы не затирали
    друг друга, а повтор того же запроса обновлял свой файл."""
    blob = json.dumps([phrases, geo_ids or []], ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]
    return f"wordstat_{phrases[0][:40]}_{digest}"


async def lookup(
    api: DirectClient,
    *,
    phrases: list[str],
    geo_ids: list[int] | None,
    out_dir: Path,
    deadline_seconds: float,
    min_shows: int = 0,
    top: int = 10,
) -> dict[str, Any]:
    """Частотность по списку фраз: файл на диске + сводка в ответ."""
    phrases = prepare(phrases)
    if min_shows < 0:
        raise ValueError("min_shows не может быть отрицательным")
    if not 1 <= top <= 100:
        raise ValueError("top должен быть от 1 до 100")

    report_ids: list[int] = []
    try:
        report_ids = await _create(api, phrases, geo_ids)
        await _await_ready(api, report_ids, time.monotonic() + deadline_seconds)
        items = await _fetch(api, report_ids)
    finally:
        # Отчёты живут в очереди аккаунта до удаления, а не до конца процесса.
        if report_ids:
            await _drop(api, report_ids)

    by_phrase = _by_phrase(items, phrases)
    tsv = to_tsv(phrases, by_phrase, min_shows)
    path = out_dir / f"{safe_stem(_stem(phrases, geo_ids))}.tsv"
    path.write_text(tsv, encoding="utf-8", newline="")

    rows = tsv.count("\n") - 1
    return {
        "path": str(path),
        "geo_ids": list(geo_ids) if geo_ids else None,
        "rows": rows,
        "columns": list(COLUMNS),
        "phrases": summarize(phrases, by_phrase, min_shows, top),
        "hint": (
            f"В ответе — топ-{top} подсказок на фразу; всего строк в файле: {rows}. "
            f"Полный список в {path}, читайте его direct_read_report постранично "
            f"и не тяните целиком в контекст. Shows — число показов за месяц по "
            f"данным Вордстата, то есть спрос в поиске, а не прогноз показов "
            f"кампании."
        ),
    }
