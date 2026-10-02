"""HTML creation report and atomic publication contract."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from test_bundle import source_bundle
from test_executor import FakeApi
from yadirect_mcp import bundle, creation_report, executor


def built():
    source = source_bundle()
    source["channels"]["search"]["groups"][0]["ads"][0]["titles"][0] = (
        "<b>Безопасно</b>: решение для производственного бизнеса"
    )
    plan = bundle.compile_bundle(source, "porg-test")
    execution = asyncio.run(executor.apply(FakeApi(), plan))
    return plan, execution


@pytest.mark.parametrize("legacy_value", ["YES", "NO"])
def test_report_does_not_present_retired_geo_as_a_current_setting(legacy_value):
    plan, execution = built()
    model = creation_report.build_model(plan, execution)
    for campaign in model["campaigns"]:
        campaign["settings"]["ENABLE_AREA_OF_INTEREST_TARGETING"] = legacy_value
    document = creation_report.render(model)
    assert "Расширенный геотаргетинг" not in document
    assert "ENABLE_AREA_OF_INTEREST_TARGETING" not in document


def test_report_lists_every_created_object_and_escapes_html():
    plan, execution = built()
    model = creation_report.build_model(plan, execution)
    document = creation_report.render(model)

    assert model["schema"] == "direct_campaign_creation_report_v1"
    assert len(model["campaigns"]) == 2
    assert "Ключевые фразы" in document
    assert "Автоматический подбор запросов" in document
    assert "Комбинаторные объявления" in document
    assert "---autotargeting" not in document  # rendered as a targeting, not a phrase
    assert "Автотаргетинг" in document
    assert "&lt;b&gt;Безопасно&lt;/b&gt;" in document
    assert "<b>Безопасно</b>" not in document
    assert "источник:" not in document
    assert "<small>из " not in document
    assert "расхождени" not in document.lower()
    assert "Хеш плана" not in document
    for technical_name in (
        "WB_MAXIMUM_CLICKS",
        "SERVING_OFF",
        "PlacementTypes",
        "brand_options",
        "categories",
    ):
        assert technical_name not in document
    assert "Максимум кликов с недельным бюджетом" in document
    assert "Результаты поиска Яндекса" in document
    assert "Варианты заголовков" in document
    assert "Варианты текстов" in document
    assert "автоматически выбирает подходящую комбинацию" in document
    assert "Целевые запросы" in document
    assert "Узкие запросы" in document
    assert all(campaign["groups"] for campaign in model["campaigns"])


def test_report_prefers_api_readback_without_exposing_internal_comparison():
    plan, execution = built()
    campaign = execution["campaigns"][0]
    group = campaign["groups"][0]
    keyword = group["keywords"][0]
    ad = group["ads"][0]
    readback = {
        "verified": True,
        "objects": {
            "campaigns": [{
                "id": campaign["id"],
                "name": "Имя из API",
                "state": "OFF",
                "status": "DRAFT",
                "settings": {},
                "counter_ids": [12345],
                "priority_goals": [],
                "negative_keywords": [],
                "excluded_sites": [],
                "bidding_strategy": {},
            }],
            "ad_groups": [{
                "id": group["id"],
                "name": "Группа из API",
                "region_ids": [213],
            }],
            "keywords": [{
                "Id": keyword["Id"],
                "Keyword": "фраза из API",
                "Status": "DRAFT",
            }],
            "ads": [{
                "id": ad["Id"],
                "type": "RESPONSIVE_AD",
                "titles": ["Заголовок из API"],
                "texts": ["Текст из API"],
                "href": "https://example.test/api",
            }],
        },
    }
    model = creation_report.build_model(plan, execution, readback)
    first = model["campaigns"][0]
    assert model["verified"] is True
    assert first["name"] == "Имя из API"
    assert first["source"] == "Direct API"
    assert first["groups"][0]["keywords"][0]["keyword"] == "фраза из API"
    assert first["groups"][0]["ads"][0]["titles"] == ["Заголовок из API"]
    document = creation_report.render(model)
    assert "Имя из API" in document
    assert "Direct API" not in document


def test_persist_uses_standard_login_create_path(tmp_path):
    plan, execution = built()
    model = creation_report.build_model(plan, execution)
    path = creation_report.persist(model, tmp_path)
    assert path == tmp_path / "porg-test" / "create" / "index.html"
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert creation_report.public_url(
        "porg-test", "https://bi-data.ru/elama/"
    ) == "https://bi-data.ru/elama/porg-test/create/"


def test_publish_backs_up_atomically_and_verifies_public_bytes(tmp_path, monkeypatch):
    local = tmp_path / "index.html"
    local.write_text("<!doctype html><title>ok</title>", encoding="utf-8")
    commands = []

    def fake_run(args):
        commands.append(args)
        return None

    def fake_get(url, **_kwargs):
        return httpx.Response(
            200,
            content=local.read_bytes(),
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(creation_report, "_run", fake_run)
    monkeypatch.setattr(creation_report.httpx, "get", fake_get)
    result = creation_report.publish(
        local,
        client_login="porg-test",
        ssh_host="reports.example.test",
        remote_root="/srv/reports/elama",
        public_base_url="https://bi-data.ru/elama",
    )

    assert result["status"] == "published"
    assert result["verified"] is True
    assert result["public_url"].endswith("/porg-test/create/")
    assert len(commands) == 3
    assert "mkdir -p" in commands[0][-1]
    assert commands[1][0] == "scp"
    assert "index.html.backup-" in commands[2][-1]
    assert "mv -f" in commands[2][-1]
    assert "chown" not in commands[2][-1]


@pytest.mark.parametrize("href", ["javascript:alert(1)", "data:text/html,x", "//evil.test"])
def test_render_ad_rejects_unsafe_link_scheme(href):
    html = creation_report._render_ad({"titles": ["A"], "texts": ["B"], "href": href})
    assert '<a href=' not in html
