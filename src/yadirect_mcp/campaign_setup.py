"""Безопасная сборка текстово-графической кампании из одного плана.

Модели неудобно вручную протаскивать ID между Campaigns.add, AdGroups.add,
Ads.add и Keywords.add. Этот модуль принимает дерево объектов, проверяет его,
а затем последовательно создаёт родительские объекты и подставляет их ID.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from typing import Any

MAX_BATCH = 1000

# Директ сверяет StartDate со своим «сегодня», а оно московское, а не то, что
# на машине с сервером. Для кабинета из Владивостока или Калининграда разница
# ровно в те сутки, из-за которых валидный план получает отказ (или наоборот).
# Москва с 2014 года часы не переводит, поэтому фиксированного смещения хватает.
MOSCOW = timezone(timedelta(hours=3))


def moscow_today() -> date:
    """Сегодняшняя дата по часам Директа."""
    return datetime.now(MOSCOW).date()


def confirmation_phrase(client_login: str) -> str:
    """Фраза, которую вызывающая модель передаёт только после согласия человека."""
    return f"CREATE CAMPAIGN {client_login}"


def normalize_plan(
    campaign: dict[str, Any],
    ad_groups: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Проверить план и вернуть независимую нормализованную копию."""
    if not isinstance(campaign, dict):
        raise ValueError("campaign должен быть JSON-объектом")
    campaign = deepcopy(campaign)
    groups = deepcopy(ad_groups)

    if not str(campaign.get("Name", "")).strip():
        raise ValueError("campaign.Name обязателен")
    if "Id" in campaign:
        raise ValueError("campaign.Id задавать нельзя: инструмент создаёт новую кампанию")
    if "TextCampaign" not in campaign or not isinstance(campaign["TextCampaign"], dict):
        raise ValueError("поддерживается новая текстово-графическая кампания: задайте TextCampaign")
    text_campaign = campaign["TextCampaign"]
    if not text_campaign.get("BiddingStrategy") and not text_campaign.get("PackageBiddingStrategy"):
        raise ValueError(
            "campaign.TextCampaign требует BiddingStrategy или PackageBiddingStrategy"
        )
    unsupported_types = {
        "UnifiedCampaign", "MobileAppCampaign", "DynamicTextCampaign",
        "CpmBannerCampaign", "SmartCampaign",
    }
    if unsupported_types.intersection(campaign):
        raise ValueError("в campaign нельзя смешивать TextCampaign с другим типом кампании")

    raw_start = campaign.get("StartDate")
    if not isinstance(raw_start, str):
        raise ValueError("campaign.StartDate обязателен в формате YYYY-MM-DD")
    try:
        start = date.fromisoformat(raw_start)
    except ValueError as exc:
        raise ValueError("campaign.StartDate должен иметь формат YYYY-MM-DD") from exc
    if start < (today or moscow_today()):
        raise ValueError("campaign.StartDate не может быть в прошлом")

    if not isinstance(groups, list) or not groups:
        raise ValueError("ad_groups должен содержать хотя бы одну группу")
    if len(groups) > MAX_BATCH:
        raise ValueError(f"за один запуск можно создать не более {MAX_BATCH} групп")

    for group_index, group in enumerate(groups):
        prefix = f"ad_groups[{group_index}]"
        if not isinstance(group, dict):
            raise ValueError(f"{prefix} должен быть JSON-объектом")
        if "CampaignId" in group:
            raise ValueError(f"{prefix}.CampaignId задавать нельзя")
        if not str(group.get("Name", "")).strip():
            raise ValueError(f"{prefix}.Name обязателен")
        region_ids = group.get("RegionIds")
        if not isinstance(region_ids, list) or not region_ids:
            raise ValueError(f"{prefix}.RegionIds должен содержать хотя бы один регион")

        ads = group.get("Ads")
        if not isinstance(ads, list) or not ads:
            raise ValueError(f"{prefix}.Ads должен содержать хотя бы одно объявление")
        for ad_index, ad in enumerate(ads):
            ad_prefix = f"{prefix}.Ads[{ad_index}]"
            if not isinstance(ad, dict):
                raise ValueError(f"{ad_prefix} должен быть JSON-объектом")
            if "AdGroupId" in ad:
                raise ValueError(f"{ad_prefix}.AdGroupId задавать нельзя")
            text_ad = ad.get("TextAd")
            if not isinstance(text_ad, dict):
                raise ValueError(f"{ad_prefix}.TextAd обязателен")
            for field in ("Title", "Text", "Mobile"):
                if field not in text_ad:
                    raise ValueError(f"{ad_prefix}.TextAd.{field} обязателен")
            if text_ad["Mobile"] not in {"YES", "NO"}:
                raise ValueError(f"{ad_prefix}.TextAd.Mobile должен быть YES или NO")
            if not text_ad.get("Href") and not text_ad.get("TurboPageId"):
                raise ValueError(f"{ad_prefix}.TextAd требует Href или TurboPageId")

        keywords = group.get("Keywords", [])
        if not isinstance(keywords, list):
            raise ValueError(f"{prefix}.Keywords должен быть массивом")
        normalized_keywords: list[dict[str, Any]] = []
        for keyword_index, keyword in enumerate(keywords):
            kw_prefix = f"{prefix}.Keywords[{keyword_index}]"
            if isinstance(keyword, str):
                keyword = {"Keyword": keyword}
            if not isinstance(keyword, dict):
                raise ValueError(f"{kw_prefix} должен быть строкой или JSON-объектом")
            if "AdGroupId" in keyword:
                raise ValueError(f"{kw_prefix}.AdGroupId задавать нельзя")
            if not str(keyword.get("Keyword", "")).strip():
                raise ValueError(f"{kw_prefix}.Keyword обязателен")
            normalized_keywords.append(keyword)
        group["Keywords"] = normalized_keywords

    return campaign, groups


def preview(
    client_login: str,
    campaign: dict[str, Any],
    ad_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """Полный проверенный план без обращения к API."""
    campaign, groups = normalize_plan(campaign, ad_groups)
    return {
        "status": "preview",
        "executed": False,
        "client_login": client_login,
        "summary": {
            "campaigns": 1,
            "ad_groups": len(groups),
            "ads": sum(len(g["Ads"]) for g in groups),
            "keywords": sum(len(g["Keywords"]) for g in groups),
        },
        "campaign": campaign,
        "ad_groups": groups,
        "confirmation_required": confirmation_phrase(client_login),
        "warning": (
            "Проверьте бюджет, стратегию, регионы, тексты, ссылки и ключевые фразы. "
            "Создание не атомарно; созданная кампания не запускается автоматически."
        ),
    }


def _action_id(action: dict[str, Any]) -> int | None:
    value = action.get("Id") if isinstance(action, dict) else None
    return value if isinstance(value, int) else None


class BatchFailure(RuntimeError):
    """Падение на одном из чанков add.

    Объекты из предыдущих чанков в Директе уже созданы. Если потерять их
    результаты вместе с исключением, оператор получит ответ «создано 0» при
    тысяче реально существующих объявлений — найти и убрать их будет нечем.
    """

    def __init__(self, original: Exception, results: list[dict[str, Any]]) -> None:
        super().__init__(str(original))
        self.original = original
        self.results = results


def _fatal(stage: str, exc: Exception) -> dict[str, Any]:
    source = getattr(exc, "original", exc)
    out: dict[str, Any] = {"stage": stage, "message": str(source)}
    if getattr(source, "code", None) is not None:
        out["error_code"] = source.code
    if getattr(source, "request_id", None):
        out["request_id"] = source.request_id
    return out


async def _add(
    api: Any,
    service: str,
    collection: str,
    objects: list[dict[str, Any]],
    client_login: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for offset in range(0, len(objects), MAX_BATCH):
        chunk = objects[offset : offset + MAX_BATCH]
        try:
            response = await api.call(
                service,
                "add",
                {collection: chunk},
                client_login=client_login,
            )
            actions = response.get("AddResults", [])
            if not isinstance(actions, list) or len(actions) != len(chunk):
                raise RuntimeError(
                    f"{service}.add вернул "
                    f"{len(actions) if isinstance(actions, list) else 0} "
                    f"результатов для {len(chunk)} объектов"
                )
        except Exception as exc:
            raise BatchFailure(exc, results) from exc
        results.extend(actions)
    return results


async def apply(
    api: Any,
    client_login: str,
    campaign: dict[str, Any],
    ad_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """Создать кампанию и дочерние объекты; не активировать показы."""
    campaign, groups = normalize_plan(campaign, ad_groups)
    campaign_actions = await _add(api, "campaigns", "Campaigns", [campaign], client_login)
    campaign_action = campaign_actions[0]
    campaign_id = _action_id(campaign_action)
    if campaign_id is None:
        return {
            "status": "failed",
            "executed": True,
            "client_login": client_login,
            "campaign": campaign_action,
            "message": "Кампания не создана; дочерние объекты не отправлялись.",
        }

    group_payloads = []
    for group in groups:
        payload = {k: v for k, v in group.items() if k not in {"Ads", "Keywords"}}
        payload["CampaignId"] = campaign_id
        group_payloads.append(payload)

    group_fatal = None
    try:
        group_actions = await _add(
            api, "adgroups", "AdGroups", group_payloads, client_login
        )
    # API не поддерживает транзакции и rollback, поэтому ловим всё: чанки до
    # упавшего уже создались, и их ID нужны, чтобы было что чинить.
    except Exception as exc:  # noqa: BLE001
        group_actions = getattr(exc, "results", [])
        group_fatal = _fatal("adgroups.add", exc)

    group_results: list[dict[str, Any]] = []
    flat_ads: list[tuple[int, dict[str, Any]]] = []
    flat_keywords: list[tuple[int, dict[str, Any]]] = []
    # strict=False: при обрыве на adgroups.add ответов меньше, чем групп.
    for index, (group, action) in enumerate(zip(groups, group_actions, strict=False)):
        group_id = _action_id(action)
        item = {
            "plan_index": index,
            "name": group["Name"],
            "id": group_id,
            "warnings": action.get("Warnings", []),
            "errors": action.get("Errors", []),
            "ads": [],
            "keywords": [],
        }
        group_results.append(item)
        if group_id is None:
            continue
        for ad in group["Ads"]:
            flat_ads.append((index, {**ad, "AdGroupId": group_id}))
        for keyword in group["Keywords"]:
            flat_keywords.append((index, {**keyword, "AdGroupId": group_id}))

    fatal_error = group_fatal
    # После обрыва на группах дочерние объекты не досылаем: план уже разъехался
    # с тем, что подтверждал человек.
    if flat_ads and fatal_error is None:
        try:
            actions = await _add(
                api, "ads", "Ads", [payload for _, payload in flat_ads], client_login
            )
        except Exception as exc:  # noqa: BLE001 — сохраняем ID кампании и групп
            actions = getattr(exc, "results", [])
            fatal_error = _fatal("ads.add", exc)
        # strict=False намеренно: после обрыва actions короче flat_ads, и в
        # результат должно попасть то, что успело создаться.
        for (group_index, _), action in zip(flat_ads, actions, strict=False):
            group_results[group_index]["ads"].append(action)
    if flat_keywords and fatal_error is None:
        try:
            actions = await _add(
                api, "keywords", "Keywords",
                [payload for _, payload in flat_keywords], client_login,
            )
        except Exception as exc:  # noqa: BLE001 — тот же разбор частичного отказа
            actions = getattr(exc, "results", [])
            fatal_error = _fatal("keywords.add", exc)
        for (group_index, _), action in zip(flat_keywords, actions, strict=False):
            group_results[group_index]["keywords"].append(action)

    requested_groups = len(groups)
    created_groups = sum(r["id"] is not None for r in group_results)
    requested_ads = sum(len(g["Ads"]) for g in groups)
    created_ads = sum(
        _action_id(action) is not None
        for result in group_results for action in result["ads"]
    )
    requested_keywords = sum(len(g["Keywords"]) for g in groups)
    created_keywords = sum(
        _action_id(action) is not None
        for result in group_results for action in result["keywords"]
    )
    complete = fatal_error is None and (
        created_groups == requested_groups
        and created_ads == requested_ads
        and created_keywords == requested_keywords
    )
    result = {
        "status": "complete" if complete else "partial",
        "executed": True,
        "client_login": client_login,
        "campaign_id": campaign_id,
        "campaign_warnings": campaign_action.get("Warnings", []),
        "groups": group_results,
        "summary": {
            "ad_groups": {"requested": requested_groups, "created": created_groups},
            "ads": {"requested": requested_ads, "created": created_ads},
            "keywords": {"requested": requested_keywords, "created": created_keywords},
        },
        "activated": False,
        "message": (
            "Кампания создана, но показы не запущены."
            if fatal_error is None
            else (
                f"Обрыв на шаге {fatal_error['stage']}. Кампания и перечисленные "
                "в groups объекты уже созданы — повторяйте только недостающие."
            )
        ),
    }
    if fatal_error is not None:
        result["fatal_error"] = fatal_error
    return result
