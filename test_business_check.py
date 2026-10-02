import pytest

from yadirect_mcp import businesses, repair

SITE = """
<a href="tel:+74952211030">+7 495 221 10 30</a>
<p>Запись: 8 (495) 221-10-44, ИНН 7701234567890, заказ 81234567890123</p>
"""


def profile(**extra):
    row = {'Id': 204158284678, 'Name': 'Белый Город', 'IsPublished': 'YES',
           'Phone': '+7 (495) 221-10-30', 'Address': 'Москва, улица Малая Дмитровка, 16, стр. 2',
           'HasOffice': 'YES', 'Urls': ['https://whitecity-clinic.ru/']}
    row.update(extra)
    return row


def test_phones_from_tel_links_and_text_are_normalized():
    assert businesses.phones_in(SITE) == ['74952211030', '74952211044']


@pytest.mark.parametrize(('row', 'site', 'expected', 'status'), [
    (profile(), ['74952211030'], None, 'PASS'),
    (profile(), ['74952211044'], None, 'BLOCK'),
    (profile(), [], ['8 495 221-10-30'], 'PASS'),
    (profile(), [], ['+7 495 000-00-00'], 'BLOCK'),
    (profile(), [], None, 'MANUAL'),
    (profile(IsPublished='NO'), ['74952211030'], None, 'BLOCK'),
])
def test_compare_requires_published_profile_with_site_or_expected_phone(row, site, expected,
                                                                        status):
    result = businesses.compare([row['Id']], [row], site_phones=site, expected_phones=expected,
                                site_urls=['https://www.whitecity-clinic.ru/mrt'])
    assert result['status'] == status
    assert result['phone_search_supported_by_api'] is False
    assert result['profiles'][0]['checks']['site_in_profile_urls'] is True


def test_profile_not_returned_by_api_is_block_not_absence():
    result = businesses.compare([1], [], site_phones=['74952211030'])
    assert result['status'] == 'BLOCK'
    assert result['profiles'][0]['accessible'] is False


class Api:
    def __init__(self, limited=False):
        self.calls = []
        self.limited = limited

    async def call_v501(self, service, method, params, *, client_login):
        assert (service, method, client_login) == ('businesses', 'get', 'client')
        assert params['FieldNames'] == businesses.FIELDS
        self.calls.append(params['SelectionCriteria']['Ids'])
        result = {'Businesses': [profile(Id=i) for i in params['SelectionCriteria']['Ids']
                                 if i % 2]}
        if self.limited:
            result['LimitedBy'] = 1
        return result


@pytest.mark.asyncio
async def test_read_batches_and_keeps_missing_profiles_for_compare():
    api = Api()
    rows = await businesses.read(api, 'client', list(range(1, 1003)))
    assert [len(c) for c in api.calls] == [1000, 2]
    assert len(rows) == 501


@pytest.mark.asyncio
async def test_read_blocks_truncated_page():
    with pytest.raises(ValueError, match='неполный'):
        await businesses.read(Api(limited=True), 'client', [1])


@pytest.mark.parametrize('ids', [[], [0], [1, 1], ['x']])
def test_business_ids_are_validated(ids):
    with pytest.raises(ValueError):
        businesses.normalize_ids(ids)


PROFILES = [{'business_id': '204158284678', 'phone': '+7 495 221-10-30',
             'address': 'Москва, улица Малая Дмитровка, 16, стр. 2', 'has_office': True}]


def test_repair_business_id_requires_expected_profiles():
    with pytest.raises(ValueError, match='business_profiles'):
        repair.normalize({'ads': [{'id': 5, 'business_id': 204158284678}]}, 'client')
    with pytest.raises(ValueError, match='только вместе'):
        repair.normalize({'campaigns': [{'id': 1, 'add_metrica_tag': True}],
                          'business_profiles': PROFILES}, 'client')


def test_repair_business_id_is_planned_with_profiles_in_hash():
    plan = repair.normalize({'ads': [{'id': 5, 'business_id': '204158284678'}],
                             'business_profiles': PROFILES}, 'client')
    assert plan['ads'] == [{'Id': 5, 'ResponsiveAd': {'BusinessId': 204158284678}}]
    assert plan['business_profiles'][0]['phone'] == '74952211030'
    other = repair.normalize({'ads': [{'id': 5, 'business_id': '204158284678'}],
                              'business_profiles': [{**PROFILES[0], 'phone': '+7 495 221-10-44'}]},
                             'client')
    assert other['plan_hash'] != plan['plan_hash']
    plain = repair.normalize({'campaigns': [{'id': 1, 'add_metrica_tag': True}]}, 'client')
    assert 'business_profiles' not in plain


def _pages(phones):
    return [{'ad_ids': ['5'], 'link_kinds': ['main'], 'site_check': {'ok': True, 'phones': phones}},
            {'ad_ids': ['6'], 'site_check': {'ok': True, 'phones': ['74952211030']}}]


def test_preflight_business_check_requires_profile_phone_on_ad_pages():
    plan = repair.normalize({'ads': [{'id': 5, 'business_id': '204158284678'}],
                             'business_profiles': PROFILES}, 'client')
    refs = {'businesses': [profile()]}
    result = repair._business_check(plan, refs, _pages(['74952211044', '74952211030']))
    assert result['status'] == 'PASS'
    assert result['site_phone_matches'] == [
        {'ad_id': 5, 'business_id': 204158284678, 'phone': '74952211030', 'pages': 1}]
    with pytest.raises(ValueError, match='не найден на страницах объявления 5'):
        repair._business_check(plan, refs, _pages(['74952211044']))
    with pytest.raises(ValueError, match='телефон организации'):
        repair._business_check(plan, {'businesses': [profile(Phone='+7 495 000-00-00')]},
                               _pages(['74952211030']))


def test_patched_ad_and_readback_field_include_business_id():
    patched = repair._patched_ad({'id': '5', 'business_id': None}, {'BusinessId': 204158284678})
    assert repair._ad_value(patched, 'business_id') == 204158284678
