"""Current geo contract, independent of frozen campaign policy snapshots."""

RETIRED_OPTION = "ENABLE_AREA_OF_INTEREST_TARGETING"
SOURCE_URL = "https://yandex.ru/dev/direct/doc/ru/annex/campaign-options"
INSTRUCTIONS = (
    "Геотаргетинг: проверяй регионы и исключения на уровне групп. "
    "Отдельный переключатель расширенного геотаргетинга отменён Яндексом 31.08.2026; "
    "ENABLE_AREA_OF_INTEREST_TARGETING больше не поддерживается. "
    "Его YES/NO или отсутствие в старом ответе API не доказывает состояние географии. "
    "Не выдавай отмену переключателя за новую находку аудита, недостаток кампании "
    "или выполненное исправление и не рекомендуй его отключать. "
    "В клиентском отчёте обсуждай географию только по конкретным регионам групп "
    "и данным о трафике; не обещай строгое физическое местонахождение пользователей."
)
WRITE_ERROR = (
    f"{RETIRED_OPTION} больше не поддерживается Яндексом: "
    "удалите его из bundle.settings и повторите direct_campaign_plan. "
    "Проверяйте region_ids групп; отключение старого переключателя не применяется."
)


def guidance() -> dict:
    return {
        "version": "2026-09-29",
        "region_level": "ad_group",
        "retired_option": RETIRED_OPTION,
        "retired_option_is_finding": False,
        "source_url": SOURCE_URL,
        "instructions": INSTRUCTIONS,
    }
