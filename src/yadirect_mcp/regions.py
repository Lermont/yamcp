"""Справочник регионов Директа: Dictionaries.get, словарь GeoRegions.

Зачем отдельный тул, а не «модель и так знает коды»:

1. Директ гео-коды НЕ валидирует. `GeoID: [999999]` в Вордстате — не ошибка,
   а данные не по тому региону; неверный `RegionIds` в группе так же молча
   сузит показы. Расплата приходит статистикой через неделю, а не сообщением
   об ошибке в момент вызова.

2. Названия неуникальны. «Москва» — это и город 213, и «Москва и область» 1;
   Ростов есть ярославский и на Дону. Без цепочки родителей выбрать нужный код
   нельзя, поэтому к каждому региону отдаём путь до корня.

Справочник большой (тысячи записей) и одинаковый для всех кабинетов, отсюда
два решения. Тянем его один раз на процесс и держим в памяти. Наружу отдаём
только совпадения с запросом: выгрузить весь справочник в контекст модели
дороже, чем любая ошибка, которую он лечит.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

log = logging.getLogger("yadirect-mcp")

MAX_LIMIT = 200

# Защита от битой ссылки на родителя и цикла в данных: цепочку строим по
# ParentId, а он приходит снаружи и на нас не рассчитан.
MAX_DEPTH = 16

_cache: list[dict[str, Any]] | None = None


def reset_cache() -> None:
    """Сбросить справочник. Нужен тестам: кеш живёт на уровне модуля."""
    global _cache
    _cache = None


def _norm(text: str) -> str:
    """Регистр и «ё» не должны мешать поиску: в справочнике «Орёл», пишут «Орел»."""
    return text.casefold().replace("ё", "е")


async def _load(api: Any, client_login: str | None) -> list[dict[str, Any]]:
    global _cache
    if _cache is None:
        result = await api.call(
            "dictionaries",
            "get",
            {"DictionaryNames": ["GeoRegions"]},
            client_login=client_login,
        )
        _cache = result.get("GeoRegions") or []
        log.info("справочник регионов загружен: %s записей", len(_cache))
    return _cache


def _index(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for item in items:
        raw = item.get("GeoRegionId")
        if raw is None:
            continue
        try:
            by_id[int(raw)] = item
        except (TypeError, ValueError):
            continue
    return by_id


def _path(item: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> list[str]:
    """Названия вышестоящих регионов от корня к родителю."""
    chain: list[str] = []
    # Свой же ID в seen: при цикле в данных регион иначе оказывается
    # родителем самому себе.
    seen: set[int] = set()
    with suppress(KeyError, TypeError, ValueError):
        seen.add(int(item["GeoRegionId"]))
    parent = item.get("ParentId")
    while parent is not None and len(chain) < MAX_DEPTH:
        try:
            parent_id = int(parent)
        except (TypeError, ValueError):
            break
        if parent_id in seen:
            break
        seen.add(parent_id)
        node = by_id.get(parent_id)
        if node is None:
            break
        chain.append(node.get("GeoRegionName") or str(parent_id))
        parent = node.get("ParentId")
    return list(reversed(chain))


def _shape(item: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": int(item["GeoRegionId"]),
        "name": item.get("GeoRegionName"),
        "type": item.get("GeoRegionType"),
        "parent_id": item.get("ParentId"),
        "path": _path(item, by_id),
    }


def search(
    items: list[dict[str, Any]], query: str, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """Совпадения по названию: сначала точные, потом по началу, потом вхождения.

    Внутри ранга короткое имя выигрывает у длинного: по запросу «Москва» нужен
    город, а не «Москворечье-Сабурово».
    """
    by_id = _index(items)
    normalized = _norm(query)
    hits: list[tuple[int, int, str, dict[str, Any]]] = []
    for item in items:
        if item.get("GeoRegionId") is None:
            continue
        name = item.get("GeoRegionName") or ""
        candidate = _norm(name)
        if candidate == normalized:
            rank = 0
        elif candidate.startswith(normalized):
            rank = 1
        elif normalized in candidate:
            rank = 2
        else:
            continue
        hits.append((rank, len(candidate), candidate, item))
    # Имя в ключе — чтобы порядок не зависел от порядка выдачи API.
    hits.sort(key=lambda hit: (hit[0], hit[1], hit[2]))
    return [_shape(item, by_id) for *_, item in hits[:limit]], len(hits)


def resolve(
    items: list[dict[str, Any]], ids: list[int]
) -> tuple[list[dict[str, Any]], list[int]]:
    """Обратная проверка: коды → названия. Несуществующие коды возвращаем явно."""
    by_id = _index(items)
    found: list[dict[str, Any]] = []
    missing: list[int] = []
    for raw in ids:
        try:
            region_id = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"Код региона должен быть числом, получено {raw!r}") from None
        node = by_id.get(region_id)
        if node is None:
            missing.append(region_id)
        else:
            found.append(_shape(node, by_id))
    return found, missing


async def lookup(
    api: Any,
    *,
    query: str | None = None,
    ids: list[int] | None = None,
    client_login: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Найти регионы по названию и/или проверить готовые коды."""
    if not query and not ids:
        raise ValueError(
            "Укажите query (поиск по названию) или ids (проверка кодов). "
            "Весь справочник целиком тул не отдаёт."
        )
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit должен быть от 1 до {MAX_LIMIT}")

    items = await _load(api, client_login)
    payload: dict[str, Any] = {"dictionary_size": len(items)}

    if ids:
        found, missing = resolve(items, ids)
        payload["resolved"] = found
        if missing:
            # Директ такие коды принимает молча, поэтому единственное место, где
            # об ошибке вообще можно узнать, — здесь.
            payload["unknown_ids"] = missing
            payload["warning"] = (
                f"Кодов нет в справочнике: {missing}. Директ их не отвергнет, "
                f"а покажет рекламу не там."
            )
    if query:
        matches, total = search(items, query, limit)
        payload["query"] = query
        payload["matches"] = matches
        payload["total_matches"] = total
        payload["truncated"] = total > len(matches)
    return payload
