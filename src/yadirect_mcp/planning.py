"""Current agency planning preferences, separate from immutable policy snapshots."""

INSTRUCTIONS = (
    "Стандартный стартовый тест для услуг/B2B и локального бизнеса — Поиск + РСЯ; "
    "для интернет-магазина — товарная кампания + Поиск. Это правило для new и established. "
    "Если иной бюджет не задан, ориентир — 30000 RUB в месяц суммарно на кампании плана. "
    "По правилу пользователя этого достаточно для стандартного теста: не сокращай его "
    "до одного канала только из-за суммы. Явное задание клиента имеет приоритет. "
    "Предпочитай полное покрытие релевантных направлений, семантики и креативных гипотез "
    "в пределах общего бюджета; исключения обосновывай. "
    "Недельные доли распределяй явно после уточнения, включает ли сумма НДС; "
    "не подставляй автоматически по 5000 в неделю. Карты локального профиля учитывай "
    "в том же общем лимите. Для магазина используй ecommerce_*_v2 и канал product: "
    "ShoppingAd/ListingAd, галерея + РСЯ с общим бюджетом. Источник — URL-фид из "
    "задания либо сайт с товарной разметкой. Источник по сайту сначала подготовь в UI; "
    "проверь FeedId через direct_product_source. Не подменяй товарную кампанию обычной РСЯ. "
    "Это правило не меняет существующие планы "
    "и не разрешает запуск."
)


def defaults() -> dict:
    """Return fresh guidance; campaign_mix is not a bundle channel schema."""
    return {
        "version": "2026-09-18",
        "source": "user_preferences",
        "scope": "new_plans",
        "stages": ["new", "established"],
        "budget": {
            "amount": 30000,
            "period": "monthly",
            "currency": "RUB",
            "scope": "planned_campaigns",
            "vat_basis": "explicit_client_choice",
            "allocation": "explicit_within_total",
        },
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
