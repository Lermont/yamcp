"""Клиент Yandex Direct API v5/v501 и совместимых методов v4.

Что здесь важного и чего нет в готовых репах:

1. Reports API асинхронный. 200 = готово, 201 = поставлен в офлайн-очередь,
   202 = ещё формируется. Обе промежуточные отдают ПУСТОЕ тело. Кто проверяет
   `resp.ok` (а это `< 400`), тот молча возвращает пустую строку как отчёт.

2. ReportName участвует в идентификации отчёта. При поллинге нужно слать
   РОВНО тот же запрос, значит имя обязано быть стабильным между попытками.
   И при этом уникальным для разного набора полей, иначе Директ ругнётся на
   дубль имени. Отсюда — имя как хеш от спецификации.
   Побочный бонус: готовые офлайн-отчёты живут 5 часов, поэтому повтор
   идентичного запроса в пределах этого окна вернёт 200 сразу и бесплатно.

3. В очереди одновременно не более 5 офлайн-отчётов на пользователя.
   Держим общий семафор на экземпляр клиента с одним пользовательским токеном.
   reportsInQueue — диагностический снимок: он включает запросы других программ
   и не заменяет ограничение конкурентности или обработку ошибки API.

4. Заголовок Units: "потрачено/остаток/суточный лимит". Отдаём наверх, чтобы
   модель видела, сколько баллов сожгла, и сама притормаживала.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Self
from weakref import WeakKeyDictionary

import httpx

log = logging.getLogger("yadirect-mcp")

API_URL = "https://api.direct.yandex.com/json/v5"
SANDBOX_URL = "https://api-sandbox.direct.yandex.com/json/v5"
API_V501_URL = "https://api.direct.yandex.com/json/v501"
SANDBOX_V501_URL = "https://api-sandbox.direct.yandex.com/json/v501"

# Legacy fallback without a separate Wordstat token; modern API lives on its own origin.
API_V4_URL = "https://api.direct.yandex.ru/v4/json/"

# 502/503/504 отдаёт балансировщик, а не сам Директ. Отчёт при этом уже стоит
# в офлайн-очереди, и повтор того же запроса (имя стабильно) его же и заберёт,
# так что сдаваться на первом таком ответе — значит терять готовую работу.
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
TRANSIENT_RETRIES = 3


@dataclass(frozen=True)
class Units:
    """Расход баллов API."""

    spent: int
    rest: int
    daily: int

    def as_dict(self) -> dict[str, int]:
        return {"spent": self.spent, "rest": self.rest, "daily": self.daily}


@dataclass(frozen=True)
class UnitsMark:
    """Позиция журнала и владелец одной составной MCP-операции."""

    sequence: int
    task_id: int | None


class DirectError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        detail: str = "",
        request_id: str | None = None,
        status: int | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.request_id = request_id
        self.status = status
        parts = [message]
        if detail:
            parts.append(detail)
        if code is not None:
            parts.append(f"(error_code={code})")
        if request_id:
            parts.append(f"RequestId={request_id}")
        super().__init__(" ".join(parts))


def _decode(resp: httpx.Response) -> str:
    """Директ иногда врёт про кодировку в Content-Type. Декодируем сами."""
    return resp.content.decode("utf-8", errors="replace")


def _retry_in(resp: httpx.Response, default: int = 5) -> int:
    """Сколько ждать до следующей попытки.

    Заголовок может прийти пустым или мусорным (прокси, кеш, страница ошибки),
    и int() на нём валит весь поллинг вместе с уже заказанным отчётом.
    """
    raw = (resp.headers.get("retryIn") or "").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return default


class DirectClient:
    def __init__(self, settings) -> None:
        self._s = settings
        self._base = SANDBOX_URL if settings.sandbox else API_URL
        self._base_v501 = SANDBOX_V501_URL if settings.sandbox else API_V501_URL
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
        self._slots = asyncio.Semaphore(min(5, settings.max_inflight))
        self.last_units: Units | None = None
        self._units_by_login: dict[str, Units] = {}
        self._task_ids: WeakKeyDictionary = WeakKeyDictionary()
        self._task_sequence = 0
        self._units_sequence = 0
        self._units_history: deque[dict[str, Any]] = deque(maxlen=1000)
        self.reports_in_queue: int | None = None
        self._report_settings_cache: dict = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── заголовки ────────────────────────────────────────────────────────

    def _headers(
        self, client_login: str | None, extra: dict[str, str] | None = None
    ) -> dict[str, str]:
        h = {
            # Слово Bearer обязательно.
            "Authorization": f"Bearer {self._s.token}",
            "Accept-Language": self._s.lang,
            "Content-Type": "application/json; charset=utf-8",
        }
        # Client-Login — HTTP-заголовок, а не поле в params. Задаётся на запрос,
        # не на сессию: один агентский токен ходит по многим клиентским логинам.
        if client_login:
            h["Client-Login"] = client_login
        if getattr(self._s, "use_operator_units", False):
            h["Use-Operator-Units"] = "true"
        if extra:
            h.update(extra)
        return h

    def _note_units(
        self,
        resp: httpx.Response,
        *,
        api_version: str,
        service: str,
        method: str,
        client_login: str | None,
    ) -> None:
        raw = resp.headers.get("Units", "")
        parts = raw.replace(" ", "").split("/")
        if len(parts) == 3:
            with suppress(ValueError):
                units = Units(int(parts[0]), int(parts[1]), int(parts[2]))
                self.last_units = units
                self._units_by_login[(client_login or "").casefold()] = units
                self._units_sequence += 1
                self._units_history.append({
                    "sequence": self._units_sequence,
                    "_task_id": self._current_task_id(),
                    "api_version": api_version,
                    "service": service,
                    "method": method,
                    "client_login": client_login,
                    "spent": units.spent,
                    "rest": units.rest,
                    "daily": units.daily,
                    "units_used_login": resp.headers.get("Units-Used-Login"),
                    "request_id": resp.headers.get("RequestId"),
                })
        q = resp.headers.get("reportsInQueue")
        if q is not None:
            with suppress(ValueError):
                self.reports_in_queue = int(q)

    def _current_task_id(self) -> int | None:
        with suppress(RuntimeError):
            task = asyncio.current_task()
            if task is not None:
                if task not in self._task_ids:
                    self._task_sequence += 1
                    self._task_ids[task] = self._task_sequence
                return self._task_ids[task]
        return None

    def units_for(self, client_login: str) -> Units | None:
        """Never use another advertiser's last response as this client's balance."""
        return self._units_by_login.get(client_login.casefold())

    def units_mark(self) -> UnitsMark:
        """Вернуть позицию журнала для текущей составной операции."""
        return UnitsMark(self._units_sequence, self._current_task_id())

    def units_since(self, mark: int | UnitsMark) -> dict[str, Any]:
        """Суммировать фактические Units текущей операции после ``mark``."""
        if isinstance(mark, UnitsMark):
            sequence = mark.sequence
            task_id = mark.task_id
            filter_by_task = True
        elif isinstance(mark, int):
            sequence = mark
            task_id = None
            filter_by_task = False
        else:
            raise ValueError("Некорректная отметка журнала Units")
        if sequence < 0 or sequence > self._units_sequence:
            raise ValueError("Некорректная отметка журнала Units")
        history = list(self._units_history)
        selected = [
            row
            for row in history
            if row["sequence"] > sequence
            and (not filter_by_task or row["_task_id"] == task_id)
        ]
        rows = []
        for source in selected:
            row = dict(source)
            row.pop("_task_id", None)
            rows.append(row)
        earliest = history[0]["sequence"] if history else self._units_sequence + 1
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["api_version"], row["service"], row["method"])
            item = grouped.setdefault(key, {
                "api_version": row["api_version"],
                "service": row["service"],
                "method": row["method"],
                "requests": 0,
                "spent": 0,
            })
            item["requests"] += 1
            item["spent"] += row["spent"]
        latest = rows[-1] if rows else None
        return {
            "scope": "current_mcp_operation",
            "requests_with_units": len(rows),
            "spent": sum(row["spent"] for row in rows),
            "rest": latest["rest"] if latest else None,
            "daily": latest["daily"] if latest else None,
            "truncated": bool(history and sequence < earliest - 1),
            "by_operation": list(grouped.values()),
            "requests": rows,
        }

    # ── обычный JSON API ─────────────────────────────────────────────────

    async def _call_once(
        self,
        base: str,
        service: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        client_login: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"method": method}
        if params is not None:
            body["params"] = params

        resp = await self._http.post(
            f"{base}/{service}",
            headers=self._headers(client_login),
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        self._note_units(
            resp,
            api_version="v501" if base == self._base_v501 else "v5",
            service=service,
            method=method,
            client_login=client_login,
        )
        if service.lower() in {"campaigns", "strategies"} and method.lower() != "get":
            self._report_settings_cache.clear()

        if resp.status_code != 200:
            raise DirectError(
                f"{service}.{method}: HTTP {resp.status_code}",
                status=resp.status_code,
                detail=_decode(resp)[:500],
                request_id=resp.headers.get("RequestId"),
            )

        try:
            data = json.loads(_decode(resp))
        except json.JSONDecodeError as exc:
            raise DirectError(
                f"{service}.{method}: некорректный JSON в ответе",
                status=resp.status_code,
                detail=_decode(resp)[:500],
                request_id=resp.headers.get("RequestId"),
            ) from exc
        if "error" in data:
            e = data["error"]
            raise DirectError(
                f"{service}.{method}: {e.get('error_string', 'ошибка')}",
                code=e.get("error_code"),
                detail=e.get("error_detail", ""),
                request_id=e.get("request_id"),
            )
        return data.get("result", {})

    async def _call_at(self, base: str, service: str, method: str,
                       params: dict[str, Any] | None = None, *,
                       client_login: str | None = None) -> dict[str, Any]:
        attempts = TRANSIENT_RETRIES + 1 if method.lower() == "get" else 1
        for attempt in range(attempts):
            try:
                return await self._call_once(base, service, method, params,
                                             client_login=client_login)
            except (httpx.TransportError, DirectError) as exc:
                transient = isinstance(exc, httpx.TransportError) or (
                    exc.status in TRANSIENT_STATUSES or exc.status == 429
                    or exc.code in {52, 506, 1000, 1001, 1002, 1020})
                if not transient or attempt + 1 == attempts:
                    raise
                await asyncio.sleep(min(2 ** attempt, 8))
        raise RuntimeError("Unreachable retry state")

    async def call(
        self,
        service: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        client_login: str | None = None,
    ) -> dict[str, Any]:
        """Вызвать стабильную JSON-ветку v5."""
        return await self._call_at(
            self._base,
            service,
            method,
            params,
            client_login=client_login,
        )

    async def call_v501(
        self,
        service: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        client_login: str | None = None,
    ) -> dict[str, Any]:
        """Вызвать JSON v501 — ветку, необходимую для Единой кампании."""
        return await self._call_at(
            self._base_v501,
            service,
            method,
            params,
            client_login=client_login,
        )

    # ── Reports ──────────────────────────────────────────────────────────

    @staticmethod
    def report_name(spec: dict[str, Any]) -> str:
        """Детерминированное имя: стабильно между поллингами, уникально на спеку."""
        blob = json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return "r_" + hashlib.sha1(blob).hexdigest()[:16]

    async def report(self, spec: dict[str, Any], *, client_login: str) -> str:
        """Возвращает сырой TSV. Спека — без ReportName, он подставится сам."""
        spec = dict(spec)
        spec.pop("ReportName", None)
        spec["ReportName"] = self.report_name(spec)

        headers = self._headers(
            client_login,
            {
                "processingMode": "auto",
                # Иначе деньги приедут в микрорублях (× 1 000 000).
                "returnMoneyInMicros": "false",
                # Шапка и строка с числом строк модели не нужны — только мусор
                # в контексте. Имена колонок (skipColumnHeader) ОСТАВЛЯЕМ.
                "skipReportHeader": "true",
                "skipReportSummary": "true",
                "Accept-Encoding": "gzip",
            },
        )
        payload = json.dumps({"params": spec}, ensure_ascii=False).encode("utf-8")
        # Актуальная схема Reports опубликована для v501. Вызов через v5 долго
        # оставался совместимым, из-за чего тесты не замечали устаревший URL,
        # но новые поля и модели атрибуции валидируются уже по контракту v501.
        url = f"{self._base_v501}/reports"
        deadline = time.monotonic() + self._s.report_deadline
        transient_retries = TRANSIENT_RETRIES

        async with self._slots:
            while True:
                resp = await self._http.post(url, headers=headers, content=payload)
                self._note_units(
                    resp,
                    api_version="v501",
                    service="reports",
                    method="request",
                    client_login=client_login,
                )
                code = resp.status_code

                if code == 200:
                    return _decode(resp)

                if code in (201, 202):
                    # 201 — поставлен в очередь, 202 — ещё считается.
                    # Тело пустое. Ждём retryIn и шлём ТОТ ЖЕ запрос.
                    if time.monotonic() > deadline:
                        raise DirectError(
                            f"Отчёт не готов за {self._s.report_deadline:.0f} c "
                            f"(логин {client_login}). Сузьте период или набор полей.",
                            status=code,
                            request_id=resp.headers.get("RequestId"),
                        )
                    wait = _retry_in(resp)
                    log.info(
                        "report %s: HTTP %s, ждём %s c (в очереди: %s)",
                        client_login, code, wait, self.reports_in_queue,
                    )
                    await asyncio.sleep(wait)
                    continue

                if (
                    code in TRANSIENT_STATUSES
                    and transient_retries > 0
                    and time.monotonic() < deadline
                ):
                    transient_retries -= 1
                    wait = _retry_in(resp)
                    log.warning(
                        "report %s: HTTP %s, повтор через %s c (осталось попыток: %s)",
                        client_login, code, wait, transient_retries,
                    )
                    await asyncio.sleep(wait)
                    continue

                raise self._report_error(resp)

    @staticmethod
    def _report_error(resp: httpx.Response) -> DirectError:
        """400 приходит с JSON-телом; 500 и прочее — как получится."""
        raw = _decode(resp)
        request_id = resp.headers.get("RequestId")
        try:
            err = json.loads(raw).get("error", {})
            return DirectError(
                err.get("error_string", f"Reports: HTTP {resp.status_code}"),
                code=err.get("error_code"),
                detail=err.get("error_detail", ""),
                request_id=err.get("request_id") or request_id,
                status=resp.status_code,
            )
        except (json.JSONDecodeError, AttributeError):
            return DirectError(
                f"Reports: HTTP {resp.status_code}",
                detail=raw[:500],
                request_id=request_id,
                status=resp.status_code,
            )

    @property
    def modern_wordstat(self) -> bool:
        return bool(self._s.wordstat_token)

    @property
    def metrika_available(self) -> bool:
        return bool(self._s.metrika_token)

    async def wordstat_top_requests(self, phrase: str, regions: list[int] | None) -> dict:
        if not self._s.wordstat_token:
            raise ValueError("Для отдельного API Вордстата задайте YD_WORDSTAT_TOKEN")
        response = await self._http.post(
            "https://api.wordstat.yandex.net/v1/topRequests",
            headers={"Authorization": "Bearer " + self._s.wordstat_token},
            json={"phrase": phrase, **({"regions": regions} if regions else {})},
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result.get("topRequests"), list):
            raise ValueError("Некорректный ответ Wordstat topRequests")
        return result

    async def metrika_counter(self, counter_id: int) -> dict:
        from .identifiers import parse_id
        identifier = parse_id(counter_id, "counter_id")
        if not self._s.metrika_token:
            raise ValueError("Проверка счётчика требует YD_METRIKA_TOKEN с доступом на чтение")
        response = await self._http.get(
            f"https://api-metrika.yandex.net/management/v1/counter/{identifier}",
            headers={"Authorization": "OAuth " + self._s.metrika_token},
        )
        response.raise_for_status()
        result = response.json().get("counter", {})
        if result.get("id") != identifier or result.get("status") == "Deleted":
            raise ValueError("Счётчик отсутствует или удалён")
        return {key: result.get(key) for key in ("id", "name", "site", "status", "permission")}

    # ── Wordstat: API v4 ─────────────────────────────────────────────────

    async def call_v4(self, method: str, param: Any = None) -> Any:
        """Вызов метода API v4. Отличий от v5 больше, чем сходства.

        1. Токен уходит ПОЛЕМ ТЕЛА, а не заголовком. На `Authorization` v4
           отвечает error_code 53 «Authorization error», то есть выглядит как
           протухший токен, хотя дело в транспорте.
        2. Ошибка приезжает с HTTP 200 и без ключа `error`: плоские
           `error_code` / `error_str` / `error_detail` в корне ответа. Проверка
           статуса или `if "error" in data` не поймает ничего.
        3. Баллы v4 считаются отдельно от v5 и в заголовках не приходят,
           поэтому `last_units` тут не трогаем: смешивать два счётчика — врать
           модели про остаток.
        """
        body: dict[str, Any] = {
            "method": method,
            "token": self._s.token,
            "locale": self._s.lang,
        }
        if param is not None:
            body["param"] = param

        resp = await self._http.post(
            API_V4_URL,
            headers={"Content-Type": "application/json; charset=utf-8"},
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        if resp.status_code != 200:
            raise DirectError(
                f"v4.{method}: HTTP {resp.status_code}",
                status=resp.status_code,
                detail=_decode(resp)[:500],
                request_id=resp.headers.get("RequestId"),
            )

        try:
            data = json.loads(_decode(resp))
        except json.JSONDecodeError as exc:
            raise DirectError(
                f"v4.{method}: некорректный JSON в ответе",
                status=resp.status_code,
                detail=_decode(resp)[:500],
                request_id=resp.headers.get("RequestId"),
            ) from exc

        if not isinstance(data, dict):
            raise DirectError(f"v4.{method}: неожиданный ответ", detail=str(data)[:500])
        if data.get("error_code") is not None or data.get("error_str"):
            raise DirectError(
                f"v4.{method}: {data.get('error_str', 'ошибка')}",
                code=data.get("error_code"),
                detail=data.get("error_detail", ""),
                request_id=resp.headers.get("RequestId"),
            )
        return data.get("data")
