"""Offline acceptance snapshots: no account operations or publication."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from yadirect_mcp import report_pipeline as pipeline


def model():
    return json.loads((Path(__file__).parent / "templates/client-report/example-data.json")
                      .read_text(encoding="utf-8"))


@pytest.mark.parametrize("change", ["missing", "duplicate", "bad_method", "not_applicable",
                                   "no_first_show", "reversed", "internal", "extra", "empty"])
def test_acceptance_rejects_incomplete_or_misleading_snapshot(change):
    data = model()
    a = data["setup"]["acceptance"]
    if change == "missing":
        a["items"].pop()
    elif change == "duplicate":
        a["items"][-1] = deepcopy(a["items"][0])
    elif change == "bad_method":
        a["items"][0]["method"] = "none"
    elif change == "not_applicable":
        a["items"][0].update(status="not_applicable", method="none")
    elif change == "no_first_show":
        a["launch"]["status"] = "observed"
    elif change == "reversed":
        a["launch"].update(status="observed", firstImpressionDate="2026-06-01")
    elif change == "internal":
        a["items"][0]["text"] = "Проверено через API и MCP."
    elif change == "extra":
        a["private_token"] = "secret"
    else:
        a["items"][0]["text"] = " "
    with pytest.raises(ValueError):
        pipeline._renderer().render(data)


@pytest.mark.parametrize("launch", ["observed", "not_started", "paused", "unknown"])
@pytest.mark.parametrize("legacy", [False, True])
def test_acceptance_render_preserves_history_and_input(launch, legacy):
    data = model()
    a = data["setup"]["acceptance"]
    a["launch"]["status"] = launch
    if launch == "observed":
        a["launch"]["firstImpressionDate"] = "2026-05-31"
    if legacy:
        data["setup"].pop("acceptance")
    before = deepcopy(data)
    result = pipeline._renderer().render(data)
    assert data == before
    from test_report_editorial import embedded
    assert embedded(result) == data


def test_optional_section_can_be_inapplicable_with_reason():
    data = model()
    data["setup"]["acceptance"]["items"][-2].update(
        status="not_applicable", method="none",
        text="Организация не используется в выбранном формате; контакты есть на сайте.")
    assert pipeline._renderer().render(data)


def test_setup_acceptance_is_omitted_from_statistics_only_export():
    result = pipeline._renderer().render(model(), "statistics")
    from test_report_editorial import embedded
    assert "setup" not in embedded(result)
