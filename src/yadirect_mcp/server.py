"""MCP-сервер для отчётности и опциональной настройки кампаний с нуля.

Восемь тулов, а не сто двадцать. Тул-лист — это часть контекста и, что важнее,
пространство выбора для модели: чем он шире, тем чаще она мажет. Поэтому тул
соответствует задаче, а не методу API: `direct_account_settings` за один вызов
читает три сервиса, которые в разборе кампании нужны вместе.

Знания о самом Директе живут не в тулах, а в ресурсах (модуль knowledge):
инструкции едут в каждый запрос, поэтому в них остаётся только то, без чего
модель ошибётся молча, а разворачивание правил — по требованию.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import account, ads, campaign_setup, config, knowledge, regions, store, wordstat
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


def _instructions(settings: config.Settings) -> str:
    """Инструкции сервера: только то, что должно быть в контексте всегда.

    Всё остальное — в ресурсах direct://kb. Правила ниже отобраны по одному
    признаку: без них модель ошибается тихо. Деньги в неверных единицах уедут
    в Директ как бюджет в миллион раз больше, отсутствие автотаргетинга в
    поисковой группе не вызовет ошибку API, а перепутанный порядок
    подтверждения создаст кампанию без спроса.
    """
    parts = [
        (
            "Выгрузка статистики Яндекс Директа для отчётов. "
            "Клиентский логин агентства передаётся в каждый вызов параметром "
            "client_login; список логинов даёт direct_list_clients. "
            "direct_report пишет отчёт на диск и возвращает сводку — не проси его "
            "вернуть все строки, читай файл через direct_read_report постранично."
        ),
        (
            "Частотность запросов даёт direct_wordstat: спрос по фразе, вложенные "
            "и похожие запросы. Он про спрос в поиске, а не про статистику "
            "кабинета, и client_login ему не нужен."
        ),
        (
            "Коды регионов не угадывай: Директ их не проверяет и молча покажет "
            "данные и рекламу не по тому региону. Сверяй через direct_regions "
            "всё, что уходит в geo_ids и RegionIds."
        ),
        (
            "Корректировки ставок, условия ретаргетинга и общие наборы минус-фраз "
            "в отчётах не видны — их читает direct_account_settings. Разбирая "
            "срезы отчёта по устройствам, полу и возрасту, сначала проверь, какие "
            "корректировки выставлены."
        ),
        (
            "Ссылки на посадочные страницы в отчётах тоже нет — её отдаёт "
            "direct_ads вместе с текстами объявлений и разобранными UTM. "
            "Не рассуждай о качестве трафика и конверсии, не посмотрев, куда "
            "он ведёт."
        ),
        (
            "База знаний по настройке и оптимизации кампаний отдаётся ресурсами: "
            "оглавление direct://kb, документы direct://kb/<имя>. Перед настройкой "
            "с нуля и перед разбором «почему кампания не работает» читай нужный "
            "документ оттуда, а не полагайся на память: правила Директа менялись."
        ),
    ]
    if settings.mode != "campaign_setup":
        parts.append("Сервер работает в режиме report: методы записи недоступны.")
        return " ".join(parts)

    parts.append(
        "Режим настройки кампаний включён. Если пользователь просит создать "
        "кампанию с нуля, сначала собери сайт, цель, географию, бюджет, сроки, "
        "стратегию, счётчики/цели Метрики, минус-слова, ключевые фразы и тексты — "
        "полный список в direct://kb/launch-checklist, границы тула в "
        "direct://kb/server-capabilities. "
        "Сначала вызови direct_campaign_setup без confirmation, покажи полный "
        "preview и запроси явное подтверждение. Только после него повтори вызов "
        "с точной confirmation из preview. Не запускай показы автоматически."
    )
    rules = [
        "денежные поля API — целые в микроединицах, то есть сумма в валюте × 1 000 000",
        (
            "в поисковой группе автотаргетинг обязателен: добавь строку "
            '"---autotargeting" в Keywords группы'
        ),
        (
            "лимиты текстов проверяй до вызова (заголовок 56, второй заголовок 30, "
            "текст 81, отображаемая ссылка 20 знаков): ошибка вызова стоит 20 баллов"
        ),
    ]
    if settings.default_weekly_budget is not None:
        rules.append(
            f"недельный бюджет по умолчанию — {settings.default_weekly_budget:g} "
            "в валюте кабинета, если пользователь не назвал свой; называй эту "
            "сумму в сводке плана"
        )
    parts.append("Правила, которые нельзя нарушать: " + "; ".join(rules) + ".")
    return " ".join(parts)


mcp = FastMCP("yandex-direct", instructions=_instructions(SETTINGS))


# ── база знаний ──────────────────────────────────────────────────────────


@mcp.resource(
    "direct://kb",
    name="Яндекс Директ: база знаний",
    description="Оглавление документов по настройке и оптимизации кампаний",
    mime_type="text/markdown",
)
def kb_index() -> str:
    return knowledge.index()


@mcp.resource(
    "direct://kb/{doc}",
    name="Яндекс Директ: документ базы знаний",
    description="Документ базы знаний по имени из оглавления direct://kb",
    mime_type="text/markdown",
)
def kb_doc(doc: str) -> str:
    return knowledge.read(doc)

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


def _fail(exc: Exception, hint: str | None = None) -> str:
    log.warning("%s: %s", type(exc).__name__, exc)
    out: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, DirectError):
        out["error_code"] = exc.code
        out["request_id"] = exc.request_id
    if hint:
        out["hint"] = hint
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


@mcp.tool(annotations={"readOnlyHint": True})
async def direct_regions(
    query: str | None = None,
    ids: list[int] | None = None,
    client_login: str | None = None,
    limit: int = 20,
) -> str:
    """Коды регионов Директа по названию и обратная проверка готовых кодов.

    Вызывать всегда, когда в geo_ids (direct_wordstat) или RegionIds
    (direct_campaign_setup) уходит регион: Директ коды НЕ проверяет. Неверный
    код не вызовет ошибку — он молча даст данные и показы не по тому региону,
    и заметно это станет только по статистике.

    query: часть названия, например "Москва", "Ростов", "Татарстан". Регистр и
        «ё» не важны. Совпадения возвращаются с путём до корня (path), потому
        что названия неуникальны: «Москва» — это и город 213, и «Москва и
        область» 1.
    ids: коды для обратной проверки. Вернёт названия, а отсутствующие в
        справочнике коды — отдельным списком unknown_ids.
    client_login: нужен только агентскому токену — Директ требует заголовок
        Client-Login на клиентских методах. Подойдёт любой логин из
        direct_list_clients.
    limit: сколько совпадений вернуть, 1–200.

    Нужен хотя бы один из query / ids: справочник целиком тул не отдаёт.
    Загружается он один раз на процесс, повторные вызовы баллов не тратят.
    """
    try:
        if client_login:
            SETTINGS.check_login(client_login)
        payload = await regions.lookup(
            _api(), query=query, ids=ids, client_login=client_login, limit=limit
        )
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        hint = None
        if isinstance(exc, DirectError) and not client_login:
            hint = (
                "Если токен агентский, Директ требует Client-Login: повторите "
                "вызов с client_login любого клиента из direct_list_clients."
            )
        return _fail(exc, hint)


@mcp.tool(annotations={"readOnlyHint": True})
async def direct_account_settings(
    client_login: str,
    sections: list[str] | None = None,
    campaign_ids: list[int] | None = None,
) -> str:
    """Настройки кабинета, которых нет в отчётах: корректировки ставок, условия
    ретаргетинга, общие наборы минус-фраз.

    Нужен при разборе «почему кампания не работает» и перед выводами по срезам
    отчёта. Reports API покажет статистику по Device, Gender, Age, но не
    покажет выставленный коэффициент, а это разные диагнозы: «нет мобильных
    конверсий» и «на мобильные стоит −100%».

    sections: какие секции читать. Доступны bid_modifiers, retargeting_lists,
        negative_keyword_sets. Пусто — все три.
    campaign_ids: для каких кампаний смотреть корректировки. Пусто — сервер
        сам возьмёт неархивные кампании клиента, но не более 50; если их
        больше, в ответе будет truncated и число пропущенных.

    Секции независимы: ошибка в одной приходит полем error внутри неё, а
    остальные возвращаются как есть. Корректировки перемножаются между собой —
    как их читать, описано в direct://kb/targeting-adjustments.
    """
    try:
        SETTINGS.check_login(client_login)
        payload = await account.read(
            _api(),
            client_login,
            sections=sections,
            campaign_ids=campaign_ids,
        )
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True})
async def direct_ads(
    client_login: str,
    campaign_ids: list[int] | None = None,
    ad_group_ids: list[int] | None = None,
    ad_ids: list[int] | None = None,
    include_archived: bool = False,
    limit: int = 1000,
) -> str:
    """Объявления и посадочные страницы: куда ведёт реклама, что в заголовках,
    размечены ли ссылки UTM.

    Ссылки в отчётах не существует ни в одном типе: поле Href есть только
    здесь. Вызывать, когда разбор дошёл до вопросов «на какую страницу идёт
    группа», «одна ли это главная на весь аккаунт», «есть ли метки» — и перед
    любыми выводами про конверсию, потому что дорогой клик на нерелевантной
    странице выглядит в отчёте так же, как дорогой клик вообще.

    campaign_ids / ad_group_ids / ad_ids: чем сузить выборку. Пусто — все
        объявления клиента.
    include_archived: включить архивные, по умолчанию нет.
    limit: сколько объявлений забрать за вызов, 1–10000.

    В ответе landing_pages — сводка по уникальным URL с числом объявлений,
    кампаниями и разобранными UTM; domains — домены, на которые идёт реклама.
    Запрашивается блок TextAd: у графических, видео и смарт-объявлений href
    придёт пустым, их число видно в ads_without_href.
    """
    try:
        SETTINGS.check_login(client_login)
        payload = await ads.read(
            _api(),
            client_login,
            campaign_ids=campaign_ids,
            ad_group_ids=ad_group_ids,
            ad_ids=ad_ids,
            include_archived=include_archived,
            limit=limit,
        )
        return _ok(payload)
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


# ── Вордстат ─────────────────────────────────────────────────────────────


@mcp.tool(annotations={"readOnlyHint": True})
async def direct_wordstat(
    phrases: list[str],
    geo_ids: list[int] | None = None,
    min_shows: int = 0,
    top: int = 10,
) -> str:
    """Частотность Вордстата: сколько раз за месяц искали фразу, что искали
    вместе с ней и что искали похожего. Пишет полный список на диск, возвращает
    сводку по каждой запрошенной фразе.

    Нужен на сборке семантики (что брать в кампанию, где спрос есть, а где нет),
    на разборе «мало показов» и когда в отчёте надо отделить падение спроса от
    падения кампании.

    phrases: до 50 фраз за вызов. Операторы Директа работают: «!» фиксирует
        словоформу, кавычки ограничивают фразу, «+» держит стоп-слово.
    geo_ids: регионы Директа, например [225] — Россия, [213] — Москва. Пусто —
        без ограничения по региону. Директ коды не проверяет: неверный код
        молча вернёт данные не по тому региону.
    min_shows: отбросить подсказки с частотностью ниже порога.
    top: сколько подсказок каждого вида показать в ответе, 1–100.

    client_login не нужен: данные Вордстата общие для всех кабинетов. Значение
    Shows — спрос в поиске за месяц, а не прогноз показов кампании. В песочнице
    метод недоступен.
    """
    try:
        if SETTINGS.sandbox:
            raise ValueError(
                "Вордстат недоступен в песочнице: метод есть только в боевом API. "
                "Уберите YD_SANDBOX."
            )
        payload = await wordstat.lookup(
            _api(),
            phrases=phrases,
            geo_ids=geo_ids,
            out_dir=SETTINGS.out_dir,
            deadline_seconds=SETTINGS.report_deadline,
            min_shows=min_shows,
            top=top,
        )
        # Через _ok не отдаём: заголовок Units — это баллы v5, а Вордстат живёт
        # в v4 со своим счётчиком. Приписать сюда остаток v5 значит соврать.
        return json.dumps(payload, ensure_ascii=False, default=str)
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
