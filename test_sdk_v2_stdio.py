"""Separate stdio processes exercise SDK v2's sealed continuation boundary."""

import json
import os
import sys

import pytest
from mcp import Client, MCPError, StdioServerParameters, types

SERVER = '''
import json
from importlib.metadata import version
import yadirect_mcp.server as s
class Fake:
    last_units = None
    writes = 0
    async def call_v501(self, service, method, params, **kwargs):
        assert (service, method) == ("adextensions", "add")
        self.writes += 1
        return {"AddResults": [{"Id": 123}]}
    async def aclose(self):
        pass
s._client = Fake()
@s.mcp.resource("test://sdk")
def probe():
    return json.dumps({"sdk": version("mcp"), "writes": s._client.writes})
s.mcp.run()
'''


def parameters(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("YD_")}
    env.update(YD_TOKEN="dummy", YD_OUT_DIR=str(tmp_path), YD_MODE="campaign_setup",
               YD_ALLOWED_LOGINS="client", YD_APPROVAL_MODE="elicitation")
    return StdioServerParameters(command=sys.executable, args=["-c", SERVER], env=env)


async def must_not_prompt(context, params):
    raise AssertionError("Raw session calls must expose input-required to the test")


async def probe(client):
    result = await client.read_resource("test://sdk")
    return json.loads(result.contents[0].text)


@pytest.mark.asyncio
async def test_sealed_state_rejects_tampering_argument_changes_and_replay(tmp_path):
    async with Client(parameters(tmp_path), mode="2026-07-28",
                      elicitation_callback=must_not_prompt, read_timeout_seconds=10) as client:
        catalog = await client.list_resources()
        assert "direct://kb" in {r.uri for r in catalog.resources}
        templates = await client.list_resource_templates()
        assert "direct://kb/{doc}" in {r.uri_template for r in templates.resource_templates}
        quality = await client.read_resource("direct://kb/forecast-quality")
        assert "CPC" in quality.contents[0].text
        initial = await probe(client)
        major, minor = map(int, initial["sdk"].split(".")[:2])
        assert major == 2 and minor >= 2
        assert initial["writes"] == 0
        good = await client.call_tool("direct_policy", {})
        bad = await client.call_tool("direct_policy", {"policy_name": "missing"})
        assert not good.is_error and bad.is_error
        for result in (good, bad):
            wire = result.model_dump(by_alias=True)
            assert wire["structuredContent"] == json.loads(wire["content"][0]["text"])
            assert "isError" in wire and "is_error" not in wire
        payload = {"client_login": "client", "assets_bundle": {"callouts": ["Delivery"]}}
        preview = await client.call_tool("direct_ad_assets_create", payload)
        payload["confirmation"] = preview.structured_content["confirmation_required"]
        session = client.session
        first = await session.call_tool(
            "direct_ad_assets_create", payload, allow_input_required=True,
        )
        assert isinstance(first, types.InputRequiredResult)
        assert "nonce" not in first.request_state  # SDK sealed, not plaintext registry state.
        assert not list(tmp_path.glob("jobs/*.json"))
        assert (await probe(client))["writes"] == 0
        competing = await session.call_tool("direct_ad_assets_create", payload)
        assert competing.is_error and "уже открыт" in competing.structured_content["error"]
        answer = {"consent": types.ElicitResult(action="accept", content={"approve": True})}
        for state, args in [
            (first.request_state + "tampered", payload),
            (first.request_state, {**payload, "client_login": "other"}),
            (first.request_state, {**payload, "assets_bundle": {"callouts": ["Changed"]}}),
        ]:
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await session.call_tool("direct_ad_assets_create", args, input_responses=answer,
                                        request_state=state, allow_input_required=True)
        assert (await probe(client))["writes"] == 0
        final = await session.call_tool(
            "direct_ad_assets_create", payload, input_responses=answer,
            request_state=first.request_state, allow_input_required=True,
        )
        assert not final.is_error
        assert final.structured_content["status"] == "complete"
        assert (await probe(client))["writes"] == 1
        replay = await session.call_tool(
            "direct_ad_assets_create", payload, input_responses=answer,
            request_state=first.request_state, allow_input_required=True,
        )
        assert replay.is_error
        assert (await probe(client))["writes"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_no_elicitation_capability_returns_structured_error_without_writes(tmp_path, mode):
    async with Client(parameters(tmp_path), mode=mode, read_timeout_seconds=10) as client:
        payload = {"client_login": "client", "assets_bundle": {"callouts": ["Delivery"]}}
        preview = await client.call_tool("direct_ad_assets_create", payload)
        payload["confirmation"] = preview.structured_content["confirmation_required"]
        refused = await client.call_tool("direct_ad_assets_create", payload)
        assert refused.is_error
        assert refused.structured_content["error_code"] == "mcp_elicitation_unsupported"
        assert refused.structured_content["executed"] is False
        assert (await probe(client))["writes"] == 0
