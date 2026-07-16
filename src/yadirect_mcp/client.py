"""Клиент Yandex Direct API v5.

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
   На батче из сотен клиентов это ловится мгновенно. Держим семафор на логин
   и дополнительно смотрим на заголовок ответа reportsInQueue.

4. Заголовок Units: "потрачено/остаток/суточный лимит". Отдаём наверх, чтобы
   модель видела, сколько баллов сожгла, и сама притормаживала.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("yadirect-mcp")

API_URL = "https://api.direct.yandex.com/json/v5"
SANDBOX_URL = "https://api-sandbox.direct.yandex.com/json/v5"


@dataclass(frozen=True)
class Units:
    """Расход баллов API."""

    spent: int
    rest: int
    daily: int

    def as_dict(self) -> dict[str, int]:
        return {"spent": self.spent, "rest": self.rest, "daily": self.daily}


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


class DirectClient:
    def __init__(self, settings) -> None:
        self._s = settings
        self._base = SANDBOX_URL if settings.sandbox else API_URL
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0))
        self._slots: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.max_inflight)
        )
        self.last_units: Units | None = None
        self.reports_in_queue: int | None = None

    async def __aenter__(self) -> "DirectClient":
        return self

    async def __aexit__(self, *_: object) -> None:
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
        if extra:
            h.update(extra)
        return h

    def _note_units(self, resp: httpx.Response) -> None:
        raw = resp.headers.get("Units", "")
        parts = raw.replace(" ", "").split("/")
        if len(parts) == 3:
            try:
                self.last_units = Units(int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                pass
        q = resp.headers.get("reportsInQueue")
        if q is not None:
            try:
                self.reports_in_queue = int(q)
            except ValueError:
                pass

    # ── обычный JSON API ─────────────────────────────────────────────────

    async def call(
        self,
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
            f"{self._base}/{service}",
            headers=self._headers(client_login),
            content=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        self._note_units(resp)

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
        url = f"{self._base}/reports"
        deadline = time.monotonic() + self._s.report_deadline
        server_error_retries = 1

        async with self._slots[client_login.lower()]:
            while True:
                resp = await self._http.post(url, headers=headers, content=payload)
                self._note_units(resp)
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
                    wait = int(resp.headers.get("retryIn", "5") or 5)
                    log.info(
                        "report %s: HTTP %s, ждём %s c (в очереди: %s)",
                        client_login, code, wait, self.reports_in_queue,
                    )
                    await asyncio.sleep(max(1, wait))
                    continue

                if code == 500 and server_error_retries > 0:
                    server_error_retries -= 1
                    await asyncio.sleep(5)
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
