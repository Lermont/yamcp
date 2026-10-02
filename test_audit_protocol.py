"""Actual MCP wire checks against an isolated server with a fake Direct transport."""

import asyncio
import json
import os
import sys

import pytest
from mcp import Client, StdioServerParameters, types


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
@pytest.mark.parametrize("first_action", ["decline", "cancel", "accept"])
async def test_elicitation_schema_and_exact_ids_over_stdio(tmp_path, first_action, mode):
    server_code = """
import yadirect_mcp.server as s
class Api:
    last_units = None
    async def call_v501(self, service, method, params, **kwargs):
        assert service == "adextensions" and method == "add"
        return {"AddResults": [{"Id": 1921019359743476961}]}
    async def aclose(self):
        pass
s._client = Api()
s.mcp.run()
"""
    env = {
        **os.environ,
        "YD_TOKEN": "dummy",
        "YD_OUT_DIR": str(tmp_path),
        "YD_MODE": "campaign_setup",
        "YD_ALLOWED_LOGINS": "client",
    }
    prompts = []
    current_action = first_action

    async def consent(context, params):
        # Codex's typed form parser rejects unknown root keys (including title).
        wire_schema = params.requested_schema
        assert set(wire_schema) <= {"$schema", "type", "properties", "required"}
        assert wire_schema["type"] == "object"
        assert wire_schema["required"] == ["approve"]
        assert wire_schema["properties"]["approve"]["type"] == "boolean"
        assert "default" not in wire_schema["properties"]["approve"]
        prompts.append(params.message)
        return types.ElicitResult(
            action=current_action, content={"approve": True} if current_action == "accept" else None
        )

    parameters = StdioServerParameters(command=sys.executable, args=["-c", server_code], env=env)
    async with Client(
        parameters, mode=mode, read_timeout_seconds=10,
        elicitation_callback=consent,
        client_info=types.Implementation(name="audit-test", version="1"),
    ) as session:
        assert session.protocol_version == ("2025-11-25" if mode == "legacy" else mode)
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert "ctx" not in tools["direct_campaign_apply"].input_schema["properties"]
        assert not tools["direct_campaign_plan"].annotations.read_only_hint
        assert tools["direct_write_job"].annotations.read_only_hint
        assert not tools["direct_write_job"].annotations.destructive_hint
        ids_schema = tools["direct_keywords"].input_schema["properties"]["campaign_ids"]
        assert '"string"' in json.dumps(ids_schema) and '"integer"' in json.dumps(ids_schema)
        payload = {"client_login": "client", "assets_bundle": {"callouts": ["Delivery"]}}
        preview = await session.call_tool("direct_ad_assets_create", payload)
        assert not preview.is_error
        payload["confirmation"] = preview.structured_content["confirmation_required"]
        result = await session.call_tool("direct_ad_assets_create", payload)
        assert len(prompts) == 1
        assert preview.structured_content["plan_hash"] in prompts[0]
        if first_action != "accept":
            assert result.is_error
            assert result.structured_content["error_code"] == "mcp_elicitation_" + first_action
            assert result.structured_content["approval"]["client"]["name"] == "audit-test"
            assert result.structured_content["executed"] is False
            assert not list(tmp_path.glob("jobs/*.json"))
            # Same exact plan/token may be retried, but requires another real host response.
            current_action = "accept"
            result = await session.call_tool("direct_ad_assets_create", payload)
            assert len(prompts) == 2
        assert not result.is_error
        for _ in range(50):
            if result.structured_content["status"] != "running":
                break
            await asyncio.sleep(0.02)
            result = await session.call_tool(
                "direct_write_job",
                {"client_login": "client", "job_id": result.structured_content["job_id"]},
            )
        data = result.structured_content
        assert data["status"] == "complete"
        assert data["results"]["AdExtensions"][0]["Id"] == "1921019359743476961"
        assert json.loads(result.content[0].text) == data
        prompt_count = len(prompts)
        replay = await session.call_tool("direct_ad_assets_create", payload)
        assert replay.is_error
        assert len(prompts) == prompt_count
    journal = json.loads(next(tmp_path.glob("jobs/*.json")).read_text(encoding="utf-8"))
    assert journal["approval"]["actor"] == "audit-test"
    assert journal["approval"]["identity_assurance"] == "client_attested"
    assert [e["stage"] for e in journal["events"]] == ["request", "response"]
