"""Offline regressions: HTTP 200 is not a ready landing page."""
import httpx
import pytest

from test_executor import FakeApi, compiled
from test_policy_audit import campaign
from test_server import _in_server
from yadirect_mcp import audit, executor, knowledge, landing, landing_content, policy, regions


@pytest.mark.parametrize('html', [
    '<main><h1>Извините, раздел<br>находится на реконструкции</h1></main>',
    '<main><header><h1>Извините, раздел<br><span>находится на реконструкции</span>'
    '</h1></header><footer>Контакты и сведения компании</footer></main>',
    '<title>Каталог</title><main><p>Раздел находится <b>в разработке</b>.</p></main>',
    '<main><p>Сайт на техническом обслуживании</p></main>',
    '<main><h1>This website is under construction</h1></main>',
    '<main><h1>Coming soon</h1></main>',
])
def test_known_placeholder_is_blocked_despite_normal_title(html):
    result = landing_content.inspect(html)
    assert result['status'] == 'BLOCK'
    assert result['placeholder']
    assert result['evidence']


@pytest.mark.parametrize('hidden', [
    '<script>Раздел находится на реконструкции</script>',
    '<style>/* Раздел находится на реконструкции */</style>',
    '<!-- Раздел находится на реконструкции -->',
    '<template><h1>Раздел находится на реконструкции</h1></template>',
    '<section hidden><b>Раздел находится на реконструкции</b></section>',
    '<div aria-hidden="true">Раздел находится на реконструкции</div>',
    '<div style="display: none !important">Раздел находится на реконструкции</div>',
    '<footer>Раздел находится на реконструкции</footer>',
])
def test_hidden_and_non_content_text_does_not_block(hidden):
    result = landing_content.inspect('<main>' + hidden + '<h1>Окна</h1></main>')
    assert not result['placeholder']
    assert result['status'] == 'MANUAL'


def test_galleries_and_long_boilerplate_never_prove_completeness():
    gallery = landing_content.inspect('<main><h1>Наши работы</h1><img src="work.jpg"></main>')
    assert gallery['media_elements'] == 1
    assert not gallery['placeholder']
    assert gallery['review_required']
    long_page = landing_content.inspect('<main><p>' + 'Предложение. ' * 500 + '</p></main>')
    assert long_page['status'] == 'MANUAL'
    empty = landing_content.inspect('<main id="app"></main><script>render()</script>')
    assert empty['text_chars'] == 0
    assert 'limited_static_text' in empty['signals']
    page = landing_content.inspect('<main>Реконструкция зданий и установка окон</main>')
    assert page['placeholder'] is False


@pytest.mark.asyncio
async def test_http_200_placeholder_blocks_preflight_and_audit(monkeypatch, respx_mock):
    async def target(url):
        return httpx.URL(url), '93.184.216.34'
    monkeypatch.setattr(landing, '_public_target', target)
    monkeypatch.setattr(landing, 'HOST_INTERVAL', 0)
    landing._CACHE.clear()
    html = '<main><h1>Каталог</h1><p>Раздел находится на реконструкции</p></main>'
    respx_mock.route().respond(200, text=html)
    regions.reset_cache()
    api = FakeApi()
    preflight = await executor.preflight(api, compiled())
    assert preflight['status'] == 'BLOCK'
    checks = preflight['effective_link_checks']
    assert checks['failed_or_unchecked'] > 0
    assert checks['content_review_required']
    assert not any('add' in c[:3] for c in api.calls)
    page = checks['pages'][0]['site_check']
    assert page['http_ok'] and not page['ok']
    result = audit.audit_campaign(campaign(id=1), selected_policy=policy.get(),
                                  landing_pages=[{'campaigns': [1], 'site_check': page}])
    findings = {f['rule']: f for f in result['findings']}
    assert findings['landing.http']['status'] == policy.PASS
    assert findings['landing.content']['status'] == policy.BLOCK


@pytest.mark.asyncio
async def test_truncated_html_does_not_pass_effective_url_check(monkeypatch, respx_mock):
    async def target(url):
        return httpx.URL(url), '93.184.216.34'
    monkeypatch.setattr(landing, '_public_target', target)
    monkeypatch.setattr(landing, 'MAX_HTML_BYTES', 60)
    monkeypatch.setattr(landing, 'HOST_INTERVAL', 0)
    respx_mock.route().respond(200, text='<main>' + 'Details ' * 100 + '</main>')
    rows = await landing.inspect_pages([{'url': 'https://example.test/truncated'}])
    assert rows[0]['http_ok'] and rows[0]['html_truncated']
    assert not rows[0]['ok']
    assert 'truncated_html' in rows[0]['content']['signals']


@pytest.mark.parametrize('mode', ['report', 'campaign_setup'])
def test_mcp_exposes_content_review_without_mutating_policy(tmp_path, mode):
    result = _in_server(
        "import asyncio, json\nimport yadirect_mcp.server as s\n"
        "r=asyncio.run(s.direct_policy()).structuredContent\n"
        "print(json.dumps({'data':r,'instructions':mcp.instructions}))",
        str(tmp_path), YD_MODE=mode,
    )
    assert result['data']['policy'] == policy.get()
    assert result['data']['landing_review']['known_placeholder'] == 'BLOCK'
    assert 'landing_review' not in result['data']['policy']
    assert 'содержание всех разделов сайта' in result['instructions']
    assert 'landing-content' in knowledge.DOCS
    assert 'HTTP 200' in knowledge.read('landing-content')


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", [False, True])
async def test_inline_images_do_not_hide_page_tail(monkeypatch, respx_mock, placeholder):
    async def target(url):
        return httpx.URL(url), "93.184.216.34"
    monkeypatch.setattr(landing, "_public_target", target)
    monkeypatch.setattr(landing, "HOST_INTERVAL", 0)
    # The old 2 MB cap stopped inside this image and missed all following content.
    tail = ("Раздел находится на реконструкции" if placeholder
            else "Подбор квартиры и сопровождение сделки")
    html = ('<html><main><img src="data:image/png;base64,' + 'A' * 3_000_000
            + '"><h1>' + tail + '</h1><form></form>'
            + '<a href="tel:+70000000000">Позвонить</a></main></html>')
    respx_mock.route().respond(200, text=html)
    row = (await landing.inspect_pages([{"url": "https://example.test/inline"}]))[0]
    assert not row["html_truncated"]
    assert row["h1"] == tail
    assert row["conversion_actions"]["phone"]
    assert row["conversion_actions"]["form"]
    assert row["ok"] is not placeholder
    assert row["content"]["placeholder"] is placeholder
    assert row["content"]["review_required"]
