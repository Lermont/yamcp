# Контракт записи в Direct API

## Проверки MCP поверх API — 18.09.2026

`client_budget` и `business_profiles` — метаданные MCP, в Campaigns.add они
не передаются. Общая сумма WeeklySpendLimit всех кампаний плана ограничена
нормализованным client_budget; валюта сверяется по Clients.get. При сумме с НДС
ставка задаётся явно; monthly × 12/52 даёт средний недельный эквивалент.
После записи читаются фактические бюджеты только новых кампаний.

`GoalId=13` в WbMaximumConversionRate означает использование PriorityGoals:
нужна хотя бы одна цель кроме 12. 13 не передаётся как элемент PriorityGoals.
Каталог проверяется по реальным ID, readback сравнивает GoalId стратегии.
MCP по умолчанию передаёт ALTERNATIVE_TEXTS_ENABLED=NO.

Businesses.get запрашивает Id, IsPublished, Phone, Address, HasOffice (пакет/страница
до 1000). Значения сравниваются с ожидаемыми business_profiles до и после записи;
неполные/дублирующиеся профили и расхождения блокируют проверку. Это проверка
профиля объявления, не привязки организации к кампании и не контактов на сайте.

Источники, проверены 18.09.2026:
[UnifiedCampaign](https://yandex.ru/dev/direct/doc/ru/campaigns/add-unified-campaign),
[Businesses.get](https://yandex.ru/dev/direct/doc/ru/businesses/get).

**Деньги.** Все денежные поля — целые числа в микроединицах: значение в валюте рекламодателя × 1 000 000. Это касается `WeeklySpendLimit`, `AverageCpa`, `AverageCpc`, `BidCeiling`, `PriorityGoals.Value`, ставок.

## Campaigns.add

Тип кампании задаётся выбором структуры и после создания не меняется. Для ЕПК — `UnifiedCampaign`:

Не более **10 кампаний за один вызов**, имя каждой — до 255 символов.
Кампанийные `NegativeKeywords` ограничены 7 словами в одной фразе, 35 символами
в слове и суммарной длиной 20 000 символов.

- **`BiddingStrategy`** (или `PackageBiddingStrategy`, взаимоисключающие). Внутри обязательны **обе** подструктуры — `Search` и `Network`.
  - Типы для `Search`: `WB_MAXIMUM_CLICKS`, `WB_MAXIMUM_CONVERSION_RATE`, `AVERAGE_CPC`, `AVERAGE_CPA`, `AVERAGE_CRR`, `HIGHEST_POSITION`, `PAY_FOR_CONVERSION`, `PAY_FOR_CONVERSION_CRR`, `SERVING_OFF`.
  - Для `Network`: те же плюс `NETWORK_DEFAULT`.
  - `PlacementTypes` (YES/NO): Search — `SearchResults`, `ProductGallery`, `DynamicPlaces`, `Maps`, `SearchOrganizationList`; Network — `Network`, `Maps`. Чтобы кампания была только поисковой, Network переводится в `SERVING_OFF`.
- **`WbMaximumClicks`**: `WeeklySpendLimit` обязателен; `BidCeiling` опционален — задавать не рекомендуется, режет эффективность стратегии.
- **`WbMaximumConversionRate`**: обязательны `WeeklySpendLimit` и `GoalId`;
  средняя CPA не задаётся, `BidCeiling` опционален.
- **`AverageCpa`**: обязательны `AverageCpa` и `GoalId`; опциональны `WeeklySpendLimit`, `BidCeiling`, `ExplorationBudget`.
- **`CustomPeriodBudget`** (`SpendLimit`, `StartDate`, `EndDate`, `AutoContinue`) — альтернатива `WeeklySpendLimit`, вместе задавать нельзя.
- **`CounterIds.Items`** — счётчики Метрики; `CounterIds` необязателен.
  Для `WB_MAXIMUM_CLICKS` допустимо не передавать ни `CounterIds`, ни
  `PriorityGoals`; `GoalId` в этой стратегии отсутствует.
- **`PriorityGoals`** — `GoalId` + `Value` (×1e6) + `IsMetrikaSourceOfValue`. Обязательны для многоцелевых стратегий и `MaxProfit` (минимум 2 цели).
- **`AttributionModel`** — `AUTO` (по умолчанию), `FCCD`, `LC`, `LSCCD`.
- **`Settings`** (YES/NO): `ADD_METRICA_TAG`, `ENABLE_SITE_MONITORING`,
  `ADD_TO_FAVORITES`, `CAMPAIGN_EXACT_PHRASE_MATCHING_ENABLED`,
  `ENABLE_COMPANY_INFO`, `REQUIRE_SERVICING`, `ALTERNATIVE_TEXTS_ENABLED`.
- **`NegativeKeywordSharedSetIds`** — не более 3 наборов.
- `PackageBiddingStrategy` несовместима с `BiddingStrategy`, `PriorityGoals`, `CounterIds`, `AttributionModel`.

## AdGroups.add

Обязательны `Name` (1–255 символов), `CampaignId`, `RegionIds` (минимум один; `0` = все регионы). **Не более 1000 групп за вызов.** В архивные кампании группы не добавляются. `NegativeKeywords` — до 7 слов и до 35 символов на слово, суммарно до 4096 символов. `TrackingParams` — до 1024 символов.

## Ads.add

**Не более 1000 объявлений за вызов.** С 30 июня новые текстовые объявления в
ЕПК создаются как `ResponsiveAd`; `TextAd` в ЕПК доступен только для
редактирования. Обязательны `Titles` (1–7, каждый ≤56) и `Texts` (1–3, каждый
≤81), а также хотя бы один из `Href` / `BusinessId`. Опционально:
`DisplayUrlPath` (≤20), `AdImageHashes` (1–5), `SitelinkSetId`,
`AdExtensionIds` (≤50), `VideoExtensionIds` (1–6), `PriceExtension`,
`ErirAdDescription`. `DisplayUrlPath` допускает буквы, цифры и `- № / % #`, но
не пробел, `_`, `--` или `//`; он и `SitelinkSetId` требуют `Href`.

MCP применяет более строгий внутренний контракт создания: 3–7 уникальных
`Titles`, из которых минимум 3 содержательно используют 45–56 знаков, и ровно 3
уникальных `Texts`. Неполный, слишком короткий или дублирующийся набор, а также
`TextAd`, блокируется локально до preview и не расходует вызов API.

Для уточнений схемы создания и обновления различаются: `Ads.add` принимает
`AdExtensionIds`, а `Ads.update` — `CalloutSetting.AdExtensions` с операциями
`SET`, `ADD` или `REMOVE`. `direct_campaign_repair` использует `SET`, когда
полностью заменяет подтверждённый набор уточнений.

**Устарели и не сохраняются:** `TurboPageId`, `VCardId`, `PreferVCardOverBusiness`. Для товарных форматов ЕПК — `ShoppingAd` / `ListingAd` с обязательными `FeedId` и `DefaultTexts`, `FeedFilterConditions` до 30 фильтров.

## Автотаргетинг = псевдо-ключевая фраза

Управляется **через сервис Keywords**, а не отдельным объектом:
- `Keywords.add` со значением `Keyword: "---autotargeting"` и явным
  `AutotargetingSettings`; если настройки не передать, API включает все
  категории запросов по умолчанию;
- в `Keywords.get` возвращается `CriteriaType: AUTOTARGETING`;
- ставка — через `KeywordBids.set`; остановка/запуск — `Keywords.suspend`/`resume`;
- **один автотаргетинг на группу**; для групп с показами на Поиске он обязателен.
  Безопасный профиль MCP включает Exact и Narrow и отключает Alternative,
  Accessory, Broader и бренды конкурентов.

## Лимиты и баллы

- Не более **5 одновременных запросов** от одного рекламодателя.
- Заголовок `Units` в каждом ответе: `потрачено/остаток/суточный лимит`. Суточный лимит зависит от активности аккаунта, пополняется по 1/24 в час скользящим окном.
- Typed preview возвращает `api_units_estimate`: запросная часть плюс стоимость каждого планируемого объекта для успешных `Campaigns.add`, `BidModifiers.add`, `AdGroups.add`, `Ads.add` и `Keywords.add`. Preflight/readback, ошибки и отдельные дополнения в прогноз не входят.
- Write-инструменты ставят отметку перед операцией и возвращают `units_usage` с фактической суммой, группировкой по сервису/методу и каждой записью `Units`, `RequestId`, `Units-Used-Login` этой операции.
- Live preflight сравнивает прогноз write-фазы с текущим остатком `Units` и не начинает частичное создание при заведомой нехватке. Коммандер предлагается только как ручной резервный путь с обязательным последующим API-аудитом.
- **20 баллов штрафа** за ошибку вызова метода (кроме серверных ошибок) — валидировать план до отправки дешевле, чем ловить ошибки API.
- Не более 5 офлайн-отчётов в очереди на пользователя; готовый отчёт живёт 5 часов, повторный идентичный запрос в этом окне отдаётся сразу и бесплатно.
- Для синхронизации локальных данных — сервис `Changes` (`checkDictionaries` → `checkCampaigns` → `check`), а не повторные `get`.

См. также: «Типы кампаний и ЕПК» (`direct://kb/campaign-types`), «Технические ограничения Директа» (`direct://kb/tech-limits`), «Возможности сервера» (`direct://kb/server-capabilities`)

Источники: [Campaigns.add](https://yandex.ru/dev/direct/doc/ru/campaigns/add) · [add: UnifiedCampaign](https://yandex.ru/dev/direct/doc/ru/campaigns/add-unified-campaign) · [AdGroups.add](https://yandex.ru/dev/direct/doc/ru/adgroups/add) · [Ads.add](https://yandex.ru/dev/direct/doc/ru/ads/add) · [Автотаргетинг в API](https://yandex.ru/dev/direct/doc/ru/best-practice/auto-targeting.html) · [Эффективная работа с API](https://yandex.ru/dev/direct/doc/ru/optimize)

## Контракт MCP после аудита 11.09.2026 (политика 1.11.0)

- Все ID объектов в JSON/structuredContent и сохранённых JSON — десятичные строки.
  Вход допускает целые числа и десятичные строки; значения float не использовать.
  В запросах Direct сервер передаёт целые int64. Для 19-значных ID нельзя применять
  JavaScript `Number`; сохраняйте исходную строку.
- `Keywords.get`: CampaignIds по 10, AdGroupIds по 1000, Ids по 10000; глобальный
  `limit` сохраняется, неполная выдача отмечается `truncated`.
- `Campaigns.update` делится по 10; успешные результаты и ошибки каждого объекта
  сохраняются даже при частичном результате или сбое следующего пакета.
- `AdImages.add` получает до 100 изображений за запрос, затем хэши перечитываются.
- Только get автоматически повторяется при транспортных ошибках, HTTP 429/5xx
  и временных кодах 52, 506, 1000–1002, 1020: максимум 4 попытки с паузами 1/2/4 с.
  Add/update не повторяются. `YD_USE_OPERATOR_UNITS=true` включает баллы агентства.
- Устаревший `IncludeDiscount` больше не отправляется. `include_discount` на входе
  оставлен для совместимости и отражается только в метаданных.
- Preview и write возвращают `job_id`; читайте `direct_write_job(client_login, job_id)`.
  Согласие для неизменного хеша перед записью запрашивается через MCP elicitation.
  Отмена, отказ, неподдерживаемая форма и отсутствие `approve=true` возвращают
  разные коды `mcp_elicitation_*` с диагностикой клиента, без записи в Direct.
  При отказе/отмене токен сохраняется до прежнего срока; повторный вызов требует
  нового ответа клиента. На время формы токен резервируется от параллельных вызовов,
  а после согласия повторно проверяются срок/логин/хеш и токен расходуется один раз.
  MCP-клиент без elicitation не может выполнять запись. Журнал и lock находятся
  в `YD_OUT_DIR/jobs`; неизвестный исход требует сверки, не повторного apply.
- Успешные проверки URL/Wordstat кешируются 15 минут на логин и хеш. Смена хеша,
  перезапуск или истечение TTL требуют повторной проверки. Preflight всегда повторяет
  доступность объектов и валютные ограничения. Неуспешный preview возвращает BLOCK
  с находками и путём полного артефакта.

### Новые поля типизированного bundle

На уровне bundle: `blocked_ips: ["203.0.113.10"]` — до 25 IPv4.
На уровне группы: `negative_keyword_shared_set_ids: ["123"]` — 1–3 уникальных ID;
существование проверяется до записи, состав набора остаётся отдельным объектом кабинета.

На уровне объявления (это фрагмент существующего объявления):

```json
{
  "price_extension": {"price": "1500.00", "old_price": "1800.00", "qualifier": "FROM", "currency": "RUB"},
  "erir_ad_description": "Описание объекта рекламирования"
}
```

`price` — сумма в единицах выбранной валюты, сервер переводит её в микроединицы.
`old_price` необязательна и должна превышать текущую цену. Цена и описание
сверяются после создания. Для существующего объявления эти поля пока меняются
через отдельную доработку repair или интерфейс: текущая поддержка — новые планы.

На уровне отдельной группы РСЯ:

```json
{
  "retargeting_rules": [{"operator": "ANY", "goals": [{"goal_id": "123", "days": 30}]}],
  "audience_priority": "NORMAL"
}
```

От 1 до 10 правил ALL/ANY/NONE, 1–10 целей на правило, период 1–540 дней;
хотя бы одно правило включающее. Группа не содержит параллельных ключей, интересов
или автотаргетинга. `goal_catalog_campaign_id` задаётся явно, принадлежность кампании
клиенту и существование целей проверяются. При повторном использовании условия
Директ может вернуть существующий ID с другим именем — сравниваются тип и правила.

### Дополнительные источники

`YD_WORDSTAT_TOKEN` включает официальный `api.wordstat.yandex.net/v1/topRequests`
(последние 30 дней). Без него сохраняется Direct v4 с явным указанием источника
в метаданных. `YD_METRIKA_TOKEN` включает проверку существования и прав чтения
счётчика через API Метрики. Без токена такая проверка остаётся MANUAL; статический
HTML не позволяет достоверно исключить установку счётчика через GTM/JavaScript.
BusinessId проверяется как доступный опубликованный профиль объявления; это
не доказательство привязки организации на уровне кампании.
