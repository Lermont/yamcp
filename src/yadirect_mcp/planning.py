"""Current agency planning preferences, separate from immutable policy snapshots."""

INSTRUCTIONS = (
    "Стандартный стартовый тест для услуг/B2B и локального бизнеса — Поиск + РСЯ; "
    "для интернет-магазина — товарная кампания + Поиск. Это правило для new и established. "
    "Если иной бюджет не задан, ориентир — 30000 RUB с НДС на 14 дней суммарно на кампании плана. "
    "По правилу пользователя этого достаточно для стандартного теста: не сокращай его "
    "до одного канала только из-за суммы. Явное задание клиента имеет приоритет. "
    "Предпочитай полное покрытие релевантных направлений, семантики и креативных гипотез "
    "в пределах общего бюджета; исключения обосновывай. "
    "По умолчанию сумма включает НДС; недельный общий лимит = сумма / (1 + НДС/100) / 2. "
    "Не подставляй автоматически по 5000 в неделю. Карты локального профиля учитывай "
    "в том же общем лимите. Для новых планов выбирай бизнес-профили *_v4. "
    "В них дополнительные варианты текста включены через API, а одно нейрообъявление "
    "на группу обязательно подготовь через интерфейс и проверь после сохранения. "
    "Для круглосуточных показов используй schedule=always_on без почасовой сетки. "
    "Для магазина канал product: ShoppingAd/ListingAd, галерея + РСЯ с общим бюджетом. "
    "Источник — URL-фид из "
    "задания либо сайт с товарной разметкой. Источник по сайту сначала подготовь в UI; "
    "проверь FeedId через direct_product_source. Не подменяй товарную кампанию обычной РСЯ. "
    "Стартовая стратегия — максимум конверсий с оплатой за клики, без целевой CPA. "
    "Максимум кликов выбирай только при отсутствии Метрики или целей; отсутствие истории "
    "не является причиной. Проверь цели; при нескольких используй GoalId=13. Настройку и "
    "отчётность выполняй в D:/yadirect-reports через MCP Директа; D:/mcp_direct "
    "предназначен для разработки MCP. Поручение пользователя разрешает необходимые "
    "действия в его рамках без повторных вопросов; технические preview, confirmation и "
    "readback обязательны. Context7 для этого workflow не требуется. Это правило не меняет "
    "существующие планы "
    "и не разрешает запуск."
)


def defaults() -> dict:
    """Return fresh guidance; campaign_mix is not a bundle channel schema."""
    return {
        "version": "2026-09-25",
        "source": "user_preferences",
        "scope": "new_plans",
        "stages": ["new", "established"],
        "budget": {
            "amount": 30000,
            "period": "two_weeks",
            "currency": "RUB",
            "scope": "planned_campaigns",
            "includes_vat": True,
            "vat_percent": 22,
            "duration_days": 14,
            "vat_basis": "RU_2026_default_explicit_override",
            "allocation": "explicit_within_total",
        },
        "preferred_profiles": {business: {stage: f"{business}_{stage}_v4"
                                for stage in ("new", "established")}
                               for business in ("services_b2b", "local_business", "ecommerce")},
        "strategy": {"type": "maximum_conversion_rate", "payment": "clicks",
                     "history_required": False, "fallback": "missing_counter_or_goals"},
        "creative": {"alternative_texts_enabled": True, "neuro_ad": True,
                     "neuro_ad_method": "required_ui_per_group"},
        "schedule": "always_on",
        "campaign_mix": {
            "services_b2b": ["search", "network"],
            "local_business": ["search", "network"],
            "ecommerce": ["product", "search"],
        },
        "additional_campaigns": {"local_business": ["maps"]},
        "compiler_support": {"search": True, "network": True, "maps": True, "product": True},
        "product_sources": {"feed_url": "direct_feed_create",
                            "website": "native_ui_source_then_api_feed_readback"},
        "budget_alone_reduces_to_single_campaign": False,
        "coverage": "all_relevant_directions_within_total_budget",
        "explicit_client_request_overrides": True,
        "instructions": INSTRUCTIONS,
    }


def initial_budget(raw: dict | None) -> dict:
    """Fill v3 planning defaults; old profile inputs are never migrated implicitly."""
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("client_budget: требуется объект")
    result = {"amount": 30000, "period": "two_weeks", "currency": "RUB",
              "includes_vat": True, **(raw or {})}
    if "vat_percent" not in result:
        if result["includes_vat"] is False:
            result["vat_percent"] = 0
        elif result["currency"] == "RUB":
            result["vat_percent"] = 22
        else:
            raise ValueError("client_budget.vat_percent: укажите НДС для валюты вне RUB")
    return result
