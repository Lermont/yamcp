"""Campaign identity for traffic reports when settings are unavailable.

Reports and Campaigns expose different inventories. A positive, complete Reports
response can confirm traffic ownership, but cannot verify goals or settings.
"""
from __future__ import annotations

import asyncio
import hashlib
import math

from . import campaigns, reports, store

FIELDS = ['CampaignId', 'CampaignName', 'CampaignType', 'Impressions', 'Clicks', 'Cost']
SETTINGS_NOTE = (
    'Показы, клики и расходы включены в отчёт. Мы пока не смогли проверить '
    'текущие настройки части кампаний и их цели. Поэтому по этим данным '
    'нельзя оценить обращения и продажи.'
)


def _id(value) -> int:
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError('Некорректный ID кампании в подтверждении отчёта')
    return int(value)


def _metric(raw: str, *, integer: bool = False) -> float | int:
    if raw in store.EMPTY:
        raise ValueError('Метрика подтверждения кампании неизвестна')
    try:
        value = float(raw.replace(',', '.'))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError('Некорректная метрика подтверждения кампании') from exc
    if not math.isfinite(value) or value < 0 or (integer and not value.is_integer()):
        raise ValueError('Некорректная метрика подтверждения кампании')
    return int(value) if integer else value


def parse_identity(tsv: str, expected: set[int]) -> list[dict]:
    columns, rows = store.parse_tsv(tsv)
    if len(columns) != len(set(columns)) or set(columns) != set(FIELDS):
        raise ValueError('Колонки подтверждения кампаний не совпадают с запросом')
    found = {}
    for row in rows:
        cid = _id(row['CampaignId'])
        if cid not in expected or cid in found:
            raise ValueError('Чужая или повторная кампания в подтверждении статистики')
        if not row['CampaignName'].strip() or row['CampaignType'] in store.EMPTY:
            raise ValueError('Неполное подтверждение названия/типа кампании')
        found[cid] = {
            'id': cid, 'name': row['CampaignName'], 'type': row['CampaignType'],
            'impressions': _metric(row['Impressions'], integer=True),
            'clicks': _metric(row['Clicks'], integer=True), 'spend': _metric(row['Cost']),
        }
    if set(found) != expected:
        absent = ', '.join(map(str, sorted(expected - set(found))))
        raise ValueError(
            'Кампании не подтверждены ни настройками, ни статистикой за период: ' + absent
        )
    return [found[cid] for cid in sorted(found)]


async def traffic_context(api, config: dict, end: str) -> dict:
    if config['measurement'] is not None:
        raise ValueError('Подтверждение через статистику разрешено только отчёту по трафику')
    ids = {c['id'] for c in config['campaigns']}
    login = config['client_login']
    payload = await campaigns.read_settings(
        api, login, campaign_ids=sorted(ids), include_archived=True, limit=1000,
    )
    snapshots = payload['campaigns']
    returned = [_id(row['id']) for row in snapshots]
    if (payload.get('truncated') or len(returned) != len(set(returned))
            or not set(returned) <= ids):
        raise ValueError('Неполная, повторная или посторонняя выборка настроек кампаний')
    missing = ids - set(returned)
    context = {'resolution': 'traffic_only', 'campaign_settings': snapshots,
               'unavailable_campaign_settings_ids': sorted(missing)}
    if not missing:
        return context
    filters = [{'Field': 'CampaignId', 'Operator': 'IN', 'Values': list(map(str, sorted(missing)))}]
    contract = reports.normalize(fields=FIELDS, report_type='CAMPAIGN_PERFORMANCE_REPORT',
                                 filters=filters, order_by=None, limit=None,
                                 goals=None, attribution_models=None)
    spec = {'SelectionCriteria': {'DateFrom': config['start_date'], 'DateTo': end,
                                  'Filter': contract['filters']},
            'FieldNames': contract['fields'], 'ReportType': 'CAMPAIGN_PERFORMANCE_REPORT',
            'DateRangeType': 'CUSTOM_DATE', 'Format': 'TSV', 'IncludeVAT': 'YES'}
    await asyncio.sleep(0.55)
    tsv = await api.report(spec, client_login=login)
    context.update(
        resolution='traffic_only_with_reports_identity', warnings=[SETTINGS_NOTE],
        reports_campaign_evidence={
            'client_login': login, 'request': spec, 'raw_tsv': tsv,
            'tsv_sha256': hashlib.sha256(tsv.encode('utf-8')).hexdigest(),
            'campaigns': parse_identity(tsv, missing),
        },
    )
    return context


def reconcile(context: dict, rows: list[dict], config: dict) -> None:
    """The extra identity report must agree with the complete daily window."""
    evidence = context.get('reports_campaign_evidence')
    if evidence is None:
        return
    for control in evidence['campaigns']:
        report_ids = {c['report_id'] for c in config['campaigns'] if c['id'] == control['id']}
        selected = [r for r in rows if r['campaign'] in report_ids]
        for metric in ('impressions', 'clicks', 'spend'):
            if not selected or any(r[metric] is None for r in selected):
                raise ValueError('Неполная дневная статистика подтверждённой кампании')
            value = sum(r[metric] for r in selected)
            tolerance = len(selected) * 0.005 + 0.01 if metric == 'spend' else 0
            if not math.isclose(value, control[metric], abs_tol=tolerance, rel_tol=0):
                raise ValueError('Дневная статистика не совпадает с подтверждением кампании')
    evidence['daily_totals_reconciled'] = True
