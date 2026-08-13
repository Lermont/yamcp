"""Отчёт на диск, в контекст — только сводка.

Главная мысль всего сервера. Отчёт по кабинету за квартал — это десятки тысяч
строк. Клиент MCP всё равно режет вывод тула (в Claude Code это
MAX_MCP_OUTPUT_TOKENS), так что сырой TSV в ответе — это или обрезанные данные,
или сожжённый контекст. Пишем файл, отдаём путь + totals + первые N строк.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

# Директ ставит "--" там, где значения нет.
EMPTY = {"--", "", "-"}

# Метрики, которые складываются.
SUMMABLE = frozenset({
    "Impressions", "Clicks", "Cost", "Conversions", "Revenue", "Profit",
    "Sessions", "Bounces",
})

# Метрики, которые складывать НЕЛЬЗЯ — только пересчитать из сумм.
# Сумма CTR по строкам — это не CTR, а мусор, но именно так его обычно и считают.
DERIVED = ("Ctr", "AvgCpc", "ConversionRate", "CostPerConversion", "GoalsRoi")


def _num(raw: str) -> float | None:
    if raw in EMPTY:
        return None
    try:
        return float(raw.replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except ValueError:
        return None


def _metric(column: str) -> tuple[str, str]:
    """`Conversions_12345_LSC` → ("Conversions", "_12345_LSC").

    Как только в запросе появляются Goals, Директ дописывает к метрикам цели
    её идентификатор и модель атрибуции. Сравнение имени колонки с "Conversions"
    целиком после этого не срабатывает, и конверсии с доходом молча выпадают из
    итогов — то есть ровно то, ради чего отчёт обычно и заказывают. Все имена
    полей Директа в CamelCase, подчёркивание встречается только в этом суффиксе.
    """
    base, sep, suffix = column.partition("_")
    return (base, sep + suffix) if sep else (column, "")


def _totals(rows: list[dict[str, str]], columns: list[str]) -> dict[str, float]:
    """Суммы по аддитивным полям + корректно пересчитанные производные."""
    sums: dict[str, float] = {}
    for col in columns:
        base, _ = _metric(col)
        if base not in SUMMABLE or base in DERIVED:
            continue
        acc, seen = 0.0, False
        for row in rows:
            v = _num(row.get(col, ""))
            if v is not None:
                acc += v
                seen = True
        if seen:
            sums[col] = round(acc, 2)

    impressions = sums.get("Impressions")
    clicks = sums.get("Clicks")
    cost = sums.get("Cost")

    if impressions:
        sums["Ctr"] = round((clicks or 0) / impressions * 100, 2)
    if clicks:
        sums["AvgCpc"] = round((cost or 0) / clicks, 2)

    # Показы, клики и расход общие на строку, а конверсии — свои у каждой пары
    # «цель + модель атрибуции», поэтому производные считаем по каждому суффиксу.
    for suffix in {_metric(c)[1] for c in columns if _metric(c)[0] == "Conversions"}:
        conversions = sums.get(f"Conversions{suffix}")
        if conversions is None:
            continue
        if clicks:
            sums[f"ConversionRate{suffix}"] = round(conversions / clicks * 100, 2)
        if conversions:
            sums[f"CostPerConversion{suffix}"] = round((cost or 0) / conversions, 2)
    return sums


def parse_tsv(tsv: str) -> tuple[list[str], list[dict[str, str]]]:
    """Первая строка — имена колонок (skipReportHeader/skipReportSummary уже сняли
    шапку и итоговую строку, skipColumnHeader мы намеренно НЕ ставим)."""
    reader = csv.reader(io.StringIO(tsv), delimiter="\t")
    try:
        columns = next(reader)
    except StopIteration:
        return [], []
    rows = []
    for line_number, row in enumerate(reader, start=2):
        if not row or all(not cell for cell in row):
            continue
        if len(row) != len(columns):
            raise ValueError(
                f"Некорректный TSV: строка {line_number} содержит {len(row)} "
                f"полей вместо {len(columns)}"
            )
        rows.append(dict(zip(columns, row)))
    return columns, rows


_UNSAFE_STEM = re.compile(r"[^0-9A-Za-zА-Яа-яЁё._-]+")


def safe_stem(stem: str) -> str:
    """Имя файла собирается из client_login, а его выбирает модель.

    Без чистки логин вида `..\\..\\x` уводит запись за пределы YD_OUT_DIR:
    `out_dir / "..\\..\\x_2026-07-01_..."` — это валидный путь на два каталога
    выше. Чтение из отчётов уже ограничено каталогом, запись должна быть тоже.
    """
    # Обрезаем до strip: точка в конце имени файла на Windows — отдельная беда.
    cleaned = _UNSAFE_STEM.sub("_", stem)[:120].strip("._")
    return cleaned or "report"


def persist(
    tsv: str,
    *,
    out_dir: Path,
    stem: str,
    inline_rows: int,
) -> dict[str, Any]:
    path = out_dir / f"{safe_stem(stem)}.tsv"

    # Пишем ДО разбора. Отчёт уже стоил баллов API и до нескольких минут
    # ожидания в офлайн-очереди; ронять его целиком из-за одной кривой строки
    # (лишний таб в тексте объявления или в поисковом запросе) нельзя.
    path.write_text(tsv, encoding="utf-8", newline="")

    try:
        columns, rows = parse_tsv(tsv)
    except ValueError as exc:
        return {
            "path": str(path),
            "rows": None,
            "columns": [],
            "totals": {},
            "preview": [],
            "preview_truncated": False,
            "parse_error": str(exc),
            "hint": (
                f"Отчёт выгружен и сохранён в {path}, но разобрать TSV не удалось: "
                f"{exc}. Данные не потеряны — откройте файл и проверьте строку."
            ),
        }

    return {
        "path": str(path),
        "rows": len(rows),
        "columns": columns,
        "totals": _totals(rows, columns),
        "preview": rows[:inline_rows],
        "preview_truncated": len(rows) > inline_rows,
        "hint": (
            f"Показаны первые {min(len(rows), inline_rows)} из {len(rows)} строк. "
            f"Полный отчёт в {path} — читайте его direct_read_report "
            f"или обрабатывайте кодом, не тяните целиком в контекст."
        ),
    }


def read_back(path: Path, offset: int, limit: int) -> dict[str, Any]:
    if offset < 0:
        raise ValueError("offset не может быть отрицательным")
    if not 1 <= limit <= 1000:
        raise ValueError("limit должен быть от 1 до 1000")
    columns, rows = parse_tsv(path.read_text(encoding="utf-8"))
    window = rows[offset : offset + limit]
    return {
        "path": str(path),
        "columns": columns,
        "rows_total": len(rows),
        "offset": offset,
        "returned": len(window),
        "data": window,
    }
