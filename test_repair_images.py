"""Image repair contract, exact IDs, pre-write checks and independent readback."""
import asyncio
from copy import deepcopy

import pytest

from test_repair import FakeApi, source
from yadirect_mcp import repair


def image_source():
    value = source()
    value['ads'][0]['id'] = '1920886687133350889'
    value['ads'][0]['ad_image_hashes'] = ['old-image', 'new-one', 'new-two']
    return value


def test_image_update_contract_and_exact_id():
    ad = repair.normalize(image_source(), 'client')['ads'][0]
    assert ad['Id'] == 1920886687133350889
    assert ad['ResponsiveAd']['AdImageHashes'] == {
        'Items': ['old-image', 'new-one', 'new-two'],
    }
    assert 'AdImageHashes' not in repair.normalize(source(), 'client')['ads'][0]['ResponsiveAd']


@pytest.mark.parametrize('hashes', [[], ['x', 'x'], ['x', ' '], None, 'abc',
                                   ['a', 'b', 'c', 'd', 'e', 'f']])
def test_invalid_image_sets(hashes):
    value = image_source()
    value['ads'][0]['ad_image_hashes'] = hashes
    with pytest.raises(ValueError, match='ad_image_hashes'):
        repair.normalize(value, 'client')


def test_missing_image_fails_before_all_writes(monkeypatch):
    async def read(*args, **kwargs):
        value = image_source()['ads'][0]
        return {'ads': [{**value, 'id': int(value['id']), 'campaign_id': 10, 'ad_group_id': 11,
                         'type': 'RESPONSIVE_AD', 'subtype': 'NONE', 'state': 'OFF',
                         'ad_image_hashes': ['old-image']}]}

    class Api(FakeApi):
        async def call_v501(self, service, method, params, *, client_login=None):
            if service == 'adimages':
                return {'AdImages': [{'AdImageHash': 'old-image'}]}
            return await super().call_v501(service, method, params, client_login=client_login)

    monkeypatch.setattr(repair.ads, 'read', read)
    api = Api()
    with pytest.raises(ValueError, match="adimages"):
        asyncio.run(repair.apply(api, repair.normalize(image_source(), 'client')))
    assert api.calls == []


def test_readback_rejects_missing_hash_even_when_ad_has_an_image(monkeypatch):
    value = image_source()
    value.pop('campaigns')
    value.pop('autotargetings')
    row = value['ads'][0]
    actual = {**deepcopy(row), 'id': int(row['id']), 'extensions': True,
              'ad_image_hashes': ['old-image']}

    async def read(*args, **kwargs):
        return {'ads': [actual]}

    monkeypatch.setattr(repair.ads, 'read', read)
    plan = repair.normalize(value, 'client')
    result = asyncio.run(repair.readback(FakeApi(), plan))
    assert not result['verified']
    actual['ad_image_hashes'] = list(reversed(row['ad_image_hashes']))
    assert asyncio.run(repair.readback(FakeApi(), plan))['verified']
