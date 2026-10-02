"""Offline Codex/Claude form acceptance fixture; no Direct client or external writes."""

import json
import secrets
from importlib.metadata import version

from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.types import CallToolResult, InputRequiredResult, TextContent

from yadirect_mcp import approval

server = MCPServer(
    "yadirect-sdk-v2-acceptance",
    instructions="Offline migration test only. No advertising or external operations.",
    request_state_security=RequestStateSecurity(
        keys=[secrets.token_bytes(32)], ttl=approval.DEFAULT_TTL_SECONDS,
    ),
)
registry = approval.ApprovalRegistry()
plan = {"client_login": "demo", "plan_hash": "a" * 64, "summary": {"local_counter_only": True}}
writes = 0


def result(data, *, error=False):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))],
                          structured_content=data, is_error=error)


@server.tool()
async def migration_preview() -> CallToolResult:
    """Issue a preview for incrementing an in-memory demonstration counter."""
    grant = registry.issue(plan["client_login"], plan["plan_hash"])
    return result({**plan, "confirmation": grant.phrase, "writes": writes})


@server.tool()
async def migration_apply(confirmation: str, ctx: Context) -> CallToolResult | InputRequiredResult:
    """Ask for consent, then increment only this process's demonstration counter."""
    global writes
    try:
        receipt = await registry.authorize(ctx, plan, "локальный тест счётчика", confirmation)
        writes += 1
        return result({"writes": writes, "approval": receipt})
    except approval.InputRequired as request:
        return request.result
    except (ValueError, approval.ConsentError) as exc:
        data = {"error": str(exc), "writes": writes}
        if isinstance(exc, approval.ConsentError):
            data.update(exc.as_dict())
        return result(data, error=True)


@server.tool()
async def migration_status() -> CallToolResult:
    """Read the SDK version and local counter without any external access."""
    return result({"sdk": version("mcp"), "writes": writes, "external_writes": False})


if __name__ == "__main__":
    server.run()
