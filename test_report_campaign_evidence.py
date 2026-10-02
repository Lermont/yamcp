"""Regression: traffic reports may verify campaigns through Reports, never infer goals."""
from copy import deepcopy

import pytest

from test_report_pipeline import API, HEADER, register
from yadirect_mcp import report_pipeline as pipeline

TRAFFIC = HEADER.replace('\tConversions_77_AUTO', '') + '2026-08-01\t1\tSEARCH\t100\t10\t120.25\n'
IDENTITY = 'CampaignId\tCampaignName\tCampaignType\tImpressions\tClicks\tCost\n'
ROW = '1\tCampaign wizard\tTEXT_CAMPAIGN\t100\t10\t120.25\n'


class MissingSettingsAPI(API):
    def __init__(self, identity=IDENTITY + ROW, *, payload=None, daily=TRAFFIC):
        super().__init__(daily)
        self.identity = identity
        self.payload = {'Campaigns': []} if payload is None else payload

    async def call_v501(self, service, method, params, **kwargs):
        assert service == 'campaigns' and method == 'get'
        assert kwargs['client_login'] == 'client'
        self.calls.append((service, method, deepcopy(params)))
        return self.payload

    async def report(self, spec, **kwargs):
        assert kwargs['client_login'] == 'client'
        self.calls.append(('reports', deepcopy(spec)))
        if 'CampaignName' in spec['FieldNames']:
            if isinstance(self.identity, Exception):
                raise self.identity
            return self.identity
        return self.tsv


@pytest.fixture(autouse=True)
def isolated_slices(monkeypatch):
    async def collect(*args):
        return {'schema': 'client_report_breakdowns_v1', 'start': '2026-08-01',
                'end': '2026-08-01', 'collectedAt': '2026-08-02T00:00:00Z',
                'slices': {key: {'status': 'collected', 'scope': 'all', 'rows': [],
                                 'reconciliation': []}
                           for key in pipeline.report_breakdowns.SLICES}}
    monkeypatch.setattr(pipeline.report_breakdowns, 'collect', collect)


@pytest.mark.asyncio
async def test_reports_only_campaign_keeps_full_traffic_and_evidence(tmp_path):
    cfg, _, old, _ = register(tmp_path, measurement=False)
    api = MissingSettingsAPI()
    result = await pipeline.refresh(cfg, api, 'client', as_of='2026-08-02')
    assert result['brief']['totals']['spend'] == 120.25
    assert result['brief']['totals']['clicks'] == 10
    assert result['brief']['totals']['conversions'] is None
    state = pipeline._load(tmp_path / 'client')
    context = state['last_refresh']['context']
    assert context['unavailable_campaign_settings_ids'] == [1]
    assert context['reports_campaign_evidence']['raw_tsv'] == IDENTITY + ROW
    assert context['statistics_time_zone'] == 'Europe/Moscow'
    assert state['model']['setup'] == old['setup']
    assert 'Мы пока не смогли проверить' in result['brief']['limitations']
    assert 'нельзя оценить обращения и продажи' in result['brief']['limitations']
    assert 'выгрузк' not in result['brief']['limitations']
    spec = api.calls[1][1]
    assert spec['ReportType'] == 'CAMPAIGN_PERFORMANCE_REPORT'
    assert spec['SelectionCriteria'] == {'DateFrom': '2026-08-01', 'DateTo': '2026-08-01',
                                       'Filter': [{'Field': 'CampaignId', 'Operator': 'IN',
                                                   'Values': ['1']}]}
    assert spec['IncludeVAT'] == 'YES' and 'Page' not in spec and 'Goals' not in spec
    later = MissingSettingsAPI(RuntimeError('must not request cached data'))
    assert (await pipeline.refresh(cfg, later, 'client', as_of='2026-08-02'))['status'] == 'cached'
    assert later.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [
    IDENTITY, '', IDENTITY + ROW + ROW, IDENTITY + ROW.replace('1\t', '2\t', 1),
    IDENTITY + ROW.replace('120.25', 'NaN'), IDENTITY + ROW.replace('100\t10', '100\t--'),
    IDENTITY + ROW.replace('100\t10', '100\t11'),
    IDENTITY + ROW.replace('120.25', '140.25'), RuntimeError('Reports unavailable'),
])
async def test_missing_or_invalid_identity_never_commits_or_becomes_zero(tmp_path, identity):
    cfg, initial, _, _ = register(tmp_path, measurement=False)
    before = (tmp_path / 'client/index.html').read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        await pipeline.refresh(cfg, MissingSettingsAPI(identity), 'client', as_of='2026-08-02')
    assert pipeline._load(tmp_path / 'client')['revision'] == initial['revision']
    assert (tmp_path / 'client/index.html').read_bytes() == before
    assert not (tmp_path / 'client/.client-report/refresh.lock').exists()


@pytest.mark.asyncio
async def test_missing_settings_never_falls_back_for_conversions(tmp_path):
    cfg, initial, _, _ = register(tmp_path, measurement=True)
    api = MissingSettingsAPI()
    with pytest.raises(ValueError, match='настройки'):
        await pipeline.refresh(cfg, api, 'client', as_of='2026-08-02')
    assert all(call[0] != 'reports' for call in api.calls)
    assert pipeline._load(tmp_path / 'client')['revision'] == initial['revision']


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'Campaigns': [], 'LimitedBy': 1},
    {'Campaigns': [{'Id': 1}, {'Id': 1}]},
    {'Campaigns': [{'Id': 2}]},
])
async def test_incomplete_or_duplicate_settings_are_not_a_fallback(tmp_path, payload):
    cfg, initial, _, _ = register(tmp_path, measurement=False)
    api = MissingSettingsAPI(payload=payload)
    with pytest.raises(ValueError):
        await pipeline.refresh(cfg, api, 'client', as_of='2026-08-02')
    assert len(api.calls) == 1
    assert pipeline._load(tmp_path / 'client')['revision'] == initial['revision']


@pytest.mark.asyncio
@pytest.mark.parametrize('measurement', [False, True])
async def test_targeting_timezone_does_not_change_reports_moscow_dates(tmp_path, measurement):
    cfg, _, _, _ = register(tmp_path, measurement=measurement)
    class OtherTimezoneAPI(API):
        async def call_v501(self, *args, **kwargs):
            result = await super().call_v501(*args, **kwargs)
            result['Campaigns'][0]['TimeZone'] = 'Asia/Yekaterinburg'
            return result
    api = OtherTimezoneAPI() if measurement else OtherTimezoneAPI(TRAFFIC)
    result = await pipeline.refresh(cfg, api, 'client', as_of='2026-08-02')
    assert result['brief']['start'] == result['brief']['end'] == '2026-08-01'
    context = pipeline._load(tmp_path / 'client')['last_refresh']['context']
    assert context['statistics_time_zone'] == 'Europe/Moscow'
    assert context['campaign_settings'][0]['time_zone'] == 'Asia/Yekaterinburg'
