"""Resume safety; external API calls are replaced by local fakes."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from yadirect_mcp import ad_resume, approval


def bundle():
    return {"launch_authorized": True, "allow_pending_moderation": True,
            "targets": [{"ad_id": "9007199254740997", "group_id": "2", "campaign_id": "1"}]}


@pytest.fixture
def fake(monkeypatch):
    state = {"campaigns": [{"id": 1, "state": "ON", "status": "ACCEPTED", "budget": 10}],
             "ads": [{"id": 9007199254740997, "campaign_id": 1, "ad_group_id": 2,
                      "state": "SUSPENDED", "status": "MODERATION", "text": "kept"},
                     {"id": 3, "campaign_id": 1, "ad_group_id": 4,
                      "state": "ON", "status": "ACCEPTED", "text": "old"}]}
    async def campaigns(*args, **kwargs):
        return {"campaigns": deepcopy(state["campaigns"])}
    async def ads(*args, **kwargs):
        return {"ads": deepcopy(state["ads"]), "truncated": state.get("truncated", False)}
    calls = []
    async def call(service, method, params, **kwargs):
        calls.append((service, method, params))
        assert (service, method) == ("ads", "resume")
        state["ads"][0]["state"] = "ON"
        return state.get("response", {"ResumeResults": [{"Id": 9007199254740997}]})
    monkeypatch.setattr(ad_resume.campaigns, "read_settings", campaigns)
    monkeypatch.setattr(ad_resume.ads, "read", ads)
    journal = SimpleNamespace(payload={"uncertain": False}, checkpoint=lambda x: None,
                              event=lambda **kwargs: None)
    return state, calls, SimpleNamespace(call_v501=call), journal


@pytest.mark.parametrize("change", [
    {"launch_authorized": False}, {"allow_pending_moderation": "true"},
    {"resume_all": True}, {"targets": []}, {"targets": bundle()["targets"] * 2},
    {"targets": [{"ad_id": 1.5, "campaign_id": 1, "group_id": 2}]},
])
def test_invalid_plan(change):
    with pytest.raises(ValueError):
        ad_resume.normalize({**bundle(), **change}, "login")


@pytest.mark.asyncio
async def test_resume_exact_ids_and_preserve_others(fake):
    state, calls, api, journal = fake
    plan = ad_resume.normalize(bundle(), "login")
    before = await ad_resume.preflight(api, plan)
    result = await ad_resume.apply(api, plan, before, journal)
    assert result["status"] == "complete" and result["readback"]["verified"]
    assert calls == [("ads", "resume", {"SelectionCriteria": {"Ids": [9007199254740997]}})]
    assert state["ads"][1] == before["ads"][0]


@pytest.mark.parametrize("status", ["DRAFT", "REJECTED", "UNKNOWN"])
@pytest.mark.asyncio
async def test_unavailable_blocks(fake, status):
    state, calls, api, _ = fake
    state["ads"][0]["status"] = status
    with pytest.raises(ValueError):
        await ad_resume.preflight(api, ad_resume.normalize(bundle(), "login"))
    assert not calls


@pytest.mark.parametrize("change", ["foreign", "truncated", "campaign_off", "duplicate"])
@pytest.mark.asyncio
async def test_invalid_snapshot_blocks(fake, change):
    state, calls, api, _ = fake
    if change == "foreign":
        state["ads"][0]["ad_group_id"] = 999
    elif change == "truncated":
        state["truncated"] = True
    elif change == "campaign_off":
        state["campaigns"][0]["state"] = "OFF"
    else:
        state["ads"].append(deepcopy(state["ads"][0]))
    with pytest.raises(ValueError):
        await ad_resume.preflight(api, ad_resume.normalize(bundle(), "login"))
    assert not calls


@pytest.mark.asyncio
async def test_drift_after_preview_blocks(fake):
    state, calls, api, journal = fake
    plan = ad_resume.normalize(bundle(), "login")
    before = await ad_resume.preflight(api, plan)
    state["campaigns"][0]["budget"] = 11
    with pytest.raises(ValueError, match="изменился"):
        await ad_resume.apply(api, plan, before, journal)
    assert not calls


@pytest.mark.parametrize("response", [{}, {"ResumeResults": []}, {"ResumeResults": 1},
    {"ResumeResults": [{"Id": 100}]},
    {"ResumeResults": [{"Errors": [{"Code": 1}]}]}])
@pytest.mark.asyncio
async def test_partial_response_never_retries(fake, response):
    state, calls, api, journal = fake
    plan = ad_resume.normalize(bundle(), "login")
    before = await ad_resume.preflight(api, plan)
    state["response"] = response
    result = await ad_resume.apply(api, plan, before, journal)
    assert result["status"] == "partial" and len(calls) == 1


@pytest.mark.asyncio
async def test_pending_requires_flag(fake):
    _, _, api, _ = fake
    with pytest.raises(ValueError):
        await ad_resume.preflight(api, ad_resume.normalize(
            {**bundle(), "allow_pending_moderation": False}, "login"))


def test_bad_readback_not_success(fake):
    state, _, _, _ = fake
    after = deepcopy(state)
    after["ads"][0]["state"] = "ON"
    after["ads"][1]["text"] = "changed"
    assert not ad_resume.verify(state, after, {9007199254740997})["verified"]


@pytest.mark.asyncio
async def test_token_bound_and_one_use():
    plan = ad_resume.normalize(bundle(), "login")
    registry = approval.ApprovalRegistry()
    grant = registry.issue("login", plan["plan_hash"])
    for login, sha in [("other", plan["plan_hash"]), ("login", "other")]:
        with pytest.raises(ValueError):
            registry.validate(grant.phrase, login, sha)
    await registry.authorize(None, plan, "resume", grant.phrase, mode="task_authorized")
    with pytest.raises(ValueError):
        registry.validate(grant.phrase, "login", plan["plan_hash"])


def test_registration_is_setup_only(tmp_path):
    from test_server import _tool_names
    assert "direct_ad_resume" not in _tool_names("report", str(tmp_path))
    assert "direct_ad_resume" in _tool_names("campaign_setup", str(tmp_path))
