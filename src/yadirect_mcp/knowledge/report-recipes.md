# Рецепты отчётов для донастройки

## Клиентская публикация: дополнять единый отчёт

Полученную статистику добавлять в существующий отчёт Media Targeting по адресу
`https://bi-data.ru/elama/<client_login>/`. Сохранить «Настройку» и предыдущие
периоды; перед обновлением прочитать текущий HTML и модель, сделать backup.
Новый самостоятельный отчёт со статистикой не заменяет единый документ.
Подробный порядок и контакты: `direct://kb/client-report`.

## Подготовка статистики

Рецепты под тул `mcp__yandex-direct__direct_report`. Шаги оптимизации, к которым они относятся, — в «Плейбук донастройки» (`direct://kb/optimization-playbook`).

## Рецепты

Для конверсионных полей MCP 1.5.0 сначала читает текущие настройки кампаний.
Если `goals` не переданы, берёт цель стратегии, а для служебного GoalId=13 —
состав `PriorityGoals`; без отдельной цели стратегии использует `PriorityGoals`.
Не переданная модель берётся из `AttributionModel` кампании. В запрос Reports
API уходят явные `Goals` и `AttributionModels`, поэтому AUTO не заменяется на LC.

Предпочтительно задавать `CampaignId IN` с нужными ID. Без этого проверяются все
возможные кампании, включая архивные для исторических периодов; другие фильтры
отчёта могут сузить эту выборку. Если цели/модели различаются или выдача настроек
усечена, автоматический отчёт не выполняется. Разделите кампании на отчёты либо
задайте явные `goals` **и** `attribution_models` для общего сравнения. Несовпадение
с настройками отмечается в метаданных. Для пакетной стратегии цель автоматически
не угадывается по кампании: нужны явные параметры сравнения. Неизвестные цели,
пустая выборка и непрочитанные ID не заменяются молча агрегатом всех целей.

`conversion_scope=all_goals` — явный запрос агрегата всех целей с LC по умолчанию
Direct API; несовместим с `goals`/`attribution_models`. Метаданные предупреждают,
что это не число заявок. Даже отдельные цели означают достижения целей, а не
подтверждённые уникальные лиды; вовлечённые сессии (цель 12) отмечаются отдельно.

Результат включает `metadata` и путь `metadata_path` к полному JSON рядом с TSV:
цели, модель и источник выбора, текущие настройки кампаний, даты отчёта,
`include_vat` (по умолчанию true), время запроса и получения, фильтры,
предупреждения и SHA-256 TSV. `direct_read_report` возвращает эти метаданные
при постраничном чтении, проверив хеш. Большой список кампаний сокращается
только в ответе MCP; полный JSON сохранён. Для старого TSV без метаданных
выдаётся предупреждение. Текущий снимок настроек не доказывает их историю;
временной лаг и модель нужно учитывать при сверке с интерфейсом и CRM.

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
- С `goals` Direct возвращает отдельные колонки `<метрика>_<goal_id>_<модель>`.
  `attribution_models` валидны только вместе с `goals`; актуальны `FCCD`, `LC`,
  `LSCCD`, `AUTO`. MCP автоматически согласует недостающие параметры с кампанией,
  как описано выше; дефолт самого API LC используется только в явном all_goals.
- Статистика доступна за 3 последних года.

## Экономия баллов

Готовый офлайн-отчёт живёт **5 часов**: повтор идентичного запроса в этом окне возвращается сразу и бесплатно (клиент считает `ReportName` как хеш спецификации, поэтому идентичность соблюдается автоматически). Уже выгруженный файл читать через `direct_read_report` постранично — без повторного обращения к API.

См. также: «Контракт записи в Direct API» (`direct://kb/api-contract`), «Возможности сервера» (`direct://kb/server-capabilities`)

Источники: [Спецификация отчёта](https://yandex.ru/dev/direct/doc/ru/spec) · [Настройки ЕПК и служебные цели](https://yandex.ru/dev/direct/doc/ru/campaigns/get-unified-campaign) · [Допустимые поля](https://yandex.ru/dev/direct/doc/ru/fields-list) · [Несовместимые поля и зависимости](https://yandex.ru/dev/direct/doc/ru/compatibility) · [Тип отчёта](https://yandex.ru/dev/direct/doc/reports/type.html)
