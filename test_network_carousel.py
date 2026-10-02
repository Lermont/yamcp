"""Real RSYA carousels remain pending until independently saved in the UI."""
import asyncio

import pytest

from test_bundle import source_bundle
from test_executor import FakeApi
from test_policy_audit import campaign, findings
from yadirect_mcp import audit, bundle, executor, policy


@pytest.mark.parametrize('image_count', [3, 5])
def test_image_variants_do_not_satisfy_carousel(image_count):
    source = source_bundle()
    ads = source['channels']['network']['groups'][0]['ads']
    for ad in ads:
        ad['ad_image_hashes'] = [f'hash-{i}' for i in range(image_count)]
    plan = bundle.compile_bundle(source, 'client')
    assert plan['ready'] is True  # UI actions happen after objects have IDs.
    actions = [a for a in plan['required_manual_actions'] if a['rule'] == 'network.carousel']
    assert len(actions) == len(ads)
    assert [a['ad_index'] for a in actions] == list(range(len(ads)))
    assert all(a['campaign_index'] == 1 and a['group_index'] == 0 for a in actions)
    assert all(a['status'] == 'pending_ui' for a in actions)
    assert all(a['requirements']['ordinary_ad_image_hashes_satisfy_requirement'] is False
               for a in actions)
    payloads = plan['campaigns'][1]['groups'][0]['ads']
    assert all(len(a['ResponsiveAd']['AdImageHashes']) == image_count for a in payloads)
    assert all('Carousel' not in a['ResponsiveAd'] for a in payloads)


def test_search_has_no_carousel_action():
    source = source_bundle()
    del source['channels']['network']
    plan = bundle.compile_bundle(source, 'client')
    assert all(a['rule'] != 'network.carousel' for a in plan['required_manual_actions'])
    result = asyncio.run(executor.apply(FakeApi(), plan))
    assert all(a['rule'] != 'network.carousel' for a in result['required_manual_actions'])
    assert 'network.carousel' not in findings(
        audit.audit_campaign(campaign(), selected_policy=policy.get())
    )


def test_executor_tracks_each_created_network_ad_without_rounding_ids():
    api = FakeApi()
    api.next_id = 1920444202421684653
    result = asyncio.run(executor.apply(api, bundle.compile_bundle(source_bundle(), 'client')))
    assert result['status'] == 'complete'
    assert result['api_creation_complete'] is True
    assert result['activated'] is False
    network = result['campaigns'][1]
    expected = {str(ad['Id']) for group in network['groups'] for ad in group['ads']}
    actions = [a for a in result['required_manual_actions'] if a['rule'] == 'network.carousel']
    assert {a['ad_id'] for a in actions} == expected
    assert all(a['campaign_id'] == network['id'] for a in actions)
    assert all(a['group_id'] == network['groups'][0]['id'] for a in actions)
    assert all(a['status'] == 'pending_ui' for a in actions)
    assert 'не завершена' in result['message']


def test_api_failure_does_not_invent_created_carousel_targets():
    result = asyncio.run(executor.apply(
        FakeApi(fail_service='ads'), bundle.compile_bundle(source_bundle(), 'client')
    ))
    assert result['api_creation_complete'] is False
    assert [a['rule'] for a in result['required_manual_actions']] == ['launch.moderation']


@pytest.mark.parametrize('mixed', [False, True])
def test_api_audit_keeps_carousel_manual_even_with_multiple_images(mixed):
    item = campaign()
    if not mixed:
        item['bidding_strategy']['Search'] = {'BiddingStrategyType': 'SERVING_OFF'}
    item['bidding_strategy']['Network'] = {
        'BiddingStrategyType': 'WB_MAXIMUM_CLICKS',
        'WbMaximumClicks': {'WeeklySpendLimit': 3_000_000_000},
    }
    result = audit.audit_campaign(item, selected_policy=policy.get(), ad_rows=[
        {'id': 1920444202421684653, 'campaign_id': 10, 'image': True,
         'ad_image_hashes': ['a', 'b', 'c']},
        {'id': 45, 'campaign_id': 99, 'image': True},
    ])
    check = findings(result)['network.carousel']
    assert check['status'] == policy.MANUAL
    assert check['evidence']['ad_ids'] == ['1920444202421684653']
    assert check['evidence']['api_supported'] is False


def test_carousel_requirements_are_independent_copies():
    action = policy.network_carousel_requirement(ad_id='1')
    action['requirements']['minimum_images'] = 99
    assert policy.network_carousel_requirement()['requirements']['minimum_images'] == 2
