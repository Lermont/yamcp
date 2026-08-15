"""Smoke tests for MCP tool registration in each safety mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from yadirect_mcp import knowledge


def _in_server(code: str, out_dir: str, **env_overrides: str):
    """Выполнить фрагмент в отдельном процессе с поднятым сервером.

    Сервер читает конфиг на импорте, поэтому режим и переменные окружения
    иначе не переключить в рамках одной сессии pytest.
    """
    env = os.environ.copy()
    env.update({"YD_TOKEN": "dummy", "YD_OUT_DIR": out_dir, "PYTHONIOENCODING": "utf-8"})
    env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, "-c", "from yadirect_mcp.server import mcp\n" + code],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    return json.loads(result.stdout)


def _tool_names(mode: str, out_dir: str) -> list[str]:
    return _in_server(
        "import asyncio, json\n"
        "print(json.dumps([tool.name for tool in asyncio.run(mcp.list_tools())]))",
        out_dir,
        YD_MODE=mode,
    )


def test_module_entrypoint_is_runnable(tmp_path):
    """Все конфиги MCP-клиентов из README стартуют сервер как `python -m yadirect_mcp`.

    Без __main__.py это падает на «cannot be directly executed», и ни одна
    документированная интеграция не поднимается.
    """
    env = os.environ.copy()
    env.update({"YD_TOKEN": "dummy", "YD_OUT_DIR": str(tmp_path), "YD_MODE": "report"})
    result = subprocess.run(
        [sys.executable, "-m", "yadirect_mcp"],
        input="",  # stdio-транспорт закрывается сразу по EOF
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "yandex-direct MCP" in result.stderr


def test_report_mode_exposes_only_read_tools(tmp_path):
    assert _tool_names("report", str(tmp_path)) == [
        "direct_list_clients",
        "direct_campaigns",
        "direct_report",
        "direct_read_report",
    ]


def test_campaign_setup_mode_adds_guarded_write_tool(tmp_path):
    assert _tool_names("campaign_setup", str(tmp_path)) == [
        "direct_list_clients",
        "direct_campaigns",
        "direct_report",
        "direct_read_report",
        "direct_campaign_setup",
    ]


# ── база знаний ──────────────────────────────────────────────────────────


def test_every_registered_doc_has_a_file():
    """Реестр и каталог knowledge/ расходятся молча: файл теряется при сборке
    пакета, а модель узнаёт об этом только в момент чтения ресурса.
    """
    for name in knowledge.DOCS:
        text = knowledge.read(name)
        assert text.startswith("# "), f"{name}: нет заголовка H1"
        assert len(text) > 500, f"{name}: подозрительно пустой документ"


def test_index_lists_every_doc():
    index = knowledge.index()
    for name in knowledge.DOCS:
        assert f"direct://kb/{name}" in index


@pytest.mark.parametrize("name", ["../config", "nope", "", "knowledge"])
def test_read_rejects_names_outside_the_registry(name):
    """Имя приходит из ресурсного URI, то есть снаружи: путь из него не собираем."""
    with pytest.raises(ValueError):
        knowledge.read(name)


def test_knowledge_is_exposed_as_resources(tmp_path):
    listing = _in_server(
        "import asyncio, json\n"
        "res = asyncio.run(mcp.list_resources())\n"
        "tpl = asyncio.run(mcp.list_resource_templates())\n"
        "print(json.dumps({'resources': [str(r.uri) for r in res],\n"
        "                  'templates': [t.uriTemplate for t in tpl]}))",
        str(tmp_path),
    )
    assert listing["resources"] == ["direct://kb"]
    assert listing["templates"] == ["direct://kb/{doc}"]


def test_default_weekly_budget_reaches_instructions(tmp_path):
    """Дефолт бюджета имеет смысл только если модель видит его без вопроса."""
    with_budget = _in_server(
        "import json; print(json.dumps(mcp.instructions))",
        str(tmp_path),
        YD_MODE="campaign_setup",
        YD_DEFAULT_WEEKLY_BUDGET="5000",
    )
    assert "5000" in with_budget

    without_budget = _in_server(
        "import json; print(json.dumps(mcp.instructions))",
        str(tmp_path),
        YD_MODE="campaign_setup",
        YD_DEFAULT_WEEKLY_BUDGET="",
    )
    assert "недельный бюджет по умолчанию" not in without_budget
    assert "direct://kb" in without_budget
