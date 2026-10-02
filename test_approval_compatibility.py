"""Approval compatibility and concurrency checks; no Direct transport is used."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import types

from yadirect_mcp import approval

PLAN = {"client_login": "client", "plan_hash": "a" * 64}


def host(action="accept", approved=True, *, capabilities=None):
    return SimpleNamespace(
        elicit=AsyncMock(return_value=SimpleNamespace(
            action=action, data=SimpleNamespace(approve=approved)
        )),
        session=SimpleNamespace(client_params=SimpleNamespace(
            clientInfo=types.Implementation(name="test-host", version="1"),
            capabilities=capabilities,
        )),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action,approved,code", [
    ("cancel", True, "mcp_elicitation_cancel"),
    ("decline", True, "mcp_elicitation_decline"),
    ("accept", False, "mcp_elicitation_not_approved"),
    ("accept", None, "mcp_elicitation_not_approved"),
])
async def test_host_rejection_preserves_token_but_requires_fresh_consent(action, approved, code):
    registry = approval.ApprovalRegistry()
    grant = registry.issue(PLAN["client_login"], PLAN["plan_hash"])
    client = host(action, approved)
    with pytest.raises(approval.ConsentError) as err:
        await registry.authorize(client, PLAN, "assets", grant.phrase)
    assert err.value.code == code
    assert err.value.as_dict()["executed"] is False
    assert err.value.client["name"] == "test-host"
    registry.validate(grant.phrase, PLAN["client_login"], PLAN["plan_hash"])

    client.elicit.return_value = SimpleNamespace(
        action="accept", data=SimpleNamespace(approve=True)
    )
    receipt = await registry.authorize(client, PLAN, "assets", grant.phrase)
    assert receipt["decision"] == "accept"
    assert client.elicit.await_count == 2
    with pytest.raises(ValueError, match="использовано"):
        await registry.authorize(client, PLAN, "assets", grant.phrase)
    assert client.elicit.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,allowed", [
    (None, False),
    (types.ElicitationCapability(url=types.UrlElicitationCapability()), False),
    (types.ElicitationCapability(), True),
    (types.ElicitationCapability(form=types.FormElicitationCapability()), True),
])
async def test_form_capabilities_include_legacy_but_not_url_only(capability, allowed):
    client = host(capabilities=types.ClientCapabilities(elicitation=capability))
    if allowed:
        assert (await approval.elicit(client, PLAN, "assets"))["decision"] == "accept"
    else:
        with pytest.raises(approval.ConsentError, match="не поддерживает"):
            await approval.elicit(client, PLAN, "assets")
        client.elicit.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_is_rechecked_after_slow_human_response(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(approval.time, "monotonic", lambda: now[0])
    registry = approval.ApprovalRegistry(ttl_seconds=10)
    grant = registry.issue(PLAN["client_login"], PLAN["plan_hash"])
    client = host()

    async def answer(**kwargs):
        now[0] = 111.0
        return SimpleNamespace(action="accept", data=SimpleNamespace(approve=True))

    client.elicit.side_effect = answer
    with pytest.raises(ValueError, match="истекло"):
        await registry.authorize(client, PLAN, "assets", grant.phrase)


@pytest.mark.asyncio
async def test_same_token_cannot_open_two_concurrent_prompts():
    registry = approval.ApprovalRegistry()
    grant = registry.issue(PLAN["client_login"], PLAN["plan_hash"])
    started, release = asyncio.Event(), asyncio.Event()
    client = host()

    async def answer(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(action="accept", data=SimpleNamespace(approve=True))

    client.elicit.side_effect = answer
    task = asyncio.create_task(registry.authorize(client, PLAN, "assets", grant.phrase))
    await started.wait()
    try:
        with pytest.raises(ValueError, match="уже открыт"):
            await registry.authorize(client, PLAN, "assets", grant.phrase)
    finally:
        release.set()
    assert (await task)["decision"] == "accept"
    assert client.elicit.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {**PLAN, "client_login": "other"}, {**PLAN, "plan_hash": "b" * 64},
])
async def test_mismatched_login_or_hash_never_opens_prompt(changed):
    registry = approval.ApprovalRegistry()
    grant = registry.issue(PLAN["client_login"], PLAN["plan_hash"])
    client = host()
    with pytest.raises(ValueError, match="другому"):
        await registry.authorize(client, changed, "assets", grant.phrase)
    client.elicit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_task_releases_reservation_without_authorizing():
    registry = approval.ApprovalRegistry()
    grant = registry.issue(PLAN["client_login"], PLAN["plan_hash"])
    client = host()
    client.elicit.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await registry.authorize(client, PLAN, "assets", grant.phrase)
    assert not registry._pending
    registry.validate(grant.phrase, PLAN["client_login"], PLAN["plan_hash"])
