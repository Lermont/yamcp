"""MCP-сервер для отчётности и опциональной настройки кампаний с нуля.

Четыре тула, а не сто двадцать. Тул-лист — это часть контекста и, что важнее,
пространство выбора для модели: чем он шире, тем чаще она мажет. Всё, что нужно
для отчётности, — это Reports API плюс два справочника.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import campaign_setup, config, store
from .client import DirectClient, DirectError

# ВАЖНО: stdio-транспорт живёт на stdout. Любой print() туда ломает протокол.
# Логи — строго в stderr.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("yadirect-mcp")

SETTINGS = config.load()

mcp = FastMCP(
    "yandex-direct",
    instructions=(
        "Выгрузка статистики Яндекс Директа для отчётов. "
        "Клиентский логин агентства передаётся в каждый вызов параметром client_login; "
        "список логинов даёт direct_list_clients. "
        "direct_report пишет отчёт на диск и возвращает сводку — не проси его "
        "вернуть все строки, читай файл через direct_read_report постранично. "
        + (
            "Режим настройки кампаний включён. Если пользователь просит создать "
            "кампанию с нуля, сначала собери сайт, цель, географию, бюджет, сроки, "
            "стратегию, счётчики/цели Метрики, минус-слова, ключевые фразы и тексты. "
            "Сначала вызови direct_campaign_setup без confirmation, покажи полный "
            "preview и запроси явное подтверждение. Только после него повтори вызов "
            "с точной confirmation из preview. Не запускай показы автоматически."
            if SETTINGS.mode == "campaign_setup"
            else "Сервер работает в режиме report: методы записи недоступны."
        )
    ),
)

_client: DirectClient | None = None


def _api() -> DirectClient:
    global _client
    if _client is None:
        _client = DirectClient(SETTINGS)
    return _client


def _ok(payload: dict[str, Any]) -> str:
    api = _api()
    if api.last_units:
        payload["units"] = api.last_units.as_dict()
    return json.dumps(payload, ensure_ascii=False, default=str)


def _fail(exc: Exception) -> str:
    log.warning("%s: %s", type(exc).__name__, exc)
    out: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, DirectError):
        out["error_code"] = exc.code
        out["request_id"] = exc.request_id
    return json.dumps(out, ensure_ascii=False)


# ── справочники ──────────────────────────────────────────────────────────


@mcp.tool(annotations={"readOnlyHint": True})
async def direct_list_clients(limit: int = 1000) -> str:
    """Список клиентских логинов агентства с валютой и остатком средств.

    Отсюда берётся client_login для остальных тулов. Вызывается без
    заголовка Client-Login — это агентский метод.
    """
    try:
        result = await _api().call(
            "agencyclients",
            "get",
            {
                "SelectionCriteria": {},
                "FieldNames": ["Login", "ClientId", "ClientInfo", "Currency", "Type"],
                "Page": {"Limit": limit},
            },
            client_login=None,
        )
        clients = [
            {
                "login": c.get("Login"),
                "client_id": c.get("ClientId"),
                "name": c.get("ClientInfo"),
                "currency": c.get("Currency"),
            }
            for c in result.get("Clients", [])
        ]
        return _ok({"clients": clients, "count": len(clients)})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True})
async def direct_campaigns(client_login: str, include_archived: bool = False) -> str:
    """Кампании клиента: id, имя, тип, статус. Нужен для маппинга CampaignId → имя
    и чтобы понять, какие кампании вообще стоит тянуть в отчёт.
    """
    try:
        SETTINGS.check_login(client_login)
        states = ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED"]
        if include_archived:
            states.append("ARCHIVED")
        result = await _api().call(
            "campaigns",
            "get",
            {
                "SelectionCriteria": {"States": states},
                "FieldNames": ["Id", "Name", "Type", "State", "Status", "StartDate"],
                "Page": {"Limit": 10000},
            },
            client_login=client_login,
        )
        campaigns = [
            {
                "id": c.get("Id"),
                "name": c.get("Name"),
                "type": c.get("Type"),
                "state": c.get("State"),
                "status": c.get("Status"),
            }
            for c in result.get("Campaigns", [])
        ]
        return _ok({"client_login": client_login, "campaigns": campaigns,
                    "count": len(campaigns)})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ── отчёты ───────────────────────────────────────────────────────────────


@mcp.tool(annotations={"readOnlyHint": True})
async def direct_report(
    client_login: str,
    date_from: str,
    date_to: str,
    fields: list[str],
    report_type: str = "CUSTOM_REPORT",
    goals: list[str] | None = None,
    attribution_models: list[str] | None = None,
    filters: list[dict] | None = None,
    order_by: list[dict] | None = None,
    limit: int | None = None,
    include_vat: bool = True,
) -> str:
    """Выгрузить статистику через Reports API. Пишет TSV на диск, возвращает
    путь + итоги + первые строки.

    client_login: клиентский логин (см. direct_list_clients)
    date_from / date_to: YYYY-MM-DD. Статистика доступна за 3 последних года.
    fields: колонки отчёта, например ["Date","CampaignName","Impressions","Clicks","Cost"].
        Набор допустимых полей зависит от report_type. Внимание: поля разных
        классов — сегмент (даёт группировку), метрика, атрибут, фильтр
        (используется только в filters и в отчёт не выводится, напр. Keyword).
    report_type: CUSTOM_REPORT (самый общий, по умолчанию) | ACCOUNT_PERFORMANCE_REPORT |
        CAMPAIGN_PERFORMANCE_REPORT | ADGROUP_PERFORMANCE_REPORT | AD_PERFORMANCE_REPORT |
        CRITERIA_PERFORMANCE_REPORT | SEARCH_QUERY_PERFORMANCE_REPORT |
        REACH_AND_FREQUENCY_PERFORMANCE_REPORT
    goals: ID целей Метрики, например ["12345678"]. Без них не будет Conversions.
    attribution_models: FC | LC | LSC | LYDC | FCCD | LSCCD | LYDCCD | AUTO.
        Работает только вместе с goals; по умолчанию LSC. Несколько моделей →
        данные выводятся по каждой отдельно.
    filters: [{"Field":"CampaignId","Operator":"IN","Values":["123","456"]}]
    order_by: [{"Field":"Cost","SortOrder":"DESCENDING"}]
    limit: ограничение строк. Требует сортировки — если не задана, подставим по
        первому полю.
    include_vat: суммы с НДС (True) или без (False).
    """
    try:
        SETTINGS.check_login(client_login)
        if not fields:
            raise ValueError("fields пустой — укажите хотя бы одну колонку")
        try:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to)
        except ValueError as exc:
            raise ValueError("date_from и date_to должны иметь формат YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("date_from не может быть позже date_to")

        selection: dict[str, Any] = {"DateFrom": date_from, "DateTo": date_to}
        if filters:
            selection["Filter"] = filters

        spec: dict[str, Any] = {
            "SelectionCriteria": selection,
            "FieldNames": list(fields),
            "ReportType": report_type,
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES" if include_vat else "NO",
            "IncludeDiscount": "NO",
        }
        if goals:
            spec["Goals"] = [str(g) for g in goals]
            # AttributionModels валиден только при заданных Goals.
            if attribution_models:
                spec["AttributionModels"] = attribution_models
        elif attribution_models:
            raise ValueError("attribution_models можно указывать только вместе с goals")

        if order_by:
            spec["OrderBy"] = order_by
        if limit:
            # Page без OrderBy Директ не любит — подстрахуемся.
            spec.setdefault("OrderBy", [{"Field": fields[0]}])
            spec["Page"] = {"Limit": int(limit)}

        api = _api()
        tsv = await api.report(spec, client_login=client_login)

        # Логин попадает в имя файла, поэтому чистит его store.safe_stem.
        stem = f"{client_login}_{date_from}_{date_to}_{api.report_name(spec)}"
        payload = store.persist(
            tsv,
            out_dir=SETTINGS.out_dir,
            stem=stem,
            inline_rows=SETTINGS.inline_rows,
        )
        payload["client_login"] = client_login
        payload["include_vat"] = include_vat
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True})
async def direct_read_report(path: str, offset: int = 0, limit: int = 100) -> str:
    """Постранично прочитать уже выгруженный отчёт по пути из direct_report.
    Без повторного обращения к API и без расхода баллов.
    """
    try:
        # Относительный путь считаем от каталога выгрузок, а не от текущего
        # каталога процесса: модель обычно передаёт просто имя файла.
        raw = Path(path)
        p = (raw if raw.is_absolute() else SETTINGS.out_dir / raw).resolve()
        # Не выпускаем модель за пределы каталога выгрузок.
        if not p.is_relative_to(SETTINGS.out_dir):
            raise ValueError(f"Путь вне {SETTINGS.out_dir}")
        if not p.exists():
            raise FileNotFoundError(f"Нет файла {p}")
        if not p.is_file() or p.suffix.lower() != ".tsv":
            raise ValueError("Можно читать только TSV-файлы отчётов")
        return json.dumps(
            store.read_back(p, offset, limit), ensure_ascii=False, default=str
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ── настройка кампании с нуля (явно включаемый write-режим) ─────────────


async def direct_campaign_setup(
    client_login: str,
    campaign: dict[str, Any],
    ad_groups: list[dict[str, Any]],
    confirmation: str | None = None,
) -> str:
    """Preview или создание одной текстово-графической кампании с нуля.

    Инструмент доступен только при YD_MODE=campaign_setup. Первый вызов всегда
    делайте БЕЗ confirmation: он проверит и вернёт полный план и точную фразу
    подтверждения. Передавать её можно только после явного согласия пользователя.

    campaign: объект CampaignAddItem API Директа без Id. Обязательны Name,
        StartDate и TextCampaign с BiddingStrategy. Денежные поля API передаются
        в микроединицах (рубли × 1_000_000).
    ad_groups: группы AdGroupAddItem без CampaignId. В каждой обязательны Name,
        RegionIds и локальный массив Ads. В Ads передаются AdAddItem без
        AdGroupId; сейчас поддержан TextAd. Необязательный Keywords принимает
        строки либо KeywordAddItem без AdGroupId.
    confirmation: точное значение confirmation_required из preview. Без него
        API не вызывается. Кампания и объявления создаются выключенными; метод
        не вызывает resume и не отправляет на запуск.

    Операция не атомарна: при частичной ошибке ответ содержит созданные ID и
    ошибки по каждому объекту. Не повторяйте весь apply вслепую.
    """
    try:
        if SETTINGS.mode != "campaign_setup":
            raise PermissionError(
                "Настройка кампаний выключена. Запустите сервер с "
                "YD_MODE=campaign_setup."
            )
        SETTINGS.check_login(client_login)
        expected = campaign_setup.confirmation_phrase(client_login)
        if confirmation != expected:
            return _ok(campaign_setup.preview(client_login, campaign, ad_groups))
        result = await campaign_setup.apply(
            _api(), client_login, campaign, ad_groups
        )
        return _ok(result)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


if SETTINGS.mode == "campaign_setup":
    mcp.tool(
        annotations={"readOnlyHint": False, "destructiveHint": True}
    )(direct_campaign_setup)


def main() -> None:
    log.info(
        "yandex-direct MCP: mode=%s, out_dir=%s, sandbox=%s, белый список логинов=%s",
        SETTINGS.mode,
        SETTINGS.out_dir,
        SETTINGS.sandbox,
        len(SETTINGS.allowed_logins) or "выкл",
    )
    mcp.run()


if __name__ == "__main__":
    main()
