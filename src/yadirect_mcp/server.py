"""MCP-сервер для отчётности, аудита и опциональной настройки кампаний.

Компактный набор тулов вместо отражения каждого метода API. Тул-лист — часть
контекста и, что важнее,
пространство выбора для модели: чем он шире, тем чаще она мажет. Поэтому тул
соответствует задаче, а не методу API: `direct_account_settings` за один вызов
читает три сервиса, которые в разборе кампании нужны вместе.

Знания о самом Директе живут не в тулах, а в ресурсах (модуль knowledge):
инструкции едут в каждый запрос, поэтому в них остаётся только то, без чего
модель ошибётся молча, а разворачивание правил — по требованию.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.types import CallToolResult, TextContent

from . import (
    account,
    adgroups,
    ads,
    approval,
    artifacts,
    assets,
    audit,
    bundle,
    campaigns,
    config,
    creation_report,
    executor,
    feeds,
    goals,
    identifiers,
    jobs,
    keywords,
    knowledge,
    landing,
    operations,
    planning,
    policy,
    products,
    publication,
    regions,
    repair,
    report_context,
    report_pipeline,
    reports,
    runtime,
    store,
    verification,
    wordstat,
    workflow,
)
from .client import DirectClient, DirectError

# ВАЖНО: stdio-транспорт живёт на stdout. Любой print() туда ломает протокол.
# Логи — строго в stderr.
log = logging.getLogger("yadirect-mcp")

SETTINGS = config.load()


def _instructions(settings: config.Settings) -> str:
    """Only invariant safety and routing rules belong in every model context."""
    parts = [
        "Яндекс Директ: client_login берётся из direct_list_clients и передаётся явно. "
        "Коды geo_ids/RegionIds проверяй через direct_regions, не угадывай. "
        "direct_report сохраняет TSV и метаданные, direct_read_report читает страницы. "
        "Для конверсий проверь цели и атрибуцию кампаний; разные настройки раздели "
        "фильтром CampaignId либо явно задай goals и attribution_models. Агрегат всех "
        "целей не равен заявкам. Перед выводами о трафике проверь direct_ads и "
        "direct_account_settings. Wordstat показывает спрос, а не показы кампании. "
        "Аудит: PASS/WARNING/BLOCK/MANUAL. План и аудит возвращают сводку и artifact_path; "
        "всегда проверяй конечные ссылки с эффективными метками и подставленными "
        "параметрами, включая быстрые ссылки и мобильный вариант. HTTP 200 у Href "
        "без меток недостаточно; для названия кампании используй yd_campaign_name, не name. "
        "полные данные читай direct_read_artifact по JSON Pointer и next_offset. "
        "Браузерные действия всегда выполняй через Playwright с установленным Chrome: "
        "карточки, кнопки, карусели, загрузку фото и проверку отчётов. "
        "Используй отдельный авторизованный профиль; не переключайся на CUA или браузерный плагин "
        "без явной просьбы пользователя. После сохранения повторно открой объект "
        "и проверь результат. "
        "База знаний: direct://kb; параметры тулов: direct://kb/tool-reference. "
        "Перед планированием читай direct://kb/structure-semantics и agency-policy. "
        "Бизнес-профили: direct://kb/policy-profiles; товары: direct://kb/product-campaigns; "
        "direct_policy возвращает available_profiles и текущие planning_defaults. "
        "Для профильного аудита передавай policy_name из плана, не подменяй его legacy-профилем. "
        "Единый клиентский отчёт Media Targeting создавай при настройке на "
        "https://bi-data.ru/elama/<client_login>/; затем дополняй его статистикой "
        "по тому же адресу, сохраняя настройку и предыдущие периоды. "
        "В левом меню — Настройка и Статистика; порядок, шаблон и контакты: "
        "direct://kb/client-report. Перед обновлением прочитай текущий отчёт "
        "и его данные, сохрани резервную копию; не заменяй весь отчёт одной статистикой."
        " Для зарегистрированного отчёта используй direct_client_report: update собирает "
        "цифры и HTML кодом, возвращает компактный brief; write_insight сохраняет только "
        "вывод по data_revision. Не читай полный HTML/TSV для обычного обновления."
    ]
    if settings.mode != "campaign_setup":
        return " ".join(parts) + " Режим report: методы записи в кабинет недоступны."
    parts.append(planning.INSTRUCTIONS)
    parts.append(
        "Настройка: прочитай direct://kb/launch-checklist и server-capabilities. "
        "Для нового клиента явно выбери policy_name: services_b2b, local_business или ecommerce "
        "с суффиксом _new_v1 без истории либо _established_v1 со статистикой. "
        "agency_default_v1 сохранён для воспроизведения прежних bundle; не мигрируй их молча. "
        "В profile_context укажи типы бизнес-целей и проверку измерения; для established нужны "
        "источники статистики с логином, каналом, географией, целью, счётчиком, периодом, "
        "конверсиями и признаками complete/reviewed. Не выдумывай эти данные. "
        "Автоматический выбор конверсий возможен по одной цели: период от 7 дней, "
        "окончание не старше 30 дней, среднее от 10 конверсий в неделю; иначе максимум кликов. "
        "Это критерий профиля, не гарантированный результат и не универсальный запрет API. "
        "Проверь бюджет и качество лидов; сам MCP не удостоверяет содержание источника. "
        "Для конверсий запрещено allow_unverified_goals=true. Local требует офис/Карты/контакты; "
        "Для товарного сценария ecommerce выбери _new_v2 или _established_v2: "
        "catalog_reviewed и покупка как цель. CRR пока не создаётся. "
        "Адрес фида бери из задания, включая отдельно переданный URL. "
        "direct_product_source читает фиды и проверяет источник; direct_feed_create "
        "создаёт URL-фид через preview/apply. Дождись Status=DONE и непустого фида. "
        "Если фида нет и на сайте есть товарная разметка, подготовь native источник "
        "по сайту в интерфейсе через Playwright и передай полученный FeedId. "
        "Адрес HTML-сайта не передавай как URL-фид. Ошибка явно заданного фида не "
        "разрешает молча менять источник. ShoppingAd/ListingAd создаются в product. "
        "В бизнес-профилях нет общего blacklist площадок: задавай исключения явно с причиной. "
        "В новом плане явно задавай client_budget: amount, period (weekly/monthly), currency, "
        "includes_vat; для суммы с НДС нужна явная vat_percent. Лимит относится только "
        "к кампаниям плана; недельные бюджеты кампаний задаются без НДС. Месячная сумма "
        "пересчитывается ×12/52 и не является жёстким потолком календарного месяца. "
        "По умолчанию tracking_profile=utm_v1 сохраняет BI-параметры и добавляет UTM. "
        "ALTERNATIVE_TEXTS_ENABLED явно NO; включай только при согласованной свободе текстов. "
        "Для каждого BusinessId нужны business_profiles с phone, address, has_office; "
        "телефон, адрес и офис сверяются через API до записи и после неё. "
        "Возраст задавай age_min/age_max по границам API; 25–54 исключает 0–17, 18–24 и 55+. "
        "Интересы аудитории необязательны и выбираются только по задаче конкретного клиента. "
        "Не добавляй отраслевые аудитории по умолчанию. Для РСЯ доступны "
        "audience_interest_ids: краткосрочные интересы из живого каталога, "
        "отдельные группы без параллельных ключей и автотаргетинга. "
        "Для maximum_clicks счётчик и бизнес-цели необязательны; без выбранных целей "
        "не требуй goals_reviewed или каталог Метрики. Для maximum_conversion_rate "
        "нужны счётчик и явный goal_id из priority_goals либо goal_id=13 для ключевых целей. "
        "Для 13 нужна хотя бы одна реальная ключевая цель, отличная от 12; "
        "в сам priority_goals значение 13 не добавляй. "
        "Все недостающие вопросы задай в начале задачи. Затем самостоятельно "
        "настрой кампании и опубликуй клиентский отчёт по известному адресу. "
        "Перед записью покажи подготовленный preview; поддержка elicitation клиентом обязательна. "
        "Сначала direct_campaign_plan и live preflight через direct_campaign_apply "
        "без confirmation; самостоятельно проверь полный артефакт и исправь BLOCK. "
        "Затем передай неизменный bundle и confirmation из preview. Токен проверяет "
        "целостность, а согласие на точный хеш запрашивается отдельно через MCP elicitation. "
        "Ответ running содержит job_id: читай direct_write_job до завершения. "
        "Запуск или возобновление показов требуют отдельной явной команды; при "
        "частичной ошибке перечитай созданные ID, не повторяй apply вслепую. "
        "Нужны semantic_plan и semantic групп; для product — ShoppingAd/ListingAd, "
        "для остальных каналов 1–3 ResponsiveAd/группу; для нескольких "
        "объявлений разные hypothesis. Минус-фразы выбирай через negative_keyword_policy "
        "с учётом бизнеса. Общий контент передавай через campaign_template/variants. "
        "API-суммы в микроединицах (× 1000000); Поиск требует явного автотаргетинга. "
        "Проверяй лимиты текстов и дополнений в direct://kb/tech-limits. "
        "Общий набор быстрых ссылок: минимум 4, желательно 8 с описаниями; "
        "Для каждого комбинаторного объявления РСЯ подготовь или сгенерируй минимум "
        "3 разных изображения (максимум 5), загрузи через direct_ad_assets_create (images) "
        "и передай все хэши в ad_image_hashes; одна картинка не проходит план. "
        "Для объявления с href и изображением или видео выбери релевантный предложению текст "
        "кнопки из доступных в интерфейсе; передай action_button с text и reason. "
        "URL кнопки явно заполняй основным href либо проверенной страницей контактов "
        "(destination=contacts и href). После API-создания сохрани кнопку через интерфейс, "
        "повторно открой и проверь текст и URL: API полей кнопки не предоставляет. "
        "В каждом объявлении РСЯ всегда добавляй карусель через интерфейс и "
        "повторно открой для проверки: AdImageHashes его не заменяют; "
        "required_manual_actions остаются обязательными. "
        "После создания любым способом сформируй клиентский HTML по фактическим ID. "
        "После настройки нужен единый /<client_login>/: "
        "HTTP 200, совпадение SHA-256 и проверка обоих разделов в браузере. "
        "Автоматический /create/ — вспомогательная выгрузка настройки, "
        "его публикация не заменяет единый отчёт. "
        "Локальный preview не заменяет публикацию. "
        "При восстановлении отчёта не создавай кампании повторно."
    )
    if settings.default_weekly_budget is not None:
        parts.append(
            f"недельный бюджет по умолчанию — {settings.default_weekly_budget:g} "
            "в валюте кабинета (технический fallback). Для нового плана задавай доли явно "
            "в пределах client_budget; fallback не отменяет общий месячный лимит."
        )
    return " ".join(parts)


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncIterator[None]:
    global _client
    try:
        yield None
    finally:
        client, _client = _client, None
        if client is not None:
            await client.aclose()


def _make_mcp() -> FastMCP:
    # FastMCP 1.x calls basicConfig in its constructor. Keep the embedding
    # application's logger configuration, including an initially empty root.
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    env_file = FastMCPSettings.model_config.get("env_file")
    FastMCPSettings.model_config["env_file"] = None
    try:
        return FastMCP("yandex-direct", instructions=_instructions(SETTINGS), lifespan=_lifespan)
    finally:
        FastMCPSettings.model_config["env_file"] = env_file
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)


mcp = _make_mcp()


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


def _attach_units(payload: dict[str, Any], units_mark: Any | None) -> None:
    api = _client
    if api is None:
        return
    units_since = getattr(api, "units_since", None)
    if units_mark is not None and units_since is not None:
        usage = units_since(units_mark)
        payload["units_usage"] = usage
        if usage["requests"]:
            latest = usage["requests"][-1]
            payload["units"] = {key: latest[key] for key in ("spent", "rest", "daily")}
    elif getattr(api, "last_units", None):
        payload["units"] = api.last_units.as_dict()


def _result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    serialized = json.dumps(identifiers.wire(payload), ensure_ascii=False, default=str)
    return CallToolResult(
        content=[TextContent(type="text", text=serialized)],
        structuredContent=json.loads(serialized),
        isError=is_error,
    )


def _ok(payload: dict[str, Any], *, units_mark: Any | None = None) -> CallToolResult:
    if "job_id" not in payload:
        _attach_units(payload, units_mark)
    return _result(
        payload,
        is_error=bool(payload.get("error")) or payload.get("status") in {"failed", "partial"},
    )


def _fail(
    exc: Exception,
    hint: str | None = None,
    *,
    units_mark: Any | None = None,
) -> CallToolResult:
    if isinstance(exc, (ValueError, PermissionError, FileNotFoundError, DirectError)):
        log.warning("%s: %s", type(exc).__name__, exc)
    else:
        log.exception(
            "Неожиданная ошибка: %s",
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    out: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, approval.ConsentError):
        out.update(exc.as_dict())
    if isinstance(exc, DirectError):
        out["error_code"] = exc.code
        out["request_id"] = exc.request_id
    if hint:
        out["hint"] = hint
    _attach_units(out, units_mark)
    return _result(out, is_error=True)


# ── справочники ──────────────────────────────────────────────────────────


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
async def direct_policy(policy_name: str = "agency_default_v1") -> CallToolResult:
    """Машиночитаемая версия регламента и модель статусов аудита.

    Ничего не читает и не меняет в кабинете. Возвращает требования к каналам,
    бюджету, стратегии, TrackingParams и сведения о внешних списках.
    available_profiles перечисляет legacy, шесть профилей v1 и два товарных e-commerce v2.
    planning_defaults задаёт текущий стартовый состав кампаний и общий бюджет;
    это рекомендации агенту вне зафиксированных снимков policy.
    """
    try:
        return _ok({"policy": policy.get(policy_name),
                    "planning_defaults": planning.defaults(), "available_profiles": [
            {"name": name, "version": item["version"],
             "business": item.get("profile", {}).get("business"),
             "stage": item.get("profile", {}).get("stage"),
             "legacy": "profile" not in item}
            for name, item in policy.POLICIES.items()
        ]})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


async def direct_campaign_plan(
    client_login: str,
    campaign_bundle: dict[str, Any],
    save_artifact: bool = True,
) -> CallToolResult:
    """Компилирует UnifiedCampaign bundle без API. По умолчанию пишет JSON и возвращает сводку.

    Нужны semantic_plan и semantic групп. Обычно 1–3 ResponsiveAd с 3–7 titles и 3 texts.
    Для product в ecommerce v2: product_source и 1–2 объявления shopping/listing,
    не более одного каждого типа в группе; контракт direct://kb/product-campaigns.
    maximum_clicks допускает отсутствие counter_ids и priority_goals; без целей
    goals_reviewed и каталог Метрики не требуются. maximum_conversion_rate
    требует счётчик и проверенный goal_id из priority_goals либо селектор 13.
    policy_name выбирает профиль; profile_context содержит данные целей/истории.
    Без policy_name сохраняются прежние правила agency_default_v1 (1.12.0).
    Возраст: age_min/age_max по границам API. Интересы необязательны, выбор — в плане
    конкретного клиента; отраслевых значений по умолчанию нет. РСЯ: audience_interest_ids
    (1–10 ID краткосрочных интересов) и audience_priority, без ключей/автотаргетинга.
    Общий контент: campaign_template + campaign_variants. Полная схема:
    direct://kb/structure-semantics. save_artifact=false возвращает полный JSON инлайн.
    """
    try:
        SETTINGS.check_login(client_login)
        payload = bundle.compile_bundle(
            campaign_bundle,
            client_login,
            default_weekly_budget=SETTINGS.default_weekly_budget,
        )
        if save_artifact:
            payload["artifact_path"] = str(bundle.persist(payload, SETTINGS.out_dir))
            return _ok(artifacts.compact(payload))
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_list_clients(limit: int = 1000, offset: int = 0,
                              include_archived: bool = False) -> CallToolResult:
    """Страница клиентских логинов с валютой; продолжение через next_offset.

    Остаток средств этот метод API не возвращает.

    Отсюда берётся client_login для остальных тулов. Вызывается без
    заголовка Client-Login — это агентский метод.
    """
    try:
        if not 1 <= limit <= 10000 or offset < 0:
            raise ValueError("limit: 1–10000, offset: от 0")
        result = await _api().call(
            "agencyclients",
            "get",
            {
                "SelectionCriteria": {} if include_archived else {"Archived": "NO"},
                "FieldNames": ["Login", "ClientId", "ClientInfo", "Currency", "Type", "Archived"],
                "Page": {"Limit": limit, "Offset": offset},
            },
            client_login=None,
        )
        clients = [
            {
                "login": c.get("Login"),
                "client_id": c.get("ClientId"),
                "name": c.get("ClientInfo"),
                "currency": c.get("Currency"),
                "archived": c.get("Archived") == "YES",
            }
            for c in result.get("Clients", [])
        ]
        return _ok({"clients": clients, "count": len(clients),
                    "truncated": result.get("LimitedBy") is not None,
                    "next_offset": result.get("LimitedBy"), "offset": offset})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_campaigns(client_login: str, include_archived: bool = False) -> CallToolResult:
    """Кампании клиента: id, имя, тип, статус. Нужен для маппинга CampaignId → имя
    и чтобы понять, какие кампании вообще стоит тянуть в отчёт.
    """
    try:
        SETTINGS.check_login(client_login)
        states = ["ON", "OFF", "SUSPENDED", "ENDED", "CONVERTED"]
        if include_archived:
            states.append("ARCHIVED")
        result = await _api().call_v501(
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
        return _ok(
            {
                "client_login": client_login,
                "api_version": "v501",
                "campaigns": campaigns,
                "count": len(campaigns),
            }
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_campaign_settings(
    client_login: str,
    campaign_ids: list[int | str] | None = None,
    include_archived: bool = False,
    limit: int = 100,
) -> CallToolResult:
    """Полные настройки TextCampaign и UnifiedCampaign через API v501.

    Возвращает раздельные стратегии Search/Network, места показов, недельные и
    дневные бюджеты, настройки геотаргетинга, счётчики и приоритетные цели,
    TrackingParams, минус-фразы и исключённые площадки. Метод только читает.
    """
    try:
        SETTINGS.check_login(client_login)
        payload = await campaigns.read_settings(
            _api(),
            client_login,
            campaign_ids=campaign_ids,
            include_archived=include_archived,
            limit=limit,
        )
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "openWorldHint": True,
        "destructiveHint": True,
        "idempotentHint": False,
    }
)
async def direct_campaign_audit(
    client_login: str,
    campaign_ids: list[int | str] | None = None,
    include_archived: bool = False,
    include_landing_pages: bool = True,
    limit: int = 50,
    policy_name: str = "agency_default_v1",
    approved_budget_campaign_ids: list[int | str] | None = None,
    save_artifact: bool = True,
) -> CallToolResult:
    """Аудит настроек и посадочных по политике: PASS/WARNING/BLOCK/MANUAL.

    По умолчанию полный JSON на диск, сводка и ссылки на факты в ответ.
    include_landing_pages включает ограниченные HTTP-проверки публичных сайтов.
    save_artifact=false явно возвращает полный JSON инлайн.
    """
    try:
        SETTINGS.check_login(client_login)
        payload = await audit.read_and_audit(
            _api(),
            client_login,
            campaign_ids=campaign_ids,
            include_archived=include_archived,
            include_landing_pages=include_landing_pages,
            limit=limit,
            policy_name=policy_name,
            approved_budget_campaign_ids=approved_budget_campaign_ids,
        )
        if save_artifact:
            payload["artifact_path"] = str(audit.persist(payload, SETTINGS.out_dir))
            return _ok(artifacts.compact(payload))
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_adgroups(
    client_login: str,
    campaign_ids: list[int | str] | None = None,
    ad_group_ids: list[int | str] | None = None,
    limit: int = 1000,
) -> CallToolResult:
    """Группы объявлений через API v501: регионы, статусы и параметры URL.

    Без фильтров читает группы первых 50 неархивных кампаний и явно сообщает
    об усечении. CampaignIds автоматически делятся на разрешённые пачки по 10.
    """
    try:
        SETTINGS.check_login(client_login)
        return _ok(
            await adgroups.read(
                _api(),
                client_login,
                campaign_ids=campaign_ids,
                ad_group_ids=ad_group_ids,
                limit=limit,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_goal_catalog(client_login: str, campaign_id: int | str) -> CallToolResult:
    """ID и названия целей Метрики, доступных выбранной кампании.

    Сначала через v501 проверяет, что кампания принадлежит client_login, затем
    вызывает официальный GetStatGoals API v4. Цвет и новизна цели этим методом
    не возвращаются и остаются ручной проверкой.
    """
    try:
        SETTINGS.check_login(client_login)
        ownership = await campaigns.read_settings(
            _api(), client_login, campaign_ids=[campaign_id], limit=1
        )
        if not ownership["campaigns"]:
            raise ValueError(f"Кампания {campaign_id} не найдена у клиента {client_login!r}")
        payload = await goals.read(_api(), campaign_id)
        payload["client_login"] = client_login
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_regions(
    query: str | None = None,
    ids: list[int] | None = None,
    client_login: str | None = None,
    limit: int = 20,
) -> CallToolResult:
    """Регионы по query или ids (хотя бы одно), limit 1–200.

    Возвращает названия, path родителей и unknown_ids. Агентскому токену нужен
    client_login. Справочник кешируется. Коды не угадывайте: API их не проверяет.
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


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_account_settings(
    client_login: str,
    sections: list[str] | None = None,
    campaign_ids: list[int | str] | None = None,
) -> CallToolResult:
    """Читает bid_modifiers, retargeting_lists, negative_keyword_sets; пустые sections — все.

    Без campaign_ids берёт до 50 неархивных кампаний, сообщает truncated.
    Ошибка секции приходит внутри неё; остальные секции доступны.
    Интерпретация: direct://kb/targeting-adjustments.
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


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_ads(
    client_login: str,
    campaign_ids: list[int | str] | None = None,
    ad_group_ids: list[int | str] | None = None,
    ad_ids: list[int | str] | None = None,
    include_archived: bool = False,
    limit: int = 1000,
) -> CallToolResult:
    """Тексты/ссылки TextAd/ResponsiveAd, источник и обработка ShoppingAd/ListingAd.

    limit 1–10000.

    Фильтры campaign_ids/ad_group_ids/ad_ids; пусто — все объявления клиента.
    landing_pages и domains группируют URL, ads_without_href считает остальные форматы.
    Проверьте перед выводами о конверсии; подробности direct://kb/tool-reference.
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


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_keywords(
    client_login: str,
    campaign_ids: list[int | str] | None = None,
    ad_group_ids: list[int | str] | None = None,
    keyword_ids: list[int | str] | None = None,
    limit: int = 10000,
) -> CallToolResult:
    """Ключевые фразы и полные настройки автотаргетинга через API v501.

    Нужен для аудита семантики и проверки того, какие категории и варианты
    брендовости реально включил Директ. Требуется хотя бы один фильтр.
    """
    try:
        SETTINGS.check_login(client_login)
        return _ok(
            await keywords.read(
                _api(),
                client_login,
                campaign_ids=campaign_ids,
                ad_group_ids=ad_group_ids,
                keyword_ids=keyword_ids,
                limit=limit,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ── отчёты ───────────────────────────────────────────────────────────────


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "openWorldHint": True,
        "destructiveHint": True,
        "idempotentHint": False,
    }
)
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
    conversion_scope: str = "campaign",
    include_discount: bool = False,
) -> CallToolResult:
    """Статистика Reports API: TSV, метаданные, итоги и первые строки.

    include_discount устарел: принимается для совместимости, в API не отправляется.
    Даты YYYY-MM-DD, деньги с НДС по include_vat. Конверсии по умолчанию согласуются
    с целями/атрибуцией кампаний; разные настройки разделите CampaignId IN либо
    задайте goals и attribution_models вместе. all_goals не равно заявкам.
    Поля, фильтры, лимиты и примеры: direct://kb/tool-reference и report-recipes.
    """
    try:
        SETTINGS.check_login(client_login)
        try:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to)
        except ValueError as exc:
            raise ValueError("date_from и date_to должны иметь формат YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("date_from не может быть позже date_to")

        contract = reports.normalize(
            fields=fields,
            report_type=report_type,
            goals=goals,
            attribution_models=attribution_models,
            filters=filters,
            order_by=order_by,
            limit=limit,
        )

        api = _api()
        contract, report_metadata = await report_context.resolve(
            api,
            client_login,
            contract,
            conversion_scope=conversion_scope,
        )

        selection: dict[str, Any] = {"DateFrom": date_from, "DateTo": date_to}
        if contract["filters"]:
            selection["Filter"] = contract["filters"]

        spec: dict[str, Any] = {
            "SelectionCriteria": selection,
            "FieldNames": contract["fields"],
            "ReportType": contract["report_type"],
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES" if include_vat else "NO",
        }
        if contract["goals"]:
            spec["Goals"] = contract["goals"]
            if contract["attribution_models"]:
                spec["AttributionModels"] = contract["attribution_models"]

        if contract["order_by"]:
            spec["OrderBy"] = contract["order_by"]
        if contract["limit"] is not None:
            spec["Page"] = {"Limit": contract["limit"]}

        report_metadata.update(
            {
                "schema": "direct_report_metadata_v1",
                "client_login": client_login,
                "date_from": date_from,
                "date_to": date_to,
                "include_vat": include_vat,
                "include_discount": include_discount,
                "report_type": contract["report_type"],
                "fields": contract["fields"],
                "filters": contract["filters"],
                "order_by": contract["order_by"],
                "row_limit": contract["limit"],
                "requested_at": datetime.now(UTC).isoformat(),
            }
        )
        tsv = await api.report(spec, client_login=client_login)
        report_metadata["received_at"] = datetime.now(UTC).isoformat()

        # Логин попадает в имя файла, поэтому чистит его store.safe_stem.
        stem = f"{client_login}_{date_from}_{date_to}_{api.report_name(spec)}"
        payload = store.persist(
            tsv,
            out_dir=SETTINGS.out_dir,
            stem=stem,
            inline_rows=SETTINGS.inline_rows,
            metadata=report_metadata,
        )
        payload["client_login"] = client_login
        payload["include_vat"] = include_vat
        return _ok(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
async def direct_client_report(
    client_login: str,
    action: str = "update",
    model_path: str | None = None,
    configuration: dict[str, Any] | None = None,
    period_id: str | None = None,
    expected_revision: str | None = None,
    insight: dict[str, str] | None = None,
    as_of: str | None = None,
    force: bool = False,
) -> CallToolResult:
    """Единый HTML кодом; ИИ получает только короткую сводку для выводов.

    initialize: проверенная модель внутри YD_OUT_DIR и configuration по
    direct://kb/client-report. update: чтение Директа, локальная сборка и backup.
    update собирает также восемь детальных срезов. enrich добавляет их к текущему
    зарегистрированному периоду, сохраняя дневную статистику и настройку.
    brief: текущая сводка без API. write_insight: title/text, period_id и
    expected_revision=brief.data_revision. Не меняет рекламу и не публикует HTML.
    """
    try:
        SETTINGS.check_login(client_login)
        if action == "initialize":
            if not model_path or configuration is None:
                raise ValueError("initialize требует model_path и configuration")
            payload = await asyncio.to_thread(
                report_pipeline.initialize, SETTINGS, client_login, model_path, configuration,
            )
        elif action == "update":
            payload = await report_pipeline.refresh(
                SETTINGS, _api(), client_login, as_of=as_of, force=force,
            )
        elif action == "brief":
            payload = await asyncio.to_thread(report_pipeline.read_brief, SETTINGS, client_login)
        elif action == "enrich":
            payload = await report_pipeline.enrich(
                SETTINGS, _api(), client_login, period_id=period_id,
            )
        elif action == "write_insight":
            if not period_id or not expected_revision or insight is None:
                raise ValueError("write_insight требует period_id, expected_revision и insight")
            payload = await asyncio.to_thread(
                report_pipeline.write_insight, SETTINGS, client_login,
                period_id, expected_revision, insight,
            )
        else:
            raise ValueError("action: initialize, update, enrich, brief или write_insight")
        # Purely local actions must not inherit an unrelated last Units header.
        return _result(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
async def direct_read_report(path: str, offset: int = 0, limit: int = 100) -> CallToolResult:
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
        return _result(store.read_back(p, offset, limit))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False})
async def direct_read_artifact(
    path: str,
    pointer: str = "",
    offset: int = 0,
    limit: int = 8000,
) -> CallToolResult:
    """Читать JSON-артефакт из YD_OUT_DIR: JSON Pointer и страницы до 16000 символов.

    json_fragment продолжается через next_offset. Без API и расхода баллов.
    """
    try:
        raw = Path(path)
        target = (raw if raw.is_absolute() else SETTINGS.out_dir / raw).resolve()
        if not target.is_relative_to(SETTINGS.out_dir.resolve()):
            raise ValueError("Путь вне YD_OUT_DIR")
        if target.suffix.lower() != ".json" or not target.is_file():
            raise ValueError("Можно читать только существующие JSON-артефакты")
        return _result(artifacts.read(target, pointer, offset, limit))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ── Вордстат ─────────────────────────────────────────────────────────────


@mcp.tool(
    annotations={
        "readOnlyHint": False,
        "openWorldHint": True,
        "destructiveHint": True,
        "idempotentHint": False,
    }
)
async def direct_wordstat(
    phrases: list[str],
    geo_ids: list[int | str] | None = None,
    min_shows: int = 0,
    top: int = 10,
) -> CallToolResult:
    """Спрос за месяц, вложенные и похожие запросы; полный TSV на диск, сводка в ответ.

    phrases до 50, top 1–100, min_shows фильтрует подсказки. geo_ids проверяйте
    через direct_regions; пусто — все регионы. client_login не нужен, sandbox
    не поддерживается. Shows не является прогнозом показов кампании.
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
        return _result(payload)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ── настройка кампании с нуля (явно включаемый write-режим) ─────────────


async def _execute_campaign(api, plan, live_preflight, journal):
    journal.verification_plan(plan)
    result = await executor.apply(api, plan)
    journal.checkpoint(result)
    result["preflight"] = live_preflight
    report_readback: dict[str, Any] | None = None
    if any(item.get("id") is not None for item in result.get("campaigns", [])):
        try:
            readback = await executor.readback(api, plan, result)
            report_readback = dict(readback)
            audit_path = audit.persist(readback.get("policy_audit", readback), SETTINGS.out_dir)
            readback["audit_artifact_path"] = str(audit_path)
            # Full objects feed the HTML report but needlessly inflate the
            # MCP response. Verification details and counts remain inline.
            readback.pop("objects", None)
            result["readback"] = readback
            if result["status"] == "complete" and not readback["verified"]:
                result["status"] = "complete_unverified"
        except Exception as exc:  # noqa: BLE001
            result["readback"] = {"verified": False, "error": str(exc)}
            if result["status"] == "complete":
                result["status"] = "complete_unverified"

    workflow.update(result)
    report_snapshot = {}
    try:
        report_model = creation_report.build_model(plan, result, report_readback)
        report_path = creation_report.persist(report_model, SETTINGS.out_dir)
        report_snapshot = publication.snapshot(report_path, SETTINGS.out_dir,
                                               journal.payload["job_id"])
        report_path = Path(report_snapshot["artifact_path"])
        report_result: dict[str, Any] = {
            "status": "local_only",
            **report_snapshot,
            "public_url": creation_report.public_url(
                plan["client_login"], SETTINGS.create_report_public_base_url
            )
            if SETTINGS.create_report_public_base_url
            else None,
            "verified": False,
        }
        result["creation_report"] = report_result
        journal.checkpoint(result)
        created_any = any(item.get("id") is not None for item in result.get("campaigns", []))
        if SETTINGS.create_report_ssh_host and created_any:
            report_result = await asyncio.to_thread(
                creation_report.publish,
                report_path,
                client_login=plan["client_login"],
                ssh_host=SETTINGS.create_report_ssh_host,
                remote_root=SETTINGS.create_report_remote_root,
                public_base_url=SETTINGS.create_report_public_base_url,
            )
            report_result["artifact_path"] = str(report_path)
        elif not created_any:
            report_result["note"] = (
                "Публичная версия не обновлена: ни одна кампания не создана."
            )
        else:
            report_result["note"] = (
                "Публикация отключена: задайте SSH host, remote root и public base URL."
            )
        result["creation_report"] = report_result
    except Exception as exc:  # noqa: BLE001
        result["creation_report"] = {
            **report_snapshot,
            "status": "publish_failed",
            "verified": False,
            "error": str(exc),
        }
    workflow.update(result)
    result["artifact_path"] = str(executor.persist(result, SETTINGS.out_dir))
    return result


def _job_payload(job_id: str, client_login: str) -> dict:
    job = jobs.read(SETTINGS.out_dir, job_id, client_login)
    payload = {"job_id": job_id, "job_status": job["status"],
               "client_login": client_login, "plan_hash": job["plan_hash"],
               "journal_path": str(SETTINGS.out_dir / "jobs" / (job_id + ".json")),
               "uncertain": job["uncertain"]}
    if job["status"] == "running" and job.get("owner_process") != jobs.PROCESS_ID:
        payload.update(status="attention_required", job_status="owner_changed",
                       message="Операция принадлежит другому процессу или была прервана. "
                               "Проверьте журнал и процесс; повторная запись запрещена.")
        return payload
    if job["status"] == "running":
        payload.update(status="running", poll_tool="direct_write_job", poll_after_seconds=3)
        return payload
    payload.update(job.get("result") or {"status": job["status"]})
    recovered = publication.latest(SETTINGS.out_dir, job)
    if recovered is not None:
        payload["current_publication"] = recovered
    checked = verification.latest(SETTINGS.out_dir, job)
    if checked is not None:
        payload["current_verification"] = checked
    if job.get("error"):
        payload["error"] = job["error"]
    if job["status"] == "done" and payload.get("status") == "preview":
        grant = approval.REGISTRY.issue(client_login, job["plan_hash"])
        payload["confirmation_required"] = grant.phrase
        payload["confirmation_ttl_seconds"] = approval.REGISTRY.ttl_seconds
        payload["human_approval"] = "mcp_elicitation_on_apply"
    return payload


async def direct_write_job(client_login: str, job_id: str) -> CallToolResult:
    """Состояние фоновой операции; чтение не возобновляет и не повторяет запись.

    При running повторите после poll_after_seconds. Ответ preview содержит
    технический токен; запись дополнительно требует MCP elicitation.
    """
    try:
        SETTINGS.check_login(client_login)
        return _ok(_job_payload(job_id, client_login))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
async def direct_runtime() -> CallToolResult:
    """Версия, идентификатор процесса, хеш исходников и необходимость перезапуска; без секретов."""
    return _ok(runtime.describe(SETTINGS))


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})
async def direct_pending_actions(
    client_login: str, offset: int = 0, limit: int = 100,
) -> CallToolResult:
    """Незавершённые действия из локальных журналов клиента; без запросов в рекламный кабинет."""
    try:
        return _ok(operations.pending(SETTINGS, client_login, offset=offset, limit=limit))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


async def direct_publish_job(
    client_login: str, job_id: str, confirmation: str | None = None,
    ctx: Context | None = None,
) -> CallToolResult:
    """Preview/apply повторной публикации готового HTML исходного job; без записи в Директ.

    Preview связывает исходный журнал, HTML и адрес публикации. Apply требует
    неизменности и MCP elicitation. Результат — отдельный publish job с source_job_id.
    """
    try:
        if SETTINGS.mode != "campaign_setup":
            raise PermissionError("Публикация требует YD_MODE=campaign_setup")
        SETTINGS.check_login(client_login)
        if confirmation is None:
            plan = publication.prepare(SETTINGS, client_login, job_id)
            grant = approval.REGISTRY.issue(client_login, plan["plan_hash"], evidence=plan)
            return _ok({**plan, "status": "preview", "executed": False,
                        "confirmation_required": grant.phrase,
                        "confirmation_ttl_seconds": approval.REGISTRY.ttl_seconds})
        # Retrieve the exact preview attempt, then recompute its full hash.
        token = confirmation.rsplit(" ", 1)[-1]
        grant = approval.REGISTRY._grants.get(token)
        if grant is None or not grant.evidence or grant.evidence.get("source_job_id") != job_id:
            raise ValueError("Нужен исходный preview публикации этого job")
        plan = publication.prepare(SETTINGS, client_login, job_id,
                                   attempt=grant.evidence["attempt"])
        approval.REGISTRY.validate(confirmation, client_login, plan["plan_hash"])
        receipt = await approval.REGISTRY.authorize(ctx, plan, "публикацию HTML", confirmation)
        async def operation(journal):
            return await publication.apply(SETTINGS, plan, journal)
        publish_id = jobs.start(SETTINGS.out_dir, client_login, plan["plan_hash"],
                                "publish", operation, receipt=receipt)
        await jobs.wait_briefly(publish_id)
        return _ok(_job_payload(publish_id, client_login))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


async def direct_verify_job(
    client_login: str,
    job_id: str,
    campaign_bundle: dict[str, Any] | None = None,
    repair_bundle: dict[str, Any] | None = None,
) -> CallToolResult:
    """Повторить API readback исходного apply/repair job без записи в Директ.

    Результат сохраняется отдельно и доступен в direct_write_job.current_verification.
    Для старого job без сохранённого плана нужен неизменный исходный bundle.
    Проверка не подтверждает UI-кнопки/карусели и не разрешает запуск.
    """
    try:
        SETTINGS.check_login(client_login)
        job = jobs.read(SETTINGS.out_dir, job_id, client_login)
        if campaign_bundle is not None and repair_bundle is not None:
            raise ValueError("Передайте только один исходный bundle")
        plan = None
        if campaign_bundle is not None:
            if job["kind"] != "apply":
                raise ValueError("campaign_bundle допустим только для apply")
            plan = bundle.compile_bundle(campaign_bundle, client_login,
                                         default_weekly_budget=SETTINGS.default_weekly_budget)
        if repair_bundle is not None:
            if job["kind"] != "repair":
                raise ValueError("repair_bundle допустим только для repair")
            plan = repair.normalize(repair_bundle, client_login)
        return _ok(await verification.run(_api(), SETTINGS.out_dir, client_login, job_id,
                                          legacy_plan=plan))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


async def direct_campaign_apply(
    client_login: str,
    campaign_bundle: dict[str, Any],
    confirmation: str | None = None,
    ctx: Context | None = None,
) -> CallToolResult:
    """Preview/apply ЕПК с job_id, журналом и подтверждением через MCP elicitation.

    Без confirmation выполняет фоновый preflight. Читайте direct_write_job до
    preview или blocked. Передайте неизменный bundle и confirmation из preview.
    Токен проверяет целостность; согласие запрашивается отдельным elicitation.
    При неизвестном результате блокировка сохраняется до сверки журнала.
    """
    try:
        if SETTINGS.mode != "campaign_setup":
            raise PermissionError("Запись требует YD_MODE=campaign_setup")
        SETTINGS.check_login(client_login)
        plan = bundle.compile_bundle(campaign_bundle, client_login,
                                     default_weekly_budget=SETTINGS.default_weekly_budget)
        receipt = None
        if confirmation is not None:
            receipt = await approval.REGISTRY.authorize(
                ctx, plan, "создание кампаний", confirmation
            )

        async def operation(journal):
            api = jobs.JournalAPI(_api(), journal)
            try:
                preflight = await executor.preflight(
                    api, plan, wordstat_out_dir=None if SETTINGS.sandbox else SETTINGS.out_dir,
                    wordstat_deadline_seconds=SETTINGS.report_deadline,
                    reuse_expensive=confirmation is not None,
                )
            except Exception as exc:  # noqa: BLE001 - preview retains actionable BLOCK
                preflight = {"status": policy.BLOCK, "findings": [
                    {"rule": "preflight.validation", "status": policy.BLOCK, "message": str(exc)}]}
            if confirmation is None or preflight["status"] == policy.BLOCK:
                response = {**plan, "status": "blocked" if preflight["status"] == policy.BLOCK
                            else "preview", "executed": False, "preflight": preflight,
                            "ready": plan["ready"] and preflight["status"] != policy.BLOCK}
                response["artifact_path"] = str(bundle.persist(response, SETTINGS.out_dir))
                return artifacts.compact(response)
            return await _execute_campaign(api, plan, preflight, journal)

        job_id = jobs.start(SETTINGS.out_dir, client_login, plan["plan_hash"],
                            "preview" if confirmation is None else "apply", operation,
                            receipt=receipt)
        await jobs.wait_briefly(job_id)
        return _ok(_job_payload(job_id, client_login))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


async def direct_campaign_repair(
    client_login: str,
    repair_bundle: dict[str, Any],
    confirmation: str | None = None,
    ctx: Context | None = None,
) -> CallToolResult:
    """Preview/apply ограниченных исправлений существующих ЕПК.

    Поддержаны минус-фразы/приоритетные цели кампаний, частичные обновления
    ResponsiveAd или товарные ShoppingAd/ListingAd и автотаргетинг. Без confirmation возвращает
    одноразовую фразу. Никогда не запускает показы.
    """
    units_mark: Any | None = None
    try:
        if SETTINGS.mode != "campaign_setup":
            raise PermissionError(
                "Исправление кампаний выключено. Запустите сервер с YD_MODE=campaign_setup."
            )
        SETTINGS.check_login(client_login)
        api = _api()
        mark = getattr(api, "units_mark", None)
        if mark is not None:
            units_mark = mark()
        plan = repair.normalize(repair_bundle, client_login)
        if confirmation is None:
            checked = await repair.preflight(api, plan)
            grant = approval.REGISTRY.issue(
                client_login, plan["plan_hash"], evidence=checked,
            )
            plan["preflight"] = checked
            plan["status"] = "preview"
            plan["executed"] = False
            plan["confirmation_required"] = grant.phrase
            plan["confirmation_ttl_seconds"] = approval.REGISTRY.ttl_seconds
            plan["artifact_path"] = str(repair.persist(plan, SETTINGS.out_dir))
            return _ok(plan, units_mark=units_mark)
        grant = approval.REGISTRY.validate(confirmation, client_login, plan["plan_hash"])
        if grant.evidence is None:
            raise ValueError("Для ремонта требуется новый preview с live preflight")
        receipt = await approval.REGISTRY.authorize(ctx, plan, "исправление кампаний", confirmation)

        async def operation(journal):
            journal.verification_plan(plan)
            guarded = jobs.JournalAPI(api, journal)
            result = await repair.apply(guarded, plan, expected_preflight=grant.evidence)
            journal.checkpoint(result)
            try:
                result["readback"] = await repair.readback(
                    guarded, plan, before=result["preflight"]["before"],
                )
            except Exception as exc:  # noqa: BLE001 - writes already journaled
                result["readback"] = {"verified": False, "error": str(exc)}
            if result["status"] == "complete" and not result["readback"]["verified"]:
                result["status"] = "complete_unverified"
            result["artifact_path"] = str(repair.persist(result, SETTINGS.out_dir))
            return result
        job_id = jobs.start(SETTINGS.out_dir, client_login, plan["plan_hash"], "repair",
                            operation, receipt=receipt)
        await jobs.wait_briefly(job_id)
        return _ok(_job_payload(job_id, client_login))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, units_mark=units_mark)


@mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True})
async def direct_product_source(
    client_login: str, source: dict[str, Any] | None = None,
    feed_ids: list[str] | None = None,
) -> CallToolResult:
    """Читает фиды и проверяет источник товаров; ничего не создаёт.

    source: type=feed/website, url, sample_urls (1–10 URL), опционально feed_id.
    Для website с FeedId нужны site_feed_verified=true и review_reason из UI.
    Без feed_id возвращает дальнейшие действия; website требует создания
    источника по сайту в интерфейсе, URL-фид — direct_feed_create.
    Наличие разметки не гарантирует успешный обход сайта Яндексом.
    """
    try:
        SETTINGS.check_login(client_login)
        if source is not None and feed_ids is not None:
            raise ValueError("Укажите source или feed_ids")
        if source is None:
            return _ok(await feeds.read(_api(), client_login, feed_ids))
        normalized = products.source(source, require_id=False)
        if normalized.get("feed_id"):
            normalized = products.source(source)
            checked = await products.check_sources(_api(), {
                "client_login": client_login, "campaigns": [{"channel": "product",
                    "product_source": normalized, "groups": []}]})
            return _ok({"source": normalized, "ready": True, "checks": checked})
        if normalized["type"] == "website":
            pages = await landing.inspect_pages([{"url": u} for u in normalized["sample_urls"]])
            markup = len(pages) == len(normalized["sample_urls"]) and all(
                p.get("ok") and not p.get("html_truncated") and p.get("product_markup")
                for p in pages)
            return _ok({"source": normalized, "ready": False, "product_markup": markup,
                        "pages": pages, "next_action": "website_source_in_direct_ui" if markup
                        else "fix_or_verify_product_markup", "message":
                        "Создайте источник товаров по сайту в интерфейсе Директа, "
                        "сверьте его URL и FeedId, затем повторите проверку. "
                        "Если API не возвращает источник, API-применение блокируется."})
        return _ok({"source": normalized, "ready": False, "next_action": "direct_feed_create"})
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


async def direct_feed_create(
    client_login: str, feed: dict[str, Any], confirmation: str | None = None,
    ctx: Context | None = None,
) -> CallToolResult:
    """Создаёт один RETAIL URL-фид: feed={name, url}; preview/apply с readback.

    URL — именно фид из задания, не HTML-сайт. Сначала выдаёт preview и токен;
    apply требует того же хеша, elicitation и повторного preflight под блокировкой.
    Возвращает job_id. Затем читайте direct_product_source до DONE с товарами.
    Обновление существующего фида и автоматический повтор записи не выполняются.
    """
    try:
        if SETTINGS.mode != "campaign_setup":
            raise PermissionError("Создание фида требует YD_MODE=campaign_setup")
        SETTINGS.check_login(client_login)
        plan = feeds.normalize(feed, client_login)
        if confirmation is None:
            checked = await feeds.preflight(_api(), plan)
            grant = approval.REGISTRY.issue(client_login, plan["plan_hash"])
            return _ok({**plan, "status": "preview", "executed": False,
                        "preflight": checked, "confirmation_required": grant.phrase,
                        "confirmation_ttl_seconds": approval.REGISTRY.ttl_seconds})
        receipt = await approval.REGISTRY.authorize(ctx, plan, "создание фида", confirmation)

        async def operation(journal):
            return await feeds.apply(jobs.JournalAPI(_api(), journal), plan)
        job_id = jobs.start(SETTINGS.out_dir, client_login, plan["plan_hash"], "feeds",
                            operation, receipt=receipt)
        await jobs.wait_briefly(job_id)
        return _ok(_job_payload(job_id, client_login))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


async def direct_ad_assets_create(
    client_login: str,
    assets_bundle: dict[str, Any],
    confirmation: str | None = None,
    ctx: Context | None = None,
) -> CallToolResult:
    """Preview/apply быстрых ссылок, уточнений и изображений PNG/JPG/GIF.

    Первый вызов только валидирует и возвращает одноразовое подтверждение.
    Созданные ID затем передаются в campaign_bundle или direct_campaign_repair.
    Минимум 4 ссылки, рекомендуется 8 с описаниями. Один набор переиспользуется
    всеми релевантными объявлениями, а не создаётся заново для каждого.
    images: [{path: абсолютный путь, name: подпись, type: AUTO}]; вместо path
    допустим image_data (base64). Хэши AdImageHash передайте в ad_image_hashes.
    """
    units_mark: Any | None = None
    try:
        if SETTINGS.mode != "campaign_setup":
            raise PermissionError(
                "Создание дополнений выключено. Запустите сервер с YD_MODE=campaign_setup."
            )
        SETTINGS.check_login(client_login)
        api = _api()
        mark = getattr(api, "units_mark", None)
        if mark is not None:
            units_mark = mark()
        plan = assets.normalize(assets_bundle, client_login)
        if confirmation is None:
            grant = approval.REGISTRY.issue(client_login, plan["plan_hash"])
            response = assets.preview(plan)
            response.update(
                {
                    "status": "preview",
                    "executed": False,
                    "confirmation_required": grant.phrase,
                    "confirmation_ttl_seconds": approval.REGISTRY.ttl_seconds,
                }
            )
            return _ok(response, units_mark=units_mark)
        receipt = await approval.REGISTRY.authorize(ctx, plan, "создание дополнений", confirmation)

        async def operation(journal):
            return await assets.apply(jobs.JournalAPI(api, journal), plan)
        job_id = jobs.start(SETTINGS.out_dir, client_login, plan["plan_hash"], "assets",
                            operation, receipt=receipt)
        await jobs.wait_briefly(job_id)
        return _ok(_job_payload(job_id, client_login))
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, units_mark=units_mark)


if SETTINGS.mode == "campaign_setup":
    mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                          "idempotentHint": False, "openWorldHint": True})(direct_feed_create)
    mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True,
                          "idempotentHint": False, "openWorldHint": True})(direct_publish_job)
    mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                          "idempotentHint": False, "openWorldHint": True})(direct_verify_job)
    mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False})(direct_write_job)
    mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False,
                          "idempotentHint": False, "openWorldHint": False})(direct_campaign_plan)
    mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )(direct_campaign_apply)
    mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )(direct_campaign_repair)
    mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }
    )(direct_ad_assets_create)


def main() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
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
