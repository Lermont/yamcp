"""Client-facing report copy, evidence separation and transactional regressions."""
from __future__ import annotations

import json
import re
from copy import deepcopy

import pytest

from test_creation_report import built
from test_report_pipeline import API, register
from test_report_pipeline import isolated_breakdown_collector as isolated_breakdown_collector
from test_server import _in_server
from yadirect_mcp import creation_report, policy, report_copy
from yadirect_mcp import report_pipeline as pipeline


def embedded(document):
    return json.loads(re.search(
        r'<script type="application/json" id="report-data">(.*?)</script>', document, re.S,
    )[1])


@pytest.mark.parametrize("mode", ["report", "campaign_setup"])
def test_report_guidance_loaded_in_both_modes_without_policy_mutation(tmp_path, mode):
    snapshot = policy.get()
    result = _in_server(
        "import asyncio, json\n"
        "import yadirect_mcp.server as s\n"
        "p = asyncio.run(s.direct_policy()).structuredContent\n"
        "print(json.dumps({'instructions': s.mcp.instructions, 'result': p, "
        "'features': s.runtime.describe(s.SETTINGS)['features']}))",
        str(tmp_path), YD_MODE=mode,
    )
    assert report_copy.INSTRUCTIONS in result["instructions"]
    assert result["result"]["client_report_editorial"] == report_copy.guidance()
    assert result["result"]["policy"] == snapshot
    assert "client_report_agency_voice" in result["features"]


def test_waiting_brief_guides_setup_copy(tmp_path):
    _, response, _, _ = register(tmp_path)
    assert response["brief"]["status"] == "waiting_for_statistics"
    assert response["brief"]["rules"] == report_copy.INSTRUCTIONS


@pytest.mark.asyncio
async def test_brief_budget_and_insight_preserve_statistics_history_and_settings(tmp_path):
    cfg, _, original, _ = register(tmp_path, history=True)
    response = await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02")
    state = pipeline._load(tmp_path / "client")
    period = state["model"]["periods"][0]
    old_revision = period["dataRevision"]
    period["budget"] = {period["campaignIds"][0]: 7500}
    period["dataRevision"] = pipeline.data_revision(state["model"], period)
    assert period["dataRevision"] != old_revision
    state = pipeline._commit(tmp_path / "client", state)
    brief = pipeline.brief(state)
    assert brief["totals"]["spend"] == 120.25
    assert brief["totals"]["conversions"] == 2.5
    assert brief["measurement"]["attribution"] == "автоматическая атрибуция"
    assert "в прошлые дни они могли отличаться" in brief["limitations"]
    assert brief["budget"]["period_plan_rub_including_vat"] == 7500
    assert brief["budget"]["account_balance"] is None
    assert brief["budget"]["top_up"] is None
    assert report_copy.INSTRUCTIONS in brief["rules"]
    assert len(json.dumps(brief, ensure_ascii=False)) < pipeline.MAX_BRIEF_CHARS
    copy = {
        "title": "Предлагаем проверить качество обращений",
        "text": "За период расход составил 120,25 ₽ с НДС. Зафиксировано 2,5 достижения "
        "цели № 77. Число реальных обращений и продаж пока неизвестно. Предлагаем "
        "сопоставить результаты с вашим учётом обращений в CRM. До этой проверки "
        "рекомендуем сохранить текущий темп расходов.",
    }
    pipeline.write_insight(cfg, "client", brief["period_id"], brief["data_revision"], copy)
    document = (tmp_path / "client/index.html").read_text("utf-8")
    public = embedded(document)
    assert public["setup"] == original["setup"]
    assert public["periods"][1:] == original["periods"]
    assert public["daily"] == state["model"]["daily"]
    assert public["periods"][0]["budget"] == period["budget"]
    assert public["periods"][0]["insight"]["text"] == copy["text"]
    assert public["views"] == original["views"]
    assert 'id="tab-setup"' in document and 'id="tab-statistics"' in document
    assert response["published"] is False
    assert policy.get()["reporting"]["client_report"]["canonical_url"] == (
        "https://bi-data.ru/elama/{client_login}/"
    )


@pytest.mark.asyncio
async def test_rejected_internal_insight_leaves_html_pointer_and_history_intact(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    brief = (await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02"))["brief"]
    folder = tmp_path / "client"
    html, state = (folder / "index.html").read_bytes(), pipeline._load(folder)
    with pytest.raises(ValueError, match="insight.text"):
        pipeline.write_insight(cfg, "client", brief["period_id"], brief["data_revision"], {
            "title": "Бюджет", "text": "Пополнение известно из сообщения клиента. "
            "Это не разрешение повысить лимиты.",
        })
    assert (folder / "index.html").read_bytes() == html
    assert pipeline._load(folder) == state


@pytest.mark.parametrize("text", [
    "Цели проверены через MCP/API.", "Подробности в JSON.",
    "Каталог целей недоступен.", "Восстановление нулей после сверки.",
    "Публикация прошла SHA-256 и readback.", "Клиенту рекомендуем увеличить бюджет.",
    "Сайт клиента требует проверки.", "Пополнение известно из сообщения клиента.",
])
def test_explicit_internal_notes_rejected_in_authored_fields(tmp_path, text):
    _, _, seed, _ = register(tmp_path, history=True)
    locations = [
        ("setup", "summary", "text"), ("setup", "nextStep", "text"),
        ("setup", "checks", 0, "text"), ("setup", "next", 0, "text"),
        ("campaigns", 0, "purpose"),
        ("periods", 0, "sourceNote"), ("periods", 0, "completenessNote"),
        ("periods", 0, "work", 0, "text"), ("periods", 0, "next", 0, "text"),
    ]
    for location in locations:
        model = deepcopy(seed)
        target = model
        for key in location[:-1]:
            target = target[key]
        target[location[-1]] = text
        with pytest.raises(ValueError, match="внутренний комментарий"):
            pipeline._renderer().render(model)


def test_ids_names_ad_copy_and_settings_are_not_mechanically_removed(tmp_path):
    _, _, model, _ = register(tmp_path)
    model["campaigns"][0]["name"] = "Разработка API для клиентов"
    model["setup"]["ad"]["titles"] = ["Разработка API и интеграций"]
    model["campaigns"][0]["settings"].append({"label": "Цель", "value": "№ 770055"})
    model["setup"]["summary"]["text"] = "Мы проверили цель № 770055 на вашем сайте."
    assert embedded(pipeline._renderer().render(model)) == model


@pytest.mark.asyncio
async def test_private_slice_evidence_does_not_enter_html_and_coverage_survives(tmp_path):
    cfg, _, _, _ = register(tmp_path)
    await pipeline.refresh(cfg, API(), "client", as_of="2026-08-02")
    state = pipeline._load(tmp_path / "client")
    part = state["model"]["periods"][0]["breakdowns"]["slices"]["queries"]
    part.update(reportType="SEARCH_QUERY_PERFORMANCE_REPORT", unavailableAdIdRows=1,
                reconciliation=[{"trafficMatches": False, "inferredGoalZeros": 3}])
    original = deepcopy(state["model"])
    document = pipeline._renderer().render(state["model"])
    public = embedded(document)["periods"][0]["breakdowns"]["slices"]["queries"]
    assert public["coverageLimited"] is True
    assert public["rows"] == part["rows"]
    assert "inferredGoalZeros" not in document and "reportType" not in document
    assert "reconciliation" not in public and "unavailableAdIdRows" not in public
    assert state["model"] == original
    assert embedded(pipeline._renderer().render(embedded(document))) == embedded(document)


@pytest.mark.parametrize("verified", [None, False, True])
def test_creation_report_addresses_client_without_claiming_unverified_work(verified):
    plan, execution = built()
    model = creation_report.build_model(plan, execution, {"verified": verified})
    original = deepcopy(model)
    document = creation_report.render(model)
    assert "Мы подготовили для вас" in document
    assert "Ваш рекламный кабинет" in document
    assert "Клиентский кабинет" not in document
    assert "расход бюджета пока не начались" not in document
    assert "без НДС" in document
    assert ("Мы проверили сохранённые настройки" in document) == (verified is True)
    assert ("Часть параметров ниже" in document) == (verified is not True)
    assert "Direct API" not in document and "plan_hash" not in document
    assert model == original
    assert creation_report.public_url("porg-test", "https://bi-data.ru/elama") == (
        "https://bi-data.ru/elama/porg-test/create/"
    )
