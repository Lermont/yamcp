"""Readback accepts only documented/observed normalization, not arbitrary drift."""

from copy import deepcopy

import pytest

from test_executor import FakeApi, compiled, readback_payloads
from yadirect_mcp import executor, phrases


def test_negatives_keep_explicit_forms_and_unexpected_tokens():
    assert phrases.equivalent(["!all !the mods", "друг", "бесплатно"],
                              ["all the mods", "друг", "друзья", "бесплатно", "бесплатный"])
    assert not phrases.equivalent(["!друг"], ["!друзья"])
    assert not phrases.equivalent(["друг", "покупка"], ["друзья"])
    assert not phrases.equivalent(["mod"], ["mods"])


@pytest.mark.asyncio
async def test_api_deduplicated_keyword_accepts_only_same_group_returned_id():
    plan = compiled()
    execution = await executor.apply(FakeApi(), plan)
    first = plan['campaigns'][0]['groups'][0]
    saved = execution['campaigns'][0]['groups'][0]
    first['keywords'].append({"Keyword": "another requested variant"})
    saved['keywords'].append(deepcopy(saved['keywords'][0]))
    settings, groups, ads, keyword_ids = readback_payloads(plan, execution)
    # The fake produces readback from the last variant, matching one of the
    # two exact phrases Direct mapped to the same ID in the same group.
    identifier = int(saved['keywords'][0]['Id'])
    rows = [{"id": identifier, "keyword": first['keywords'][0]['Keyword']}]
    result = executor.compare_readback(plan, execution, settings, groups, ads, keyword_ids,
                                       keyword_rows=rows)
    checks = [x for x in result['findings'] if x['rule'] == 'readback.keyword_content'
              and x.get('evidence', {}).get('keyword_id') == identifier]
    assert len(checks) == 2 and all(x['status'] == 'PASS' for x in checks)
    rows[0]['keyword'] = 'unrequested drift'
    result = executor.compare_readback(plan, execution, settings, groups, ads, keyword_ids,
                                       keyword_rows=rows)
    checks = [x for x in result['findings'] if x['rule'] == 'readback.keyword_content'
              and x.get('evidence', {}).get('keyword_id') == identifier]
    assert len(checks) == 2 and all(x['status'] == 'BLOCK' for x in checks)
