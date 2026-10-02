"""Offline coverage for reviewer receipts after narrow image repairs."""
import hashlib
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from test_manual_review import commit
from test_operations_stage3 import fixture
from yadirect_mcp import creative, jobs, manual_review, repair, verification, workflow

AD = '1921019359743476961'


def inputs():
    plan = repair.normalize({'ads': [{'id': AD, 'ad_image_hashes': ['new-a', 'new-b']}]}, 'client')
    before = {'ads': [{'id': AD, 'campaign_id': '123', 'ad_group_id': '456'}],
              'campaigns': [{'id': '123', 'bidding_strategy': {
                  'Network': {'BiddingStrategyType': 'WB_MAXIMUM_CONVERSION_RATE'}}}]}
    return plan, before


def test_exact_targets_only_for_network_image_changes():
    plan, before = inputs()
    actions = creative.repaired_carousel_actions(plan, before)
    assert len(actions) == 1
    assert {k: actions[0][k] for k in ('ad_id', 'group_id', 'campaign_id')} == {
        'ad_id': AD, 'group_id': '456', 'campaign_id': '123'}
    assert actions[0]['rule'] == 'network.carousel'
    search = deepcopy(before)
    search['campaigns'][0]['bidding_strategy']['Network']['BiddingStrategyType'] = 'SERVING_OFF'
    assert creative.repaired_carousel_actions(plan, search) == []
    plan['ads'][0]['ResponsiveAd'] = {'Titles': ['text']}
    assert creative.repaired_carousel_actions(plan, before) == []


@pytest.mark.parametrize('missing', ['ads', 'campaigns'])
def test_missing_ownership_or_placement_is_not_a_pass(missing):
    plan, before = inputs()
    before[missing] = []
    with pytest.raises(ValueError):
        creative.repaired_carousel_actions(plan, before)


@pytest.mark.asyncio
async def test_verified_repair_requires_receipt_without_mutating_source(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    settings, job_id, path = fixture(tmp_path)
    plan, before = inputs()
    data = json.loads(path.read_text('utf-8'))
    encoded = json.dumps(plan)
    data.update(kind='repair', plan_hash=plan['plan_hash'], verification_plan_json=encoded,
                verification_plan_sha256=hashlib.sha256(encoded.encode()).hexdigest())
    data['result'] = {'status': 'complete', 'executed': True, 'activated': False,
                      'preflight': {'before': before}, 'updates': {'ads': [{'Id': AD}]}}
    jobs._atomic(path, data)
    original = path.read_bytes()
    monkeypatch.setattr(repair, 'readback', AsyncMock(return_value={'verified': True}))
    checked = await verification.run(AsyncMock(), settings.out_dir, 'client', job_id)
    assert checked['status'] == 'verified'
    assert checked['workflow']['ui_verification'] == 'pending'
    action = checked['required_manual_actions'][0]
    evidence = tmp_path / 'ui-evidence' / 'carousel.json'
    evidence.parent.mkdir()
    evidence.write_text('{"saved":true}', 'utf-8')
    review = {'outcome': 'verified', 'reviewer': 'Test', 'saved_and_reopened': True,
              'checked_at': datetime.now(UTC).isoformat(),
              'checked_fields': {'slides': ['new-a', 'new-b'],
                                 'order_checked': True, 'images_loaded': True},
              'evidence_paths': [str(evidence)]}
    await commit(settings, job_id, workflow.action_id(job_id, action), review)
    result = manual_review.current(settings.out_dir, jobs.read(settings.out_dir, job_id, 'client'))
    assert result['workflow']['setup'] == 'complete'
    assert result['workflow']['ui_verification'] == 'verified_by_reviewer'
    assert path.read_bytes() == original
