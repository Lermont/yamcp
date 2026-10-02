"""Offline Chrome acceptance of default copy and both report sections (no packages)."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from yadirect_mcp import report_pipeline as pipeline

CHROME = shutil.which("google-chrome") or shutil.which("chromium") or str(
    Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
    / "Google/Chrome/Application/chrome.exe"
)
pytestmark = pytest.mark.skipif(not Path(CHROME).is_file(), reason="Chrome is not installed")
ROOT = Path(__file__).parent


@pytest.mark.parametrize("scenario,width", [
    ("goals", 1440), ("goals", 390), ("traffic", 1440), ("unavailable_goals", 1440),
    ("incomplete", 1440), ("budget", 1440), ("recommendation", 1440), ("setup", 1440),
])
def test_client_report_rendered_copy_and_navigation(tmp_path, scenario, width):
    model = json.loads((ROOT / "templates/client-report/example-data.json").read_text("utf-8"))
    period = model["periods"][0]
    expected = "для оценки реальных обращений и продаж нужна сверка"
    if scenario in {"traffic", "unavailable_goals"}:
        period["measurement"] = {"available": False}
        expected = "на ваш сайт"
        if scenario == "unavailable_goals":
            period["measurement"]["note"] = (
                "Мы пока не смогли проверить цели. Сейчас оцениваем показы, переходы и расходы."
            )
            expected = "Мы пока не смогли проверить цели"
    elif scenario == "incomplete":
        model["daily"] = [r for r in model["daily"] if r["date"] != period["start"]]
        expected = "Общий итог пока не рассчитываем"
    elif scenario == "budget":
        period["budget"] = {}
        expected = "План бюджета на этот период не указан"
    elif scenario == "recommendation":
        period["dataRevision"] = "test-revision"
        period["insight"] = {
            "dataRevision": "test-revision", "title": "Наши рекомендации",
            "text": "Предлагаем сверить цели № 77 с вашим учётом обращений в CRM. "
            "Рекомендуем сохранить текущий темп расходов до оценки их качества.",
        }
        expected = "Предлагаем сверить цели № 77"
    elif scenario == "setup":
        model["periods"], model["daily"] = [], []
        expected = "В этом разделе пока нет статистики"
    output = pipeline._renderer().render(model)
    check = r"""
    <script>
    (() => {
      const result = {ok:false};
      const assert = (value, note) => {if (!value) throw new Error(note);};
      try {
        document.querySelector('#tab-setup').click();
        assert(!document.querySelector('#panel-setup').hidden, 'Setup hidden');
        assert(document.querySelector('#panel-setup h1').textContent, 'Setup missing');
        assert(document.querySelectorAll('.campaign-card').length === 4, 'Campaigns lost');
        document.querySelector('#tab-statistics').click();
        assert(!document.querySelector('#panel-statistics').hidden, 'Statistics hidden');
        const text = document.querySelector('#panel-statistics').textContent;
        assert(text.includes(EXPECTED), 'Missing client copy: ' + EXPECTED);
        assert(!/MCP|API|JSON|TSV|каталог целей|восстановление нулей|командой клиента/i.test(text),
               'Internal commentary exposed');
        assert(!text.includes('Отслеживание целей не настроено'), 'Unavailable inferred absent');
        assert(document.documentElement.scrollWidth <= innerWidth + 1, 'Horizontal overflow');
        if (SCENARIO !== 'setup') {
          const periods = document.querySelector('#period');
          assert(periods.options.length === 2, 'History lost');
          periods.value = '2026-07';
          periods.dispatchEvent(new Event('change'));
          assert(document.querySelector('#period').value === '2026-07', 'History inaccessible');
          document.querySelector('[data-channel="search"]').click();
          const selected = document.querySelector('[data-channel="search"]');
          assert(selected.getAttribute('aria-pressed') === 'true',
                 'Channel filter broken');
        }
        result.ok = true;
      } catch(error) { result.error = error.message; }
      const node = document.createElement('pre'); node.id = 'editorial-test-result';
      node.textContent = JSON.stringify(result); document.body.appendChild(node);
    })();
    </script>
    """.replace("EXPECTED", json.dumps(expected, ensure_ascii=True)).replace(
        "SCENARIO", json.dumps(scenario),
    )
    page = tmp_path / "report.html"
    page.write_text(output.replace("</body>", check + "</body>"), encoding="utf-8")
    result = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
         "--disable-background-networking", "--disable-component-update",
         "--no-default-browser-check",
         f"--user-data-dir={tmp_path / 'chrome-profile'}", f"--window-size={width},1000",
         "--virtual-time-budget=1000", "--dump-dom", page.as_uri()],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
    )
    match = re.search(r'<pre id="editorial-test-result">(.*?)</pre>', result.stdout, re.S)
    assert match, result.stderr[-2000:]
    evidence = json.loads(html.unescape(match[1]))
    assert evidence["ok"], evidence
