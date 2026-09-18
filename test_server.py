"""Smoke tests for MCP tool registration in each safety mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from yadirect_mcp import knowledge, policy


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


@pytest.mark.parametrize("mode", ["report", "campaign_setup"])
def test_effective_url_rule_is_loaded_in_each_mode(tmp_path, mode):
    result = _in_server(
        "import json\n"
        "import yadirect_mcp.server as s\n"
        "print(json.dumps({'instruction': s._instructions(s.SETTINGS), "
        "'policy': s.policy.AGENCY_POLICY_V1['version']}))",
        str(tmp_path), YD_MODE=mode,
    )
    assert "yd_campaign_name" in result["instruction"]
    assert "Href" in result["instruction"]
    assert result["policy"] == policy.AGENCY_POLICY_V1["version"]


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


READ_TOOLS = [
    "direct_policy",
    "direct_list_clients",
    "direct_campaigns",
    "direct_campaign_settings",
    "direct_campaign_audit",
    "direct_adgroups",
    "direct_goal_catalog",
    "direct_regions",
    "direct_account_settings",
    "direct_ads",
    "direct_keywords",
    "direct_report",
    "direct_client_report",
    "direct_read_report",
    "direct_read_artifact",
    "direct_wordstat",
    "direct_runtime",
    "direct_pending_actions",
    "direct_product_source",
]


def test_report_mode_exposes_only_read_tools(tmp_path):
    assert _tool_names("report", str(tmp_path)) == READ_TOOLS


def test_campaign_setup_mode_adds_guarded_write_tool(tmp_path):
    assert _tool_names("campaign_setup", str(tmp_path)) == [
        *READ_TOOLS,
        "direct_feed_create",
        "direct_publish_job",
        "direct_verify_job",
        "direct_write_job",
        "direct_campaign_plan",
        "direct_campaign_apply",
        "direct_campaign_repair",
        "direct_ad_assets_create",
    ]


def test_legacy_raw_setup_is_removed(tmp_path):
    payload = _in_server(
        "import json\nimport yadirect_mcp.server as s\n"
        "print(json.dumps({'removed': not hasattr(s, 'direct_campaign_setup')}))",
        str(tmp_path),
        YD_MODE="campaign_setup",
    )
    assert payload["removed"]


def test_server_attaches_units_for_the_current_operation(tmp_path):
    payload = _in_server(
        "import json\n"
        "import yadirect_mcp.server as s\n"
        "class Fake:\n"
        "    last_units = None\n"
        "    def units_since(self, mark):\n"
        "        return {'scope': 'current_mcp_operation', "
        "'requests_with_units': 1, 'spent': 17, 'rest': 983, 'daily': 1000, "
        "'truncated': False, 'by_operation': [], 'requests': "
        "[{'spent': 17, 'rest': 983, 'daily': 1000}]}\n"
        "s._client = Fake()\n"
        "print(s._ok({'status': 'complete'}, units_mark=0).content[0].text)",
        str(tmp_path),
    )
    assert payload["units"] == {"spent": 17, "rest": 983, "daily": 1000}
    assert payload["units_usage"]["spent"] == 17
    assert payload["units_usage"]["scope"] == "current_mcp_operation"


def test_typed_preview_does_not_persist_one_time_confirmation(tmp_path):
    payload = _in_server(
        "import asyncio, json\n"
        "from pathlib import Path\n"
        "from test_bundle import source_bundle\n"
        "import yadirect_mcp.server as s\n"
        "class Fake:\n"
        "    last_units = None\n"
        "    async def call(self, service, method, params, client_login=None):\n"
        "        return {'GeoRegions': [{'GeoRegionId': 213, "
        "'GeoRegionName': 'Москва', 'GeoRegionType': 'City', 'ParentId': None}]}\n"
        "    async def call_v501(self, service, method, params, client_login=None):\n"
        "        if service == 'sitelinks':\n"
        "            return {'SitelinksSets': [{'Id': 10, 'Sitelinks': "
        "[{'Href': 'https://example.test/link'}] * 8}]}\n"
        "        return {'Campaigns': []}\n"
        "s._client = Fake()\n"
        "async def checked_preflight(*args, **kwargs): return {'status': 'PASS'}\n"
        "s.executor.preflight = checked_preflight\n"
        "async def checked_urls(pages, **kwargs):\n"
        "    return [{'url': p['url'], 'ok': True, 'status_code': 200} for p in pages]\n"
        "s.executor.link_checks.landing.inspect_pages = checked_urls\n"
        "result = asyncio.run(s.direct_campaign_apply("
        "'client', source_bundle())).structuredContent\n"
        "stored = Path(result['artifact_path']).read_text(encoding='utf-8')\n"
        "print(json.dumps({'token_on_disk': result['confirmation_required'] in stored, "
        "'status': result['status']}))",
        str(tmp_path),
        YD_MODE="campaign_setup",
    )
    assert payload == {"token_on_disk": False, "status": "preview"}


def test_typed_apply_generates_and_publishes_creation_report(tmp_path):
    payload = _in_server(
        "import asyncio, json\n"
        "from pathlib import Path\n"
        "from test_bundle import source_bundle\n"
        "from test_executor import FakeApi\n"
        "import yadirect_mcp.server as s\n"
        "async def fake_preflight(api, plan, **kwargs):\n"
        "    return {'status': 'PASS', 'regions': [], 'goals': {}}\n"
        "async def fake_readback(api, plan, result):\n"
        "    return {'verified': True, 'comparison': {}, "
        "'policy_audit': {'ready': True}, "
        "'counts': {'campaigns': 2, 'ad_groups': 2, 'ads': 6, 'keywords': 4}, "
        "'objects': {}}\n"
        "def fake_publish(path, **kwargs):\n"
        "    return {'status': 'published', 'public_url': "
        "'https://bi-data.ru/elama/client/create/', "
        "'verified': True, 'sha256': 'abc'}\n"
        "s.executor.preflight = fake_preflight\n"
        "s.executor.readback = fake_readback\n"
        "s.creation_report.publish = fake_publish\n"
        "s._client = FakeApi()\n"
        "s._client.last_units = None\n"
        "from types import SimpleNamespace\n"
        "class Host:\n"
        "    async def elicit(self, **kwargs):\n"
        "        return SimpleNamespace(action='accept', data=SimpleNamespace(approve=True))\n"
        "async def run():\n"
        "    source = source_bundle()\n"
        "    preview = (await s.direct_campaign_apply('client', source)).structuredContent\n"
        "    return (await s.direct_campaign_apply("
        "'client', source, preview['confirmation_required'], ctx=Host())).structuredContent\n"
        "result = asyncio.run(run())\n"
        "report = result['creation_report']\n"
        "html = Path(report['artifact_path']).read_text(encoding='utf-8')\n"
        "print(json.dumps({'status': report['status'], "
        "'verified': report['verified'], 'url': report['public_url'], "
        "'has_campaigns': 'Отчёт о создании рекламных кампаний' in html}))",
        str(tmp_path),
        YD_MODE="campaign_setup",
        YD_CREATE_REPORT_SSH_HOST="reports.example.test",
        YD_CREATE_REPORT_REMOTE_ROOT="/var/www/reports",
        YD_CREATE_REPORT_PUBLIC_BASE_URL="https://bi-data.ru/elama",
    )
    assert payload == {
        "status": "published",
        "verified": True,
        "url": "https://bi-data.ru/elama/client/create/",
        "has_campaigns": True,
    }


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


@pytest.mark.parametrize("mode", ["report", "campaign_setup"])
def test_publication_requirement_covers_separate_api_scripts_in_every_mode(tmp_path, mode):
    instructions = _in_server(
        "import json; print(json.dumps(mcp.instructions))",
        str(tmp_path),
        YD_MODE=mode,
    )
    assert "Единый клиентский отчёт" in instructions
    assert "https://bi-data.ru/elama/<client_login>/" in instructions
    assert "сохраняя настройку и предыдущие периоды" in instructions
    assert "direct://kb/client-report" in instructions
    if mode == "report":
        assert "direct_campaign_apply" not in instructions
        assert "creation_report.status" not in instructions
        assert "required_manual_actions" not in instructions
        assert "Режим report" in instructions
        return
    assert "любым способом" in instructions
    assert "отдельного скрипта" not in instructions or "выполнить явно" in instructions
    assert "Локальный preview не заменяет публикацию" in instructions
    assert "HTTP 200" in instructions
    assert "совпадение SHA-256" in instructions
    assert "нужен единый /<client_login>/" in instructions
    assert "вспомогательная выгрузка настройки" in instructions
    assert "не создавай кампании повторно" in instructions
    assert "В каждом объявлении РСЯ всегда добавляй" in instructions
    assert "required_manual_actions" in instructions
    assert "AdImageHashes его" in instructions


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
    for instructions in (with_budget, without_budget):
        assert "Поиск + РСЯ" in instructions
        assert "товарная кампания + Поиск" in instructions
        assert "30000 RUB в месяц суммарно" in instructions
        assert "не сокращай его до одного канала" in instructions
        assert "Не подменяй товарную кампанию обычной РСЯ" in instructions
        assert "всего 10000 рублей" not in instructions
    assert "fallback не отменяет общий месячный лимит" in with_budget


def test_mcp_native_results_annotations_and_local_reads(tmp_path):
    result = _in_server(
        '''import asyncio, json
from pathlib import Path
import yadirect_mcp.server as s
async def run():
    good = await mcp.call_tool("direct_policy", {})
    bad = await mcp.call_tool("direct_policy", {"policy_name": "missing"})
    path = Path(s.SETTINGS.out_dir) / "audit.json"
    path.write_text('{"large": "abcdefghij"}', encoding="utf-8")
    page = await mcp.call_tool("direct_read_artifact", {
        "path": str(path), "pointer": "/large", "limit": 5})
    blocked = await mcp.call_tool("direct_read_artifact", {"path": "../secret.json"})
    annotations = {tool.name: tool.annotations.model_dump() for tool in await mcp.list_tools()}
    return {"ok": not good.isError, "structured": good.structuredContent,
        "text": json.loads(good.content[0].text), "error": bad.isError,
        "error_data": bad.structuredContent, "page": page.structuredContent,
        "blocked": blocked.isError, "client_created": s._client is not None,
        "annotations": annotations}
print(json.dumps(asyncio.run(run())))''',
        str(tmp_path), YD_MODE="campaign_setup",
    )
    assert result["ok"] and result["structured"] == result["text"]
    assert result["error"] and result["error_data"]["error"]
    assert result["page"]["next_offset"] == 5
    assert result["blocked"]
    assert not result["client_created"]
    annotations = result["annotations"]
    for name in ("direct_report", "direct_campaign_audit",
                 "direct_wordstat", "direct_campaign_apply"):
        assert annotations[name]["readOnlyHint"] is False
        assert annotations[name]["idempotentHint"] is False
    assert annotations["direct_report"]["openWorldHint"] is True
    assert annotations["direct_campaign_plan"]["openWorldHint"] is False
    assert annotations["direct_campaign_plan"]["readOnlyHint"] is False
    assert annotations["direct_campaign_plan"]["destructiveHint"] is False


def test_server_lifespan_closes_client_and_preserves_host_logging(tmp_path):
    result = _in_server(
        '''import asyncio, json, logging
import yadirect_mcp.server as s
async def run():
    async with s._lifespan(mcp):
        client = s._api()
        assert client is s._api()
        assert not client._http.is_closed
    return {"closed": client._http.is_closed, "cleared": s._client is None,
            "root_handlers": len(logging.getLogger().handlers)}
print(json.dumps(asyncio.run(run())))''',
        str(tmp_path),
    )
    assert result == {"closed": True, "cleared": True, "root_handlers": 0}


def test_compact_plan_artifact_matches_full_plan(tmp_path):
    result = _in_server(
        '''import asyncio, json
from pathlib import Path
from test_bundle import source_bundle
import yadirect_mcp.server as s
async def run():
    preview = (await s.direct_campaign_plan("client", source_bundle())).structuredContent
    stored = json.loads(Path(preview["artifact_path"]).read_text(encoding="utf-8"))
    full = (await s.direct_campaign_plan("client", source_bundle(), False)).structuredContent
    return {"same": stored == full, "hash": stored["plan_hash"] == preview["plan_hash"],
        "compact": len(json.dumps(preview)) < len(json.dumps(full)),
        "client_created": s._client is not None}
print(json.dumps(asyncio.run(run())))''',
        str(tmp_path),
    )
    assert result == {"same": True, "hash": True, "compact": True, "client_created": False}


def test_stdio_wire_preserves_structured_success_and_error(tmp_path):
    result = _in_server(
        '''import asyncio, json, os, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def run():
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "yadirect_mcp"], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            good = await session.call_tool("direct_policy", {})
            bad = await session.call_tool("direct_policy", {"policy_name": "missing"})
            tools = await session.list_tools()
            return {"success": not good.isError, "error": bad.isError,
                "version": good.structuredContent["policy"]["version"],
                "has_error": "error" in bad.structuredContent,
                "text_matches": json.loads(good.content[0].text) == good.structuredContent,
                "reader": "direct_read_artifact" in [tool.name for tool in tools.tools]}
print(json.dumps(asyncio.run(run())))''',
        str(tmp_path), YD_MODE="report",
    )
    assert result == {"success": True, "error": True, "version": policy.AGENCY_POLICY_V1["version"],
                      "has_error": True, "text_matches": True, "reader": True}
