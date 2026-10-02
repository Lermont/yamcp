"""Guarded creation of reusable sitelink sets and callout extensions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import image_validation, policy
from .identifiers import parse_id

MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _images(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("assets_bundle.images должен быть массивом до 100 изображений")
    images, manifest = [], []
    for index, row in enumerate(raw):
        prefix = f"assets_bundle.images[{index}]"
        if not isinstance(row, dict) or set(row) - {"path", "image_data", "name", "type"}:
            raise ValueError(f"{prefix}: допустимы path или image_data, name и type")
        if ("path" in row) == ("image_data" in row):
            raise ValueError(f"{prefix}: требуется ровно одно из path/image_data")
        name = row.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 255:
            raise ValueError(f"{prefix}.name: от 1 до 255 символов")
        image_type = row.get("type", "AUTO")
        if image_type not in {"AUTO", "REGULAR", "WIDE", "FIXED_IMAGE"}:
            raise ValueError(f"{prefix}.type: AUTO, REGULAR, WIDE или FIXED_IMAGE")
        if "path" in row:
            path = Path(row["path"])
            if not path.is_absolute() or not path.is_file():
                raise ValueError(f"{prefix}.path должен быть абсолютным путём к файлу")
            with path.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        else:
            encoded = row["image_data"]
            if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
                raise ValueError(f"{prefix}.image_data: base64 до 10 МБ")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError(f"{prefix}.image_data: некорректный base64") from exc
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"{prefix}: размер изображения должен быть от 1 байта до 10 МБ")
        if not data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
            raise ValueError(f"{prefix}: поддерживаются только PNG, JPG и GIF")
        if image_type == "FIXED_IMAGE" and len(data) > 512 * 1024:
            raise ValueError(f"{prefix}: FIXED_IMAGE допускает не более 512 КБ")
        metadata = image_validation.inspect(data, image_type)
        images.append({"Name": name.strip(), "Type": image_type,
                       "ImageData": base64.b64encode(data).decode("ascii")})
        manifest.append({"name": name.strip(), "type": image_type, "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest(), **metadata})
    return images, manifest


def preview(plan: dict[str, Any]) -> dict[str, Any]:
    """Approval binds actual bytes while the response stays small and reviewable."""
    return {key: value for key, value in plan.items() if key != "images"}


async def read_sets(api: Any, client_login: str, set_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Read each reusable set once, not once per ad; fail on incomplete data."""
    ids = sorted({parse_id(value, "sitelink_set_id") for value in set_ids})
    result = {}
    for offset in range(0, len(ids), 10000):
        chunk = ids[offset : offset + 10000]
        response = await api.call_v501(
            "sitelinks", "get",
            {"SelectionCriteria": {"Ids": chunk}, "FieldNames": ["Id", "Sitelinks"]},
            client_login=client_login,
        )
        if response.get("LimitedBy") is not None:
            raise ValueError("sitelinks.get: усечённый ответ; число ссылок не подтверждено")
        for row in response.get("SitelinksSets", []):
            if row.get("Id") in chunk and isinstance(row.get("Sitelinks"), list):
                result[row["Id"]] = row
    missing = sorted(set(ids) - set(result))
    if missing:
        raise ValueError(f"Не удалось прочитать наборы быстрых ссылок: {missing}")
    return result


async def validate_references(
    api: Any, client_login: str, set_ids: list[int]
) -> list[dict[str, Any]]:
    sets = await read_sets(api, client_login, set_ids)
    checks = []
    for set_id, row in sets.items():
        count = len(row["Sitelinks"])
        if not policy.SITELINKS_MINIMUM <= count <= policy.SITELINKS_MAXIMUM:
            raise ValueError(f"Набор быстрых ссылок {set_id}: {count}; требуется от 4 до 8")
        checks.append({
            "sitelink_set_id": set_id, "count": count,
            "status": policy.PASS if count == policy.SITELINKS_RECOMMENDED else policy.WARNING,
            "recommended": policy.SITELINKS_RECOMMENDED,
        })
    return checks


async def enrich_ads(
    api: Any, client_login: str, ad_rows: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Attach verified counts; missing API evidence is unknown, never a PASS."""
    ids = [row["sitelink_set_id"] for row in ad_rows if row.get("sitelink_set_id")]
    error = None
    try:
        sets = await read_sets(api, client_login, ids)
    except Exception as exc:  # noqa: BLE001 - audit must retain other findings
        sets = {}
        error = str(exc)
    for row in ad_rows:
        value = sets.get(row.get("sitelink_set_id"))
        row["sitelink_count"] = len(value["Sitelinks"]) if value is not None else None
        row["sitelink_check_error"] = error if value is None else None
    return sets


def normalize(raw: dict[str, Any], client_login: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("assets_bundle должен быть объектом")
    unknown = sorted(set(raw) - {"sitelink_sets", "callouts", "images"})
    if unknown:
        raise ValueError(
            "assets_bundle содержит неизвестные поля: " + ", ".join(unknown)
        )
    source = deepcopy(raw)
    sitelink_sets = []
    for set_index, value in enumerate(source.get("sitelink_sets") or []):
        prefix = f"assets_bundle.sitelink_sets[{set_index}]"
        if not isinstance(value, dict) or set(value) != {"sitelinks"}:
            raise ValueError(f"{prefix} должен содержать только sitelinks")
        rows = value["sitelinks"]
        if (not isinstance(rows, list)
                or not policy.SITELINKS_MINIMUM <= len(rows) <= policy.SITELINKS_MAXIMUM):
            raise ValueError(f"{prefix}.sitelinks должен содержать от 4 до 8 ссылок")
        compiled = []
        seen_titles = set()
        for index, row in enumerate(rows):
            item_prefix = f"{prefix}.sitelinks[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{item_prefix} должен быть объектом")
            extra = set(row) - {"title", "href", "description"}
            if extra:
                raise ValueError(
                    f"{item_prefix} содержит неизвестные поля: {', '.join(sorted(extra))}"
                )
            title = str(row.get("title") or "").strip()
            href = str(row.get("href") or "").strip()
            description = str(row.get("description") or "").strip()
            parsed = urlsplit(href)
            if not title or len(title) > 30:
                raise ValueError(f"{item_prefix}.title: от 1 до 30 символов")
            if title.casefold() in seen_titles:
                raise ValueError(f"{prefix}.sitelinks содержит дубликаты заголовков")
            seen_titles.add(title.casefold())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(href) > 1024:
                raise ValueError(f"{item_prefix}.href должен быть полным HTTP(S) URL")
            if len(description) > 60:
                raise ValueError(f"{item_prefix}.description длиннее 60 символов")
            compiled_row = {"Title": title, "Href": href}
            if description:
                compiled_row["Description"] = description
            compiled.append(compiled_row)
        sitelink_sets.append({"Sitelinks": compiled})

    callouts = []
    seen = set()
    for index, raw_text in enumerate(source.get("callouts") or []):
        text = str(raw_text or "").strip()
        if not text or len(text) > 25:
            raise ValueError(
                f"assets_bundle.callouts[{index}] должен содержать 1–25 символов"
            )
        if text.casefold() in seen:
            raise ValueError("assets_bundle.callouts содержит дубликаты")
        seen.add(text.casefold())
        callouts.append({"Callout": {"CalloutText": text}})
    images, manifest = _images(source.get("images", []))
    if not sitelink_sets and not callouts and not images:
        raise ValueError("assets_bundle не содержит быстрых ссылок, уточнений или изображений")
    plan = {
        "schema": "direct_ad_assets_v1",
        "client_login": client_login,
        "api_version": "v501",
        "sitelink_sets": sitelink_sets,
        "callouts": callouts,
        "images": images,
        "image_manifest": manifest,
    }
    canonical = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    plan["plan_hash"] = hashlib.sha256(canonical).hexdigest()
    plan["summary"] = {
        "sitelink_sets": len(sitelink_sets),
        "sitelink_counts": [len(row["Sitelinks"]) for row in sitelink_sets],
        "callouts": len(callouts),
        "images": len(images),
    }
    plan["recommendations"] = [
        {"set_index": index, "count": len(row["Sitelinks"]),
         "recommended": policy.SITELINKS_RECOMMENDED,
         "message": ("Рекомендуется 8 полезных быстрых ссылок с описаниями, "
                     "без искусственного дублирования.")}
        for index, row in enumerate(sitelink_sets)
        if len(row["Sitelinks"]) < policy.SITELINKS_RECOMMENDED
    ]
    return plan


async def apply(api: Any, plan: dict[str, Any]) -> dict[str, Any]:
    # Validate the complete batch before sitelinks/callouts can be committed.
    for offset in range(0, len(plan.get("images", [])), 100):
        _images([{"name": row["Name"], "type": row.get("Type", "AUTO"),
                  "image_data": row["ImageData"]}
                 for row in plan["images"][offset:offset + 100]])
    login = plan["client_login"]
    results: dict[str, Any] = {}
    result = {"status": "complete", "executed": True, "client_login": login,
              "plan_hash": plan["plan_hash"], "results": results}
    for service, collection, rows, batch_size, identifier in (
        ("sitelinks", "SitelinksSets", plan["sitelink_sets"], 1000, "Id"),
        ("adextensions", "AdExtensions", plan["callouts"], 1000, "Id"),
        ("adimages", "AdImages", plan.get("images", []), 100, "AdImageHash"),
    ):
        results[collection] = []
        for offset in range(0, len(rows), batch_size):
            chunk = rows[offset : offset + batch_size]
            try:
                response = await api.call_v501(
                    service, "add", {collection: chunk}, client_login=login
                )
                actions = response.get("AddResults", [])
                if (not isinstance(actions, list)
                        or not all(isinstance(row, dict) for row in actions)):
                    raise RuntimeError(f"{service}.add вернул некорректный AddResults")
                results[collection].extend(actions)
                if len(actions) != len(chunk) or any(
                    row.get(identifier) is None or row.get("Errors") for row in actions
                ):
                    raise RuntimeError(f"{service}.add не создал все подтверждённые объекты")
            except Exception as exc:  # noqa: BLE001 - preserve IDs from earlier committed writes
                result.update(status="partial", error=str(exc), failed_service=service)
                return result
    hashes = [row["AdImageHash"] for row in results["AdImages"]]
    if hashes:
        try:
            response = await api.call_v501(
                "adimages", "get", {"SelectionCriteria": {"AdImageHashes": hashes},
                                    "FieldNames": ["AdImageHash", "Name", "Type"]},
                client_login=login,
            )
            found = {row["AdImageHash"] for row in response.get("AdImages", [])}
            result["images_verified"] = set(hashes) == found and "LimitedBy" not in response
            if not result["images_verified"]:
                result.update(status="readback_failed", error="Не все изображения найдены в API")
        except Exception as exc:  # noqa: BLE001 - upload may already be committed
            result.update(status="readback_failed", images_verified=False, error=str(exc))
    return result
