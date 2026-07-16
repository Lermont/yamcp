"""Конфигурация из окружения.

Обязательное:
    YD_TOKEN            — OAuth-токен агентства (scope direct:api)

Опциональное:
    YD_AGENCY_LOGIN     — логин агентства; нужен только для agencyclients.get
    YD_ALLOWED_LOGINS   — белый список клиентских логинов через запятую.
                          Пусто = разрешены любые. Страховка от того, что модель
                          сходит не в тот кабинет.
    YD_OUT_DIR          — куда складывать выгруженные TSV (по умолчанию ./out)
    YD_SANDBOX          — true → песочница
    YD_MAX_INFLIGHT     — сколько офлайн-отчётов держать в очереди на один логин.
                          Директ разрешает 5, берём 4 с запасом.
    YD_INLINE_ROWS      — сколько строк отдавать в ответе тула (остальное на диске)
    YD_REPORT_DEADLINE  — сколько секунд ждать готовности офлайн-отчёта
    YD_LANG             — Accept-Language для сообщений об ошибках (ru/en)
    YD_MODE             — report (по умолчанию) или campaign_setup.
                          Второй режим добавляет подтверждаемое создание кампаний.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    token: str
    agency_login: str | None
    allowed_logins: frozenset[str]
    out_dir: Path
    sandbox: bool
    max_inflight: int
    inline_rows: int
    report_deadline: float
    lang: str
    mode: str = "report"

    def check_login(self, client_login: str) -> None:
        """Бросает ValueError, если логин не в белом списке."""
        if self.allowed_logins and client_login.lower() not in self.allowed_logins:
            raise ValueError(
                f"Логин {client_login!r} не разрешён. "
                f"Добавьте его в YD_ALLOWED_LOGINS или уберите переменную."
            )


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    return raw in ("1", "true", "yes", "on") if raw else default


def load() -> Settings:
    token = os.getenv("YD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("YD_TOKEN не задан")

    allowed = {
        s.strip().lower()
        for s in os.getenv("YD_ALLOWED_LOGINS", "").split(",")
        if s.strip()
    }

    out_dir = Path(os.getenv("YD_OUT_DIR", "./out")).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        max_inflight = int(os.getenv("YD_MAX_INFLIGHT", "4"))
        inline_rows = int(os.getenv("YD_INLINE_ROWS", "30"))
        report_deadline = float(os.getenv("YD_REPORT_DEADLINE", "600"))
    except ValueError as exc:
        raise RuntimeError(f"Некорректное числовое значение в конфигурации: {exc}") from exc
    if not 1 <= max_inflight <= 5:
        raise RuntimeError("YD_MAX_INFLIGHT должен быть от 1 до 5")
    if not 0 <= inline_rows <= 1000:
        raise RuntimeError("YD_INLINE_ROWS должен быть от 0 до 1000")
    if report_deadline <= 0:
        raise RuntimeError("YD_REPORT_DEADLINE должен быть больше 0")

    lang = os.getenv("YD_LANG", "ru").strip().lower()
    if lang not in {"ru", "en"}:
        raise RuntimeError("YD_LANG должен быть ru или en")
    mode = os.getenv("YD_MODE", "report").strip().lower()
    if mode not in {"report", "campaign_setup"}:
        raise RuntimeError("YD_MODE должен быть report или campaign_setup")

    return Settings(
        token=token,
        agency_login=os.getenv("YD_AGENCY_LOGIN", "").strip() or None,
        allowed_logins=frozenset(allowed),
        out_dir=out_dir,
        sandbox=_flag("YD_SANDBOX"),
        max_inflight=max_inflight,
        inline_rows=inline_rows,
        report_deadline=report_deadline,
        lang=lang,
        mode=mode,
    )
