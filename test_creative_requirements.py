"""Regression coverage for planned CTA buttons and multiple RSYA image variants."""
import asyncio
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from test_bundle import source_bundle
from test_executor import FakeApi, readback_payloads
from test_policy_audit import campaign, findings
from yadirect_mcp import ads, audit, bundle, creative, executor, landing, policy


def button_actions(result):
    return [a for a in result['required_manual_actions'] if a['rule'] == 'ads.action_button']


@pytest.mark.parametrize('hashes', [None, ['one'], ['one', 'two'], ['one'] * 3,
                                  ['one', 'two', 'one'], [' one ', 'one', 'two']])
def test_network_incomplete_images_block_before_any_api_call(hashes):
    source = source_bundle()
    ad = source['channels']['network']['groups'][0]['ads'][0]
    ad.pop('ad_image_hashes')
    if hashes is not None:
        ad['ad_image_hashes'] = hashes
    plan = bundle.compile_bundle(source, 'client')
    assert not plan['ready']
    assert any(f['rule'] == 'ads.network_image' and f['status'] == 'BLOCK'
               for f in plan['findings'])
    api = FakeApi()
    with pytest.raises(ValueError):
        asyncio.run(executor.apply(api, plan))
    assert not api.calls


@pytest.mark.parametrize('count', [3, 4, 5])
def test_all_images_reach_the_single_responsive_ad(count):
    source = source_bundle()
    hashes = [f'image-{i}' for i in range(count)]
    source['channels']['network']['groups'][0]['ads'][0]['ad_image_hashes'] = hashes
    plan = bundle.compile_bundle(source, 'client')
    api = FakeApi()
    execution = asyncio.run(executor.apply(api, plan))
    writes = [c[3]['Ads'] for c in api.calls if c[1:3] == ('ads', 'add')]
    assert writes[0][2]['ResponsiveAd']['AdImageHashes'] == hashes
    assert execution['api_creation_complete']
    assert not execution['setup_complete']


def test_button_selection_required_and_not_sent_as_an_api_field():
    source = source_bundle()
    source['channels']['search']['groups'][0]['ads'][0].pop('action_button')
    missing = bundle.compile_bundle(source, 'client')
    assert missing['ready']
    assert not any(f['rule'] == 'ads.action_button_selection' for f in missing['findings'])
    plan = bundle.compile_bundle(source_bundle(), 'client')
    actions = button_actions(plan)
    assert len(actions) == plan['summary']['ads']
    assert actions[0]['requested']['href'] == 'https://example.test/service'
    api = FakeApi()
    result = asyncio.run(executor.apply(api, plan))
    assert button_actions(result)[0]['requested'] == actions[0]['requested']
    for call in api.calls:
        if call[1:3] == ('ads', 'add'):
            assert all(set(ad) == {'ResponsiveAd', 'AdGroupId'} for ad in call[3]['Ads'])
            assert all('action_button' not in ad['ResponsiveAd'] for ad in call[3]['Ads'])


def test_button_choice_and_contacts_url_are_bound_to_plan_hash():
    source = source_bundle()
    original = bundle.compile_bundle(source, 'client')
    selected = source['channels']['network']['groups'][0]['ads'][0]['action_button']
    selected.update(destination='contacts', href='https://example.test/contact-us',
                    reason='The contact page contains the consultation form')
    changed = bundle.compile_bundle(source, 'client')
    assert changed['plan_hash'] != original['plan_hash']
    assert button_actions(changed)[2]['requested']['href'].endswith('/contact-us')
    selected['text'] = 'Contact us'
    assert bundle.compile_bundle(source, 'client')['plan_hash'] != changed['plan_hash']


@pytest.mark.parametrize('change', [
    {'text': ''}, {'reason': ''}, {'href': ''}, {'href': '/contacts'},
    {'href': 'javascript:alert(1)'}, {'href': 'https://user:password@example.test/'},
    {'destination': 'contacts'},
    {'destination': 'contacts', 'href': 'https://unrelated.test/contacts'},
    {'destination': 'main', 'href': 'https://example.test/not-the-main-link'},
    {'destination': 'unknown'}, {'unknown_field': True},
])
def test_invalid_button_rejected(change):
    value = {'text': 'Learn more', 'reason': 'Relevant page'} | change
    with pytest.raises(ValueError, match='action_button'):
        creative.action_button(value, 'https://example.test/service')


@pytest.mark.asyncio
async def test_contacts_button_url_gets_effective_tracking_and_mobile_check(monkeypatch):
    seen = []
    async def checked(pages, **kwargs):
        seen.extend(deepcopy(pages))
        return [{'url': p['url'], 'ok': not urlsplit(p['url']).path.endswith('contacts')}
                for p in pages]
    monkeypatch.setattr(landing, 'inspect_pages', checked)
    source = source_bundle()
    source['channels']['search']['groups'][0]['ads'][0]['action_button'].update(
        destination='contacts', href='https://example.test/contacts')
    plan = bundle.compile_bundle(source, 'client')
    result = await executor.preflight(FakeApi(), plan)
    assert result['status'] == 'BLOCK'
    contacts = [p for p in seen if 'button' in p['link_kinds']
                and urlsplit(p['url']).path.endswith('contacts')]
    assert {p['device'] for p in contacts} == {'desktop', 'mobile'}
    assert all('yd_campaign_name' in parse_qs(urlsplit(p['url']).query) for p in contacts)


def test_rejected_middle_ad_does_not_shift_ui_button_targets():
    plan = bundle.compile_bundle(source_bundle(), 'client')
    buttons = plan['campaigns'][1]['groups'][0]['action_buttons']
    buttons[0]['text'] = 'First'
    buttons[1]['text'] = 'Failed'
    buttons[2]['text'] = 'Third'
    execution = [{'plan_index': 1, 'channel': 'network', 'id': 111,
                  'groups': [{'plan_index': 0, 'id': 222, 'ads': [
                      {'Id': 1920444202421684653}, {'Errors': [{'Code': 1}]},
                      {'Id': 1920444202421684655}]}]}]
    actions = creative.executed_actions(plan, execution)
    buttons = [a for a in actions if a['rule'] == 'ads.action_button']
    assert [a['ad_id'] for a in buttons] == ['1920444202421684653', '1920444202421684655']
    assert [a['requested']['text'] for a in buttons] == ['First', 'Third']
    buttons[0]['requested']['text'] = 'mutated'
    assert plan['campaigns'][1]['groups'][0]['action_buttons'][0]['text'] == 'First'


@pytest.mark.parametrize('returned', [['a'], ['a', 'a', 'b'], ['a', 'b', 'wrong']])
def test_readback_detects_lost_duplicate_or_substituted_image(returned):
    source = source_bundle()
    source['channels']['network']['groups'][0]['ads'][0]['ad_image_hashes'] = ['a', 'b', 'c']
    plan = bundle.compile_bundle(source, 'client')
    execution = asyncio.run(executor.apply(FakeApi(), plan))
    settings, groups, rows, keyword_ids = readback_payloads(plan, execution)
    rows['ads'][2]['ad_image_hashes'] = returned
    result = executor.compare_readback(plan, execution, settings, groups, rows, keyword_ids)
    assert not result['verified']
    assert any(f['rule'] == 'readback.ad_images' and f['status'] == 'BLOCK'
               for f in result['findings'])


def test_api_image_count_is_unique_and_audit_does_not_trust_boolean_presence():
    shaped = ads._shape({'Type': 'RESPONSIVE_AD', 'ResponsiveAd': {
        'AdImages': {'Items': [{'ImageHash': 'a'}, {'ImageHash': 'a'}, {'ImageHash': 'b'}]}}})
    assert shaped['image'] and shaped['image_count'] == 2
    assert shaped['ad_image_hashes'] == ['a', 'b']
    item = campaign()
    item['bidding_strategy']['Search'] = {'BiddingStrategyType': 'SERVING_OFF'}
    item['bidding_strategy']['Network'] = {'BiddingStrategyType': 'WB_MAXIMUM_CLICKS'}
    row = {'id': 7, 'campaign_id': item['id'], 'type': 'RESPONSIVE_AD',
           'href': 'https://example.test/', 'image': True}
    result = audit.audit_campaign(item, selected_policy=policy.get(), ad_rows=[row])
    assert findings(result)['ads.network_image']['status'] == 'BLOCK'
    assert 'ads.action_button' not in findings(result)
    row['ad_image_hashes'] = ['a', 'b', 'c']
    result = audit.audit_campaign(item, selected_policy=policy.get(), ad_rows=[row])
    assert findings(result)['ads.network_image']['status'] == 'PASS'
    assert findings(result)['ads.action_button']['status'] == 'MANUAL'


def test_executor_rechecks_images_even_when_stale_plan_claims_ready():
    plan = bundle.compile_bundle(source_bundle(), 'client')
    plan['campaigns'][1]['groups'][0]['ads'][0]['ResponsiveAd']['AdImageHashes'] = ['only-one']
    api = FakeApi()
    with pytest.raises(ValueError):
        asyncio.run(executor.apply(api, plan))
    assert not api.calls
