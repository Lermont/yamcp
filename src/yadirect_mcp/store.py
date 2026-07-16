"""Отчёт на диск, в контекст — только сводка.

Главная мысль всего сервера. Отчёт по кабинету за квартал — это десятки тысяч
строк. Клиент MCP всё равно режет вывод тула (в Claude Code это
MAX_MCP_OUTPUT_TOKENS), так что сырой TSV в ответе — это или обрезанные данные,
или сожжённый контекст. Пишем файл, отдаём путь + totals + первые N строк.
"""

from __future__ import annotations

import csv
import io
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


def _totals(rows: list[dict[str, str]], columns: list[str]) -> dict[str, float]:
    """Суммы по аддитивным полям + корректно пересчитанные производные."""
    sums: dict[str, float] = {}
    for col in columns:
        if col not in SUMMABLE or col in DERIVED:
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
    conversions = sums.get("Conversions")

    if impressions:
        sums["Ctr"] = round((clicks or 0) / impressions * 100, 2)
    if clicks:
        sums["AvgCpc"] = round((cost or 0) / clicks, 2)
        if conversions is not None:
            sums["ConversionRate"] = round(conversions / clicks * 100, 2)
    if conversions:
        sums["CostPerConversion"] = round((cost or 0) / conversions, 2)
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


def persist(
    tsv: str,
    *,
    out_dir: Path,
    stem: str,
    inline_rows: int,
) -> dict[str, Any]:
    columns, rows = parse_tsv(tsv)

    path = out_dir / f"{stem}.tsv"
    path.write_text(tsv, encoding="utf-8")

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
