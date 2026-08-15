# Рецепты отчётов для донастройки

Рецепты под тул `mcp__yandex-direct__direct_report`. Шаги оптимизации, к которым они относятся, — в «Плейбук донастройки» (`direct://kb/optimization-playbook`).

## Рецепты

**Минус-слова (поисковые запросы).** `report_type: SEARCH_QUERY_PERFORMANCE_REPORT`, `fields: ["CampaignName","AdGroupName","Query","MatchType","Impressions","Clicks","Cost","Conversions"]`, `goals: [<id цели>]`. Сортировка по `Impressions` убыв. Ищем: много показов при нуле кликов, клики без конверсий, явно нецелевые запросы. Поля `Sessions`/`BounceRate` в этом типе отчёта недоступны.

**Минус-площадки РСЯ.** `report_type: CUSTOM_REPORT`, `fields: ["Placement","AdNetworkType","Impressions","Clicks","Cost","Conversions","CostPerConversion"]`, `filters: [{"Field":"AdNetworkType","Operator":"EQUALS","Values":["AD_NETWORK"]}]`. Указание `Placement` **автоматически добавляет группировку по `AdNetworkType`**.

**Ключевые фразы и условия показа.** `report_type: CRITERIA_PERFORMANCE_REPORT`, `fields: ["CampaignName","AdGroupName","Criterion","CriterionType","Impressions","Clicks","Ctr","Cost","AvgCpc","Conversions","CostPerConversion"]`. По `CriterionType` отделяется автотаргетинг (`AUTOTARGETING`) от ключевых фраз. Детальная разбивка по категориям запросов автотаргетинга (целевые/узкие/широкие/альтернативные/сопутствующие) — в Мастере отчётов интерфейса.

**А/Б объявлений.** `report_type: AD_PERFORMANCE_REPORT`, `fields: ["CampaignName","AdGroupName","AdId","Impressions","Clicks","Ctr","Cost","Conversions","CostPerConversion"]`. Оценивать только группы с 10+ кликами.

**Основания для корректировок.** `report_type: CUSTOM_REPORT`, `fields: ["CampaignName","Device","Gender","Age","Impressions","Clicks","Cost","Conversions","CostPerConversion"]`. Отчёт даёт только статистику по срезу, но не заданные коэффициенты: перед выводами прочитать уже выставленные корректировки через `direct_account_settings`, иначе «на мобильных нет конверсий» окажется корректировкой −100%, а не поведением аудитории.

**Динамика и поиск обрывов.** `report_type: CUSTOM_REPORT`, `fields: ["Date","CampaignName","Impressions","Clicks","Cost","Conversions"]`.

## Правила совместимости полей

- `Date`, `Week`, `Month`, `Quarter`, `Year` — **взаимоисключающие**, только одно поле на отчёт.
- `Criterion`, `CriterionId`, `CriterionType` **несовместимы** с `Criteria`, `CriteriaId`, `CriteriaType` — выбирать одну номенклатуру.
- `CriterionType`, `CriteriaType`, `AudienceTargetId`, `DynamicTextAdTargetId`, `Keyword`, `SmartAdTargetId` — взаимоисключающие.
- `ClickType` несовместим с `Impressions`, `Ctr`, `AvgImpressionPosition`, `WeightedImpressions`, `WeightedCtr`, `AvgTrafficVolume`.
- `Query` — атрибут, доступен только в `SEARCH_QUERY_PERFORMANCE_REPORT`.
- Классы полей различаются: **сегмент** даёт группировку, **метрика** — число, **атрибут** — фиксированное значение, **фильтр** используется только в `filters` и в отчёт не выводится (например `Keyword`).
- Группировка не добавляет поле в вывод автоматически: в отчёт попадает только то, что перечислено в `fields`.
- `Conversions` без `goals` пустые. `attribution_models` валидны только вместе с `goals`; по умолчанию `LSC`. Несколько моделей — данные дублируются по каждой.
- Статистика доступна за 3 последних года.

## Экономия баллов

Готовый офлайн-отчёт живёт **5 часов**: повтор идентичного запроса в этом окне возвращается сразу и бесплатно (клиент считает `ReportName` как хеш спецификации, поэтому идентичность соблюдается автоматически). Уже выгруженный файл читать через `direct_read_report` постранично — без повторного обращения к API.

См. также: «Контракт записи в Direct API» (`direct://kb/api-contract`), «Возможности сервера» (`direct://kb/server-capabilities`)

Источники: [Допустимые поля](https://yandex.ru/dev/direct/doc/ru/fields-list) · [Несовместимые поля и зависимости](https://yandex.ru/dev/direct/doc/ru/compatibility) · [Тип отчёта](https://yandex.ru/dev/direct/doc/reports/type.html)
