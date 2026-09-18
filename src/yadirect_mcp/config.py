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
    YD_DEFAULT_WEEKLY_BUDGET
                        — недельный бюджет кампании по умолчанию, в валюте
                          кабинета (не в микроединицах). Пусто = не подсказывать.
                          Попадает в instructions, чтобы не проговаривать одну
                          и ту же сумму на каждом запуске.
    YD_CREATE_REPORT_SSH_HOST
                        — SSH-алиас для публикации итогового HTML. Пусто =
                          сохранить отчёт только локально.
    YD_CREATE_REPORT_REMOTE_ROOT
                        — корень клиентских отчётов на удалённом сервере.
    YD_CREATE_REPORT_PUBLIC_BASE_URL
                        — публичный базовый URL клиентских отчётов.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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
    wordstat_token: str = ""
    metrika_token: str = ""
    use_operator_units: bool = False
    mode: str = "report"
    default_weekly_budget: float | None = None
    create_report_ssh_host: str | None = None
    create_report_remote_root: str = ""
    create_report_public_base_url: str = ""

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

    raw_budget = os.getenv("YD_DEFAULT_WEEKLY_BUDGET", "").strip().replace(",", ".")
    default_weekly_budget: float | None = None
    if raw_budget:
        try:
            default_weekly_budget = float(raw_budget)
        except ValueError as exc:
            raise RuntimeError(
                f"YD_DEFAULT_WEEKLY_BUDGET должен быть числом, получено {raw_budget!r}"
            ) from exc
        if default_weekly_budget <= 0:
            raise RuntimeError("YD_DEFAULT_WEEKLY_BUDGET должен быть больше 0")

    create_report_ssh_host = (
        os.getenv("YD_CREATE_REPORT_SSH_HOST", "").strip() or None
    )
    create_report_remote_root = os.getenv(
        "YD_CREATE_REPORT_REMOTE_ROOT",
        "",
    ).strip().rstrip("/")
    if create_report_remote_root and (
        not create_report_remote_root.startswith("/") or create_report_remote_root == "/"
        or ".." in create_report_remote_root.split("/")
    ):
        raise RuntimeError("YD_CREATE_REPORT_REMOTE_ROOT должен быть абсолютным путём")
    create_report_public_base_url = os.getenv(
        "YD_CREATE_REPORT_PUBLIC_BASE_URL", ""
    ).strip().rstrip("/")
    url = urlsplit(create_report_public_base_url)
    if create_report_public_base_url and (
        url.scheme not in {"http", "https"} or not url.hostname
        or url.username is not None or url.password is not None or url.query or url.fragment
    ):
        raise RuntimeError(
            "YD_CREATE_REPORT_PUBLIC_BASE_URL должен быть HTTP(S) URL"
        )

    if create_report_ssh_host and not (
        create_report_remote_root and create_report_public_base_url
    ):
        raise RuntimeError(
            "Публикация требует явных YD_CREATE_REPORT_REMOTE_ROOT и "
            "YD_CREATE_REPORT_PUBLIC_BASE_URL вместе с YD_CREATE_REPORT_SSH_HOST"
        )

    return Settings(
        wordstat_token=os.getenv("YD_WORDSTAT_TOKEN", "").strip(),
        metrika_token=os.getenv("YD_METRIKA_TOKEN", "").strip(),
        use_operator_units=_flag("YD_USE_OPERATOR_UNITS"),
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
        default_weekly_budget=default_weekly_budget,
        create_report_ssh_host=create_report_ssh_host,
        create_report_remote_root=create_report_remote_root,
        create_report_public_base_url=create_report_public_base_url,
    )
