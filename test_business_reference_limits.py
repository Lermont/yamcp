import pytest

from yadirect_mcp import preflight_refs


class BusinessApi:
    def __init__(self, *, limited=False, missing=False):
        self.calls = []
        self.limited = limited
        self.missing = missing

    async def call_v501(self, service, method, params, *, client_login):
        assert (service, method, client_login) == ('businesses', 'get', 'client')
        assert params['Page']['Limit'] <= 1000
        ids = params['SelectionCriteria']['Ids']
        assert len(ids) <= 1000
        self.calls.append(ids)
        rows = [{'Id': i, 'Name': str(i), 'IsPublished': 'YES'} for i in ids]
        result = {'Businesses': rows[:-1] if self.missing else rows}
        if self.limited:
            result['LimitedBy'] = 1000
        return result


@pytest.mark.asyncio
async def test_business_references_respect_page_limit_and_read_all_batches():
    api = BusinessApi()
    ids = range(1, 1002)
    result = await preflight_refs.assets(api, 'client', [{'BusinessId': i} for i in ids])
    assert [len(c) for c in api.calls] == [1000, 1]
    assert {b['Id'] for b in result['businesses']} == set(ids)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['limited', 'missing'])
async def test_business_references_still_block_incomplete_results(failure):
    api = BusinessApi(**{failure: True})
    with pytest.raises(ValueError, match='businesses'):
        await preflight_refs.assets(api, 'client', [{'BusinessId': 1}])
