"""Smoke tests for MCP tool registration in each safety mode."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _tool_names(mode: str, out_dir: str) -> list[str]:
    env = os.environ.copy()
    env.update(
        {
            "YD_TOKEN": "dummy",
            "YD_OUT_DIR": out_dir,
            "YD_MODE": mode,
        }
    )
    code = (
        "import asyncio, json; "
        "from yadirect_mcp.server import mcp; "
        "print(json.dumps([tool.name for tool in asyncio.run(mcp.list_tools())]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return json.loads(result.stdout)


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
