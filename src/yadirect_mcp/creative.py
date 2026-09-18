"""Creative requirements shared by planning, execution and UI handoff."""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from . import policy, products


def action_button(source: Any, main_href: str | None) -> dict[str, str] | None:
    """Keep an explicit, relevant UI choice outside the public API payload."""
    if source is None:
        return None
    if not isinstance(source, dict) or set(source) - {"text", "href", "destination", "reason"}:
        raise ValueError("action_button: допустимы text, href, destination, reason")
    result = {}
    for name in ("text", "reason"):
        value = source.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"action_button.{name}: требуется непустая строка")
        result[name] = value.strip()
    destination = source.get("destination", "main")
    if destination not in {"main", "contacts"}:
        raise ValueError("action_button.destination: main либо contacts")
    href = source.get("href", main_href if destination == "main" else None)
    if not isinstance(href, str) or not href.strip():
        raise ValueError("action_button.href: укажите основной URL либо URL страницы контактов")
    href = href.strip()
    parsed = urlsplit(href)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or len(href) > 1024):
        raise ValueError("action_button.href: требуется полный HTTP(S) URL до 1024 знаков")
    if main_href:
        if destination == "main" and href != main_href:
            raise ValueError("action_button.href: для main используйте основной href объявления")
        if destination == "contacts" and parsed.hostname != urlsplit(main_href).hostname:
            raise ValueError("action_button.href: контакты должны находиться на сайте объявления")
    return {**result, "href": href, "destination": destination}


def image_hashes(value: Any) -> list[str]:
    """Count distinct nonempty hashes; repeated entries never add creative variants."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and v.strip()))


def image_count_ok(value: Any) -> bool:
    count = len(image_hashes(value))
    return policy.NETWORK_IMAGES_MINIMUM <= count <= policy.RESPONSIVE_IMAGES_MAXIMUM


def button_requirement(button: dict[str, str] | None, **target: Any) -> dict[str, Any]:
    return {
        "rule": "ads.action_button", "required": True, "status": "pending_ui",
        "verification_method": "saved_ui_per_ad",
        "requirements": deepcopy(policy.AGENCY_POLICY_V1["creative"]["action_button"]),
        "requested": deepcopy(button),
        "message": (
            "Выбрать наиболее релевантный предложению текст из доступных кнопок интерфейса, "
            "явно заполнить URL (основной или проверенной страницы контактов), сохранить "
            "и повторно открыть объявление: проверить выбранный текст и полный URL. "
            "Публичный API не подтверждает сохранение кнопки."
        ),
        **target,
    }


def needs_button(payload: dict) -> bool:
    return bool(payload.get("Href") and (payload.get("AdImageHashes")
                                         or payload.get("VideoExtensionIds")))


def planned_actions(campaigns: list[dict]) -> list[dict]:
    actions = []
    for ci, item in enumerate(campaigns):
        for gi, group in enumerate(item["groups"]):
            for ai, ad in enumerate(group["ads"]):
                target = {"campaign_index": ci, "group_index": gi, "ad_index": ai}
                button = group["action_buttons"][ai]
                if needs_button(products.payload(ad)) or button:
                    actions.append(button_requirement(button, **target))
                if products.kind(ad):
                    actions.append({"rule": "product.generated_offers", "required": True,
                                    "message": "Проверить товары после фильтра: URL, цены, наличие "
                                               "и изображения; sample_urls не покрывают весь фид.",
                                    **target})
                if item["channel"] == "network":
                    actions.append(policy.network_carousel_requirement(**target))
    return actions


def executed_actions(plan: dict, campaigns: list[dict]) -> list[dict]:
    """Map only successfully created objects, preserving exact IDs and failed slots."""
    actions = []
    for item in campaigns:
        planned = plan["campaigns"][item["plan_index"]]
        for group in item["groups"]:
            planned_group = planned["groups"][group["plan_index"]]
            for ai, action in enumerate(group["ads"]):
                if action.get("Errors") or action.get("Id") is None:
                    continue
                target = {"campaign_id": item["id"], "group_id": group["id"],
                          "ad_id": str(action["Id"])}
                button = planned_group["action_buttons"][ai]
                if needs_button(products.payload(planned_group["ads"][ai])) or button:
                    actions.append(button_requirement(button, **target))
                if products.kind(planned_group["ads"][ai]):
                    actions.append({"rule": "product.generated_offers", "required": True,
                                    "message": "Проверить товары после фильтра: URL, цены, наличие "
                                               "и изображения; sample_urls не покрывают весь фид.",
                                    **target})
                if item["channel"] == "network":
                    actions.append(policy.network_carousel_requirement(**target))
    return actions
