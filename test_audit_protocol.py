"""Actual MCP wire checks against an isolated server with a fake Direct transport."""

import asyncio
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
@pytest.mark.parametrize("first_action", ["decline", "cancel", "accept"])
async def test_elicitation_schema_and_exact_ids_over_stdio(tmp_path, first_action):
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
        wire_schema = params.requestedSchema
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
    async with stdio_client(parameters) as (read, write), ClientSession(
        read,
        write,
        elicitation_callback=consent,
        client_info=types.Implementation(name="audit-test", version="1"),
    ) as session:
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
        assert "ctx" not in tools["direct_campaign_apply"].inputSchema["properties"]
        assert not tools["direct_campaign_plan"].annotations.readOnlyHint
        assert tools["direct_write_job"].annotations.readOnlyHint
        assert not tools["direct_write_job"].annotations.destructiveHint
        ids_schema = tools["direct_keywords"].inputSchema["properties"]["campaign_ids"]
        assert '"string"' in json.dumps(ids_schema) and '"integer"' in json.dumps(ids_schema)
        payload = {"client_login": "client", "assets_bundle": {"callouts": ["Delivery"]}}
        preview = await session.call_tool("direct_ad_assets_create", payload)
        assert not preview.isError
        payload["confirmation"] = preview.structuredContent["confirmation_required"]
        result = await session.call_tool("direct_ad_assets_create", payload)
        assert len(prompts) == 1
        assert preview.structuredContent["plan_hash"] in prompts[0]
        if first_action != "accept":
            assert result.isError
            assert result.structuredContent["error_code"] == "mcp_elicitation_" + first_action
            assert result.structuredContent["approval"]["client"]["name"] == "audit-test"
            assert result.structuredContent["executed"] is False
            assert not list(tmp_path.glob("jobs/*.json"))
            # Same exact plan/token may be retried, but requires another real host response.
            current_action = "accept"
            result = await session.call_tool("direct_ad_assets_create", payload)
            assert len(prompts) == 2
        assert not result.isError
        for _ in range(50):
            if result.structuredContent["status"] != "running":
                break
            await asyncio.sleep(0.02)
            result = await session.call_tool(
                "direct_write_job",
                {"client_login": "client", "job_id": result.structuredContent["job_id"]},
            )
        data = result.structuredContent
        assert data["status"] == "complete"
        assert data["results"]["AdExtensions"][0]["Id"] == "1921019359743476961"
        assert json.loads(result.content[0].text) == data
        prompt_count = len(prompts)
        replay = await session.call_tool("direct_ad_assets_create", payload)
        assert replay.isError
        assert len(prompts) == prompt_count
    journal = json.loads(next(tmp_path.glob("jobs/*.json")).read_text(encoding="utf-8"))
    assert journal["approval"]["actor"] == "audit-test"
    assert journal["approval"]["identity_assurance"] == "client_attested"
    assert [e["stage"] for e in journal["events"]] == ["request", "response"]
