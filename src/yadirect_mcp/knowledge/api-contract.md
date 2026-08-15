# Контракт записи в Direct API

**Деньги.** Все денежные поля — целые числа в микроединицах: значение в валюте рекламодателя × 1 000 000. Это касается `WeeklySpendLimit`, `AverageCpa`, `AverageCpc`, `BidCeiling`, `PriorityGoals.Value`, ставок.

## Campaigns.add

Тип кампании задаётся выбором структуры и после создания не меняется. Для ЕПК — `UnifiedCampaign`:

- **`BiddingStrategy`** (или `PackageBiddingStrategy`, взаимоисключающие). Внутри обязательны **обе** подструктуры — `Search` и `Network`.
  - Типы для `Search`: `WB_MAXIMUM_CLICKS`, `WB_MAXIMUM_CONVERSION_RATE`, `AVERAGE_CPC`, `AVERAGE_CPA`, `AVERAGE_CRR`, `HIGHEST_POSITION`, `PAY_FOR_CONVERSION`, `PAY_FOR_CONVERSION_CRR`, `SERVING_OFF`.
  - Для `Network`: те же плюс `NETWORK_DEFAULT`.
  - `PlacementTypes` (YES/NO): Search — `SearchResults`, `ProductGallery`, `DynamicPlaces`, `Maps`, `SearchOrganizationList`; Network — `Network`, `Maps`. Чтобы кампания была только поисковой, Network переводится в `SERVING_OFF`.
- **`WbMaximumClicks`**: `WeeklySpendLimit` обязателен; `BidCeiling` опционален — задавать не рекомендуется, режет эффективность стратегии.
- **`AverageCpa`**: обязательны `AverageCpa` и `GoalId`; опциональны `WeeklySpendLimit`, `BidCeiling`, `ExplorationBudget`.
- **`CustomPeriodBudget`** (`SpendLimit`, `StartDate`, `EndDate`, `AutoContinue`) — альтернатива `WeeklySpendLimit`, вместе задавать нельзя.
- **`CounterIds.Items`** — счётчики Метрики.
- **`PriorityGoals`** — `GoalId` + `Value` (×1e6) + `IsMetrikaSourceOfValue`. Обязательны для многоцелевых стратегий и `MaxProfit` (минимум 2 цели).
- **`AttributionModel`** — `AUTO` (по умолчанию), `FCCD`, `LC`, `LSCCD`.
- **`Settings`** (YES/NO): `ADD_METRICA_TAG`, `ENABLE_SITE_MONITORING`, `ADD_TO_FAVORITES`, `CAMPAIGN_EXACT_PHRASE_MATCHING_ENABLED`, `ENABLE_AREA_OF_INTEREST_TARGETING`, `ENABLE_COMPANY_INFO`, `REQUIRE_SERVICING`, `ALTERNATIVE_TEXTS_ENABLED`.
- **`NegativeKeywordSharedSetIds`** — не более 3 наборов.
- `PackageBiddingStrategy` несовместима с `BiddingStrategy`, `PriorityGoals`, `CounterIds`, `AttributionModel`.

## AdGroups.add

Обязательны `Name` (1–255 символов), `CampaignId`, `RegionIds` (минимум один; `0` = все регионы). **Не более 1000 групп за вызов.** В архивные кампании группы не добавляются. `NegativeKeywords` — до 7 слов, до 35 символов на слово. `TrackingParams` — до 1024 символов.

## Ads.add

**Не более 1000 объявлений за вызов.** `TextAd`: обязательны `Title`, `Text`, `Mobile` (параметр устарел, система использует `NO`) и хотя бы одно из `Href` / `TurboPageId` / `VCardId` / `BusinessId`. Опционально: `Title2` (≤30), `DisplayUrlPath` (≤20), `AdImageHash`, `SitelinkSetId`, `AdExtensionIds` (≤50), `PriceExtension`, `ErirAdDescription`.

**Устарели и не сохраняются:** `TurboPageId`, `VCardId`, `PreferVCardOverBusiness`. Для товарных форматов ЕПК — `ShoppingAd` / `ListingAd` с обязательными `FeedId` и `DefaultTexts`, `FeedFilterConditions` до 30 фильтров.

## Автотаргетинг = псевдо-ключевая фраза

Управляется **через сервис Keywords**, а не отдельным объектом:
- `Keywords.add` со значением `Keyword: "---autotargeting"`;
- в `Keywords.get` возвращается `CriteriaType: AUTOTARGETING`;
- ставка — через `KeywordBids.set`; остановка/запуск — `Keywords.suspend`/`resume`;
- **один автотаргетинг на группу**; для групп с показами на Поиске он включён по умолчанию и не отключается, в РСЯ — выключен, но может быть включён.

## Лимиты и баллы

- Не более **5 одновременных запросов** от одного рекламодателя.
- Заголовок `Units` в каждом ответе: `потрачено/остаток/суточный лимит`. Суточный лимит зависит от активности аккаунта, пополняется по 1/24 в час скользящим окном.
- **20 баллов штрафа** за ошибку вызова метода (кроме серверных ошибок) — валидировать план до отправки дешевле, чем ловить ошибки API.
- Не более 5 офлайн-отчётов в очереди на пользователя; готовый отчёт живёт 5 часов, повторный идентичный запрос в этом окне отдаётся сразу и бесплатно.
- Для синхронизации локальных данных — сервис `Changes` (`checkDictionaries` → `checkCampaigns` → `check`), а не повторные `get`.

См. также: «Типы кампаний и ЕПК» (`direct://kb/campaign-types`), «Технические ограничения Директа» (`direct://kb/tech-limits`), «Возможности сервера» (`direct://kb/server-capabilities`)

Источники: [Campaign](https://yandex.ru/dev/direct/doc/ru/objects/campaign) · [add: UnifiedCampaign](https://yandex.ru/dev/direct/doc/ref-v5/campaigns/add-unified-campaign.html) · [AdGroups.add](https://yandex.ru/dev/direct/doc/ref-v5/adgroups/add.html) · [Ads.add](https://yandex.ru/dev/direct/doc/ref-v5/ads/add.html) · [Автотаргетинг в API](https://yandex.ru/dev/direct/doc/ru/best-practice/auto-targeting.html) · [Эффективная работа с API](https://yandex.ru/dev/direct/doc/ru/optimize)
