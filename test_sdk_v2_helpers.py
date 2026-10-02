"""Isolated settings and helper entrypoints, without reading real host configuration."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters, types

ROOT = Path(__file__).resolve().parent


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("YD_", "MCP_"))}
    return {**env, "PYTHONIOENCODING": "utf-8"}


def test_sdk_does_not_load_dotenv_or_mcp_environment(tmp_path):
    (tmp_path / ".env").write_text("MCP_DEBUG=true\nYD_MODE=campaign_setup\n", encoding="utf-8")
    env = {**clean_env(), "YD_TOKEN": "dummy", "YD_OUT_DIR": str(tmp_path), "MCP_DEBUG": "true"}
    code = ("import json; from yadirect_mcp.server import mcp, SETTINGS; "
            "print(json.dumps([SETTINGS.mode, mcp.settings.debug]))")
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            check=True, capture_output=True, text=True, timeout=30)
    assert json.loads(result.stdout) == ["report", False]


def test_local_session_uses_float_timeout_configured_cwd_and_structured_results(tmp_path):
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    config = ("[mcp_servers.yandex-direct]\ncommand = " + json.dumps(sys.executable)
              + '\nargs = ["-m", "yadirect_mcp"]\ncwd = ' + json.dumps(str(tmp_path))
              + '\n[mcp_servers.yandex-direct.env]\nYD_TOKEN = "dummy"\nYD_MODE = "report"'
              + '\nYD_OUT_DIR = ' + json.dumps(str(tmp_path / "out")))
    (config_dir / "config.toml").write_text(config, encoding="utf-8")
    code = ("import runpy; from pathlib import Path; Path.home = lambda: Path("
            + repr(str(tmp_path))
            + "); runpy.run_path(" + repr(str(ROOT / "scripts/local_mcp_session.py"))
            + ", run_name='__main__')")
    requests = [{"tool": "direct_runtime", "fields": ["dependencies", "out_dir", "mode"]},
                {"tool": "direct_policy", "arguments": {"policy_name": "missing"}}, {"stop": True}]
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=clean_env(),
                            input="\n".join(json.dumps(r) for r in requests) + "\n",
                            check=True, capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    connected, runtime, failure = map(json.loads, result.stdout.splitlines())
    assert connected["status"] == "MCP connected"
    assert runtime["dependencies"]["mcp"].startswith("2.")
    assert runtime["out_dir"] == str(tmp_path / "out") and runtime["mode"] == "report"
    assert failure["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_offline_ui_acceptance_fixture(tmp_path, mode):
    current = "decline"

    async def answer(context, params):
        assert "demo" in params.message and "a" * 64 in params.message
        return types.ElicitResult(action=current, content={"approve": True})

    params = StdioServerParameters(command=sys.executable,
                                  args=[str(ROOT / "scripts/sdk_v2_acceptance_server.py")],
                                  env=clean_env(), cwd=str(tmp_path))
    async with Client(params, mode=mode, elicitation_callback=answer,
                      read_timeout_seconds=10) as client:
        preview = (await client.call_tool("migration_preview", {})).structured_content
        payload = {"confirmation": preview["confirmation"]}
        for current in ("decline", "cancel"):
            refused = await client.call_tool("migration_apply", payload)
            assert refused.is_error and refused.structured_content["writes"] == 0
            assert refused.structured_content["error_code"] == "mcp_elicitation_" + current
        current = "accept"
        applied = await client.call_tool("migration_apply", payload)
        assert not applied.is_error and applied.structured_content["writes"] == 1
        assert (await client.call_tool("migration_apply", payload)).is_error
        status = (await client.call_tool("migration_status", {})).structured_content
        assert status["writes"] == 1 and status["external_writes"] is False
