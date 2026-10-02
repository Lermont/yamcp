"""Отчёт на диск, в контекст — только сводка.

Главная мысль всего сервера. Отчёт по кабинету за квартал — это десятки тысяч
строк. Клиент MCP всё равно режет вывод тула (в Claude Code это
MAX_MCP_OUTPUT_TOKENS), так что сырой TSV в ответе — это или обрезанные данные,
или сожжённый контекст. Пишем файл, отдаём путь + totals + первые N строк.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from functools import lru_cache
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
        # strict=True держит инвариант проверки выше: расхождение длин здесь
        # означало бы молча потерянную колонку.
        rows.append(dict(zip(columns, row, strict=True)))
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
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = out_dir / f"{safe_stem(stem)}.tsv"

    # Пишем ДО разбора. Отчёт уже стоил баллов API и до нескольких минут
    # ожидания в офлайн-очереди; ронять его целиком из-за одной кривой строки
    # (лишний таб в тексте объявления или в поисковом запросе) нельзя.
    path.write_text(tsv, encoding="utf-8", newline="")
    metadata_payload = _write_metadata(path, metadata)

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
            **metadata_payload,
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
        **metadata_payload,
        "hint": (
            f"Показаны первые {min(len(rows), inline_rows)} из {len(rows)} строк. "
            f"Полный отчёт в {path} — читайте его direct_read_report "
            f"или обрабатывайте кодом, не тяните целиком в контекст."
        ),
    }


def _metadata_preview(metadata: dict[str, Any]) -> dict[str, Any]:
    result = dict(metadata)
    for key in ("campaign_settings", "comparison_mismatch_campaign_ids"):
        if isinstance(result.get(key), list) and len(result[key]) > 50:
            result[f"{key}_count"] = len(result[key])
            result[key] = result[key][:50]
            result["preview_truncated"] = True
    return result


def _write_metadata(path: Path, metadata: dict[str, Any] | None) -> dict[str, Any]:
    sidecar = path.with_suffix(".metadata.json")
    if metadata is None:
        # Reusing a legacy output stem must not retain another report's labels.
        sidecar.unlink(missing_ok=True)
        return {}
    full = {**metadata, "tsv_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    sidecar.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"metadata_path": str(sidecar), "metadata": _metadata_preview(full)}


def _read_metadata(path: Path) -> dict[str, Any]:
    sidecar = path.with_suffix(".metadata.json")
    if not sidecar.exists():
        return {"metadata_warning": (
            "У старого отчёта нет метаданных; цели и атрибуция не подтверждены."
        )}
    try:
        full = json.loads(sidecar.read_text(encoding="utf-8"))
        if (not isinstance(full, dict)
                or full.get("tsv_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
            raise ValueError("metadata/TSV mismatch")
    except (OSError, ValueError):
        return {"metadata_warning": (
            "Метаданные повреждены или относятся к другой версии TSV; "
            "не используйте их для выводов."
        )}
    return {"metadata_path": str(sidecar), "metadata": _metadata_preview(full)}


def _fingerprint(path: Path) -> tuple[int, int, int, str]:
    try:
        stat = path.stat()
        # Windows can retain identical timestamps for consecutive same-size
        # writes. Content must participate or cached metadata may label new TSV
        # bytes with old goals despite the sidecar's SHA-256 protection.
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.st_size <= 2_000_000 else ""
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, digest
    except FileNotFoundError:
        return 0, 0, 0, ""


def _snapshot(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        columns, rows = parse_tsv(text)
        return {"columns": columns, "data": rows, **_read_metadata(path)}
    except ValueError as exc:
        return {
            "columns": [], "data": [], "parse_error": str(exc),
            "raw_lines": text.splitlines(), **_read_metadata(path),
        }


@lru_cache(maxsize=4)
def _cached_snapshot(
    path: Path, fingerprint: tuple[int, int, int, str],
    metadata_fingerprint: tuple[int, int, int, str],
) -> dict[str, Any]:
    return _snapshot(path)


def read_back(path: Path, offset: int, limit: int) -> dict[str, Any]:
    if offset < 0:
        raise ValueError("offset не может быть отрицательным")
    if not 1 <= limit <= 1000:
        raise ValueError("limit должен быть от 1 до 1000")
    fingerprint = _fingerprint(path)
    # Bound the parse cache by entry count and source bytes. Hash small files on
    # each read; larger reports do not occupy persistent memory.
    snapshot = (
        _cached_snapshot(path, fingerprint, _fingerprint(path.with_suffix(".metadata.json")))
        if fingerprint[0] <= 2_000_000 else _snapshot(path)
    )
    rows = snapshot["data"]
    window = rows[offset : offset + limit]
    if "parse_error" in snapshot:
        lines = snapshot["raw_lines"]
        window = lines[offset : offset + limit]
        return {
            "path": str(path), "parse_error": snapshot["parse_error"],
            "mode": "raw_lines", "offset": offset, "returned": len(window),
            "lines_total": len(lines), "raw_lines": window,
            "next_offset": offset + len(window) if offset + len(window) < len(lines) else None,
            "hint": ("offset считает физические строки с 0, включая заголовок. "
                     "Значения не исправлены."),
            **{key: value for key, value in snapshot.items() if key.startswith("metadata")},
        }
    return {
        "path": str(path),
        "columns": snapshot["columns"],
        "rows_total": len(rows),
        "offset": offset,
        "returned": len(window),
        "data": window,
        **{key: value for key, value in snapshot.items() if key.startswith("metadata")},
    }
