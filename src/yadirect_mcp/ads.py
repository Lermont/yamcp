"""Объявления и посадочные страницы — то, чего в отчётах нет вообще.

Reports API не отдаёт ссылку объявления ни в одном типе отчёта: `Href`
существует только в `ads.get`, и запрос такого поля в отчёте отваливается с
error_code=8000. Из-за этого разбор упирается в стену на самом частом вопросе:
куда ведёт группа и размечена ли ссылка. Отчёт покажет, что фраза «свадебный
букет» собрала клики по 20 рублей, но не покажет, что все группы ведут на
главную, — а это разные диагнозы и разные работы.

Тул отвечает на задачу «что показывается и куда ведёт», поэтому кроме списка
объявлений он сразу отдаёт сводку по посадочным: уникальные URL, сколько
объявлений на каждый, из каких кампаний и что в UTM. Обычно именно эта сводка
и нужна, а полный список объявлений — уже для точечной правки.

Запрашивается только блок TextAd. Остальные типы объявлений (графические,
видео, смарт) вернутся с href=null и своим значением type — это видно в
ответе и в поле ads_without_href, а не превращается в «ссылок нет».

Архивные отфильтрованы здесь, а не через SelectionCriteria.States: неверное
значение перечисления в критерии стоит 20 баллов и целый вызов, а State и так
приходит в каждом объекте.

Пустой SelectionCriteria метод не принимает — требует хотя бы один из Ids,
AdGroupIds, CampaignIds (error_code=4001). Поэтому без явной выборки сначала
читаются кампании клиента, и CampaignIds уходят пачками по 10: столько метод
берёт за раз.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

# ads.get отдаёт до 10 000 объектов за вызов; в кабинете столько объявлений не
# бывает, но потолок нужен, чтобы Page был осмысленным.
MAX_LIMIT = 10000
# ads.get принимает не более 10 CampaignIds в критерии — режем на пачки.
CAMPAIGNS_PER_CALL = 10
# Потолок автоподбора, когда выборку не сузили: 50 кампаний — это 5 вызовов,
# дальше баллы тратятся на кабинет, который всё равно не разобрать за раз.
MAX_AUTO_CAMPAIGNS = 50
# Полный список объявлений едет в контекст модели. Тексты, заголовки и ссылки
# на полусотне объявлений — это уже страница; дальше отдаём только сводку по
# посадочным, она и отвечает на вопрос «куда ведёт реклама».
ADS_PREVIEW = 50

ARCHIVED = "ARCHIVED"

_AD_FIELDS = ["Id", "CampaignId", "AdGroupId", "Type", "Subtype", "State", "Status"]
_TEXT_AD_FIELDS = [
    "Title",
    "Title2",
    "Text",
    "Href",
    "DisplayUrlPath",
    "SitelinkSetId",
    "VCardId",
    "AdImageHash",
    "TurboPageId",
]


def _utm(href: str | None) -> dict[str, str]:
    """UTM-метки из ссылки.

    Динамические параметры Директа (`{campaign_id}` и прочие) остаются как
    есть: подставляются они на клике, и модель должна видеть именно шаблон,
    иначе «метка есть» и «метка работает» перестанут различаться.
    """
    if not href:
        return {}
    query = urlsplit(href).query
    return {
        k: v for k, v in parse_qsl(query, keep_blank_values=True) if k.startswith("utm_")
    }


def _shape(ad: dict[str, Any]) -> dict[str, Any]:
    text_ad = ad.get("TextAd") or {}
    href = text_ad.get("Href")
    out: dict[str, Any] = {
        "id": ad.get("Id"),
        "campaign_id": ad.get("CampaignId"),
        "ad_group_id": ad.get("AdGroupId"),
        "type": ad.get("Type"),
        "state": ad.get("State"),
        "status": ad.get("Status"),
        "href": href,
    }
    if ad.get("Subtype"):
        out["subtype"] = ad.get("Subtype")
    if text_ad:
        out["title"] = text_ad.get("Title")
        if text_ad.get("Title2"):
            out["title2"] = text_ad.get("Title2")
        out["text"] = text_ad.get("Text")
        if text_ad.get("DisplayUrlPath"):
            out["display_url_path"] = text_ad.get("DisplayUrlPath")
        # Дополнения возвращаются идентификаторами, а не содержимым. Для
        # разбора важен факт «заполнено или нет» — за содержимым нужен свой
        # вызов, и тянуть его в каждый ответ незачем.
        out["sitelinks"] = text_ad.get("SitelinkSetId") is not None
        out["vcard"] = text_ad.get("VCardId") is not None
        out["image"] = text_ad.get("AdImageHash") is not None
        if text_ad.get("TurboPageId") is not None:
            out["turbo_page"] = True
    return out


def _landing_pages(ads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сводка по посадочным: URL → объявления, кампании, UTM.

    Сортировка по числу объявлений: сверху оказывается страница, на которую
    приходится основной трафик. Одна главная на все группы видна сразу.
    """
    pages: dict[str, dict[str, Any]] = {}
    for ad in ads:
        href = ad.get("href")
        if not href:
            continue
        page = pages.setdefault(
            href,
            {"url": href, "ads": 0, "campaigns": [], "utm": _utm(href)},
        )
        page["ads"] += 1
        cid = ad.get("campaign_id")
        if cid is not None and cid not in page["campaigns"]:
            page["campaigns"].append(cid)
    return sorted(pages.values(), key=lambda p: (-p["ads"], p["url"]))


def _domains(pages: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for page in pages:
        host = urlsplit(page["url"]).netloc.lower()
        if host and host not in seen:
            seen.append(host)
    return seen


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


async def _campaign_ids(api: Any, client_login: str) -> tuple[list[int], int]:
    """ID кампаний, когда выборку не сузили: без них критерий невалиден."""
    result = await api.call(
        "campaigns",
        "get",
        {
            "SelectionCriteria": {"States": ["ON", "OFF", "SUSPENDED", "ENDED"]},
            "FieldNames": ["Id"],
            "Page": {"Limit": 10000},
        },
        client_login=client_login,
    )
    ids = [c["Id"] for c in result.get("Campaigns", []) if c.get("Id") is not None]
    return ids[:MAX_AUTO_CAMPAIGNS], len(ids)


async def read(
    api: Any,
    client_login: str,
    *,
    campaign_ids: list[int] | None = None,
    ad_group_ids: list[int] | None = None,
    ad_ids: list[int] | None = None,
    include_archived: bool = False,
    limit: int = 1000,
) -> dict[str, Any]:
    """Прочитать объявления клиента вместе со сводкой по посадочным страницам."""
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit должен быть от 1 до {MAX_LIMIT}")

    base: dict[str, Any] = {}
    if ad_group_ids:
        base["AdGroupIds"] = [int(i) for i in ad_group_ids]
    if ad_ids:
        base["Ids"] = [int(i) for i in ad_ids]

    campaigns_total: int | None = None
    if campaign_ids:
        selected = [int(i) for i in campaign_ids]
    elif base:
        # Группы или сами объявления уже задают выборку — кампании не нужны,
        # и лишний вызов campaigns.get не делаем.
        selected = []
    else:
        selected, campaigns_total = await _campaign_ids(api, client_login)
        if not selected:
            return {
                "client_login": client_login,
                "count": 0,
                "landing_pages": [],
                "domains": [],
                "ads": [],
                "note": "У клиента нет кампаний, в которых могли бы быть объявления.",
            }

    criteria = (
        [{**base, "CampaignIds": chunk} for chunk in _chunks(selected, CAMPAIGNS_PER_CALL)]
        if selected
        else [base]
    )

    raw: list[dict[str, Any]] = []
    limited_by: Any = None
    for selection in criteria:
        result = await api.call(
            "ads",
            "get",
            {
                "SelectionCriteria": selection,
                "FieldNames": _AD_FIELDS,
                "TextAdFieldNames": _TEXT_AD_FIELDS,
                "Page": {"Limit": limit},
            },
            client_login=client_login,
        )
        raw.extend(result.get("Ads", []))
        if result.get("LimitedBy") is not None:
            limited_by = result["LimitedBy"]

    archived = [a for a in raw if a.get("State") == ARCHIVED]
    if not include_archived:
        raw = [a for a in raw if a.get("State") != ARCHIVED]

    ads = [_shape(a) for a in raw]
    pages = _landing_pages(ads)

    payload: dict[str, Any] = {
        "client_login": client_login,
        "count": len(ads),
        "landing_pages": pages,
        "domains": _domains(pages),
        "ads_without_href": sum(1 for a in ads if not a.get("href")),
        "ads_without_utm": sum(1 for a in ads if a.get("href") and not _utm(a["href"])),
        "ads": ads[:ADS_PREVIEW],
    }
    if len(ads) > ADS_PREVIEW:
        payload["ads_truncated"] = True
        payload["note"] = (
            f"Показаны первые {ADS_PREVIEW} объявлений из {len(ads)}. "
            "Сводка landing_pages посчитана по всем; за остальными объявлениями "
            "сузьте выборку через campaign_ids или ad_group_ids."
        )
    if archived and not include_archived:
        payload["archived_skipped"] = len(archived)
    if campaigns_total is not None and campaigns_total > len(selected):
        payload["campaigns_total"] = campaigns_total
        payload["campaigns_scanned"] = len(selected)
        payload["truncated"] = True
        payload["note"] = (
            f"Просмотрены первые {len(selected)} кампаний из {campaigns_total}. "
            f"Для остальных передайте campaign_ids явно."
        )
    # Директ обрезает выдачу молча — умолчание читается как «объявлений
    # больше нет», и вывод о посадочных делается по половине аккаунта.
    if limited_by is not None:
        payload["truncated"] = True
        payload["limited_by"] = limited_by
    return payload
