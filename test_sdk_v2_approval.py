"""Approval continuation adversarial cases; no advertising transport."""

import asyncio
import json

import pytest
from mcp import types

from test_approval_compatibility import PLAN, host
from yadirect_mcp import approval


def modern_host():
    ctx = host()
    ctx.protocol_version = "2026-07-28"
    ctx.request_state = None
    ctx.input_responses = None
    return ctx


async def begin(registry, ctx, grant):
    with pytest.raises(approval.InputRequired) as event:
        await registry.authorize(ctx, PLAN, "assets", grant.phrase)
    result = event.value.result
    assert PLAN["plan_hash"] in result.input_requests["consent"].params.message
    ctx.request_state = result.request_state
    ctx.input_responses = {
        "consent": types.ElicitResult(action="accept", content={"approve": True}),
    }
    ctx.elicit.assert_not_awaited()
    return result


@pytest.mark.asyncio
async def test_only_one_concurrent_resume_can_consume_reserved_token():
    registry, ctx = approval.ApprovalRegistry(), modern_host()
    grant = registry.issue(**PLAN)
    await begin(registry, ctx, grant)
    other = modern_host()
    with pytest.raises(ValueError, match="уже открыт"):
        await registry.authorize(other, PLAN, "assets", grant.phrase)
    outcomes = await asyncio.gather(
        registry.authorize(ctx, PLAN, "assets", grant.phrase),
        registry.authorize(ctx, PLAN, "assets", grant.phrase), return_exceptions=True,
    )
    assert len([r for r in outcomes if isinstance(r, dict) and r["decision"] == "accept"]) == 1
    assert len([r for r in outcomes if isinstance(r, ValueError)]) == 1
    assert not registry._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("action,content,code", [
    ("decline", None, "decline"), ("cancel", None, "cancel"),
    ("accept", {"approve": False}, "not_approved"),
    ("accept", {"approve": "true"}, "not_approved"),
    ("accept", {"approve": 1}, "not_approved"),
    ("accept", None, "invalid_response"),
])
async def test_rejection_releases_reservation_but_old_state_never_authorizes(action, content, code):
    registry, ctx = approval.ApprovalRegistry(), modern_host()
    grant = registry.issue(**PLAN)
    await begin(registry, ctx, grant)
    old_state = ctx.request_state
    ctx.input_responses = {"consent": types.ElicitResult(action=action, content=content)}
    with pytest.raises(approval.ConsentError) as err:
        await registry.authorize(ctx, PLAN, "assets", grant.phrase)
    assert err.value.code == "mcp_elicitation_" + code
    assert not registry._pending
    registry.validate(grant.phrase, **PLAN)
    ctx.request_state = None
    ctx.input_responses = None
    await begin(registry, ctx, grant)
    new_state = ctx.request_state
    ctx.request_state = old_state
    with pytest.raises(ValueError, match="резервированием"):
        await registry.authorize(ctx, PLAN, "assets", grant.phrase)
    ctx.request_state = new_state
    assert (await registry.authorize(ctx, PLAN, "assets", grant.phrase))["decision"] == "accept"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["login", "hash", "operation", "client", "nonce"])
async def test_resume_is_bound_to_plan_operation_client_and_reservation(change):
    registry, ctx = approval.ApprovalRegistry(), modern_host()
    grant = registry.issue(**PLAN)
    await begin(registry, ctx, grant)
    plan, operation = dict(PLAN), "assets"
    if change == "login":
        plan["client_login"] = "other"
    elif change == "hash":
        plan["plan_hash"] = "b" * 64
    elif change == "operation":
        operation = "launch"
    elif change == "client":
        ctx.session.client_params.client_info = types.Implementation(name="other", version="1")
    else:
        state = json.loads(ctx.request_state)
        state["nonce"] = "forged"
        ctx.request_state = json.dumps(state)
    with pytest.raises(ValueError):
        await registry.authorize(ctx, plan, operation, grant.phrase)
    registry.validate(grant.phrase, **PLAN)


@pytest.mark.asyncio
async def test_expiry_and_restart_invalidate_abandoned_continuations(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(approval.time, "monotonic", lambda: now[0])
    registry, ctx = approval.ApprovalRegistry(ttl_seconds=10), modern_host()
    grant = registry.issue(**PLAN)
    await begin(registry, ctx, grant)
    with pytest.raises(ValueError, match="неизвестно"):
        await approval.ApprovalRegistry().authorize(ctx, PLAN, "assets", grant.phrase)
    now[0] = 111.0
    with pytest.raises(ValueError, match="истекло"):
        await registry.authorize(ctx, PLAN, "assets", grant.phrase)
    assert not registry._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("missing", ["capabilities", "identity", "name", "version"])
async def test_unknown_identity_or_capability_never_authorizes(modern, missing):
    ctx = modern_host() if modern else host()
    if missing == "capabilities":
        ctx.session.client_capabilities = None
    elif missing == "identity":
        ctx.session.client_params = None
    else:
        setattr(ctx.session.client_params.client_info, missing, "")
    registry = approval.ApprovalRegistry()
    grant = registry.issue(**PLAN)
    with pytest.raises(approval.ConsentError):
        await registry.authorize(ctx, PLAN, "assets", grant.phrase)
    ctx.elicit.assert_not_awaited()
    assert not registry._pending


@pytest.mark.asyncio
async def test_task_authorized_stays_explicit_and_needs_no_form():
    registry = approval.ApprovalRegistry()
    grant = registry.issue(**PLAN)
    result = await registry.authorize(None, PLAN, "assets", grant.phrase, mode="task_authorized")
    assert result["mechanism"] == "configured_task_authorization"
    with pytest.raises(ValueError):
        await registry.authorize(None, PLAN, "assets", grant.phrase, mode="task_authorized")


@pytest.mark.asyncio
async def test_legacy_accept_without_data_keeps_structured_error_contract():
    ctx = host()
    ctx.elicit.return_value.data = None
    registry = approval.ApprovalRegistry()
    grant = registry.issue(**PLAN)
    with pytest.raises(approval.ConsentError) as err:
        await registry.authorize(ctx, PLAN, "assets", grant.phrase)
    assert err.value.code == "mcp_elicitation_invalid_response"
    registry.validate(grant.phrase, **PLAN)
