"""Guarded creation of sitelinks and callouts."""

from __future__ import annotations

import asyncio

import pytest

from yadirect_mcp import assets


def source():
    return {
        "sitelink_sets": [{
            "sitelinks": [{
                "title": "Каталог",
                "href": "https://example.test/catalog/",
                "description": "Посмотрите доступные решения",
            }] + [{"title": f"Раздел {i}", "href": f"https://example.test/{i}/"}
                  for i in range(2, 5)]
        }],
        "callouts": ["Для бизнеса", "Каталог решений"],
    }


class FakeApi:
    def __init__(self):
        self.calls = []
        self.next_id = 10

    async def call_v501(self, service, method, params, *, client_login=None):
        self.calls.append((service, method, params, client_login))
        rows = next(iter(params.values()))
        result = []
        for _ in rows:
            self.next_id += 1
            result.append({"Id": self.next_id})
        return {"AddResults": result}


def test_assets_are_validated_and_hashed():
    plan = assets.normalize(source(), "client")
    assert len(plan["plan_hash"]) == 64
    assert plan["sitelink_sets"][0]["Sitelinks"][0]["Title"] == "Каталог"
    assert plan["callouts"][0]["Callout"]["CalloutText"] == "Для бизнеса"


def test_invalid_sitelink_and_duplicate_callout_are_rejected():
    value = source()
    value["sitelink_sets"][0]["sitelinks"][0]["href"] = "/relative"
    with pytest.raises(ValueError, match="HTTP"):
        assets.normalize(value, "client")
    value = source()
    value["callouts"] = ["Каталог", " каталог "]
    with pytest.raises(ValueError, match="дубликаты"):
        assets.normalize(value, "client")


def test_assets_apply_uses_v501():
    api = FakeApi()
    plan = assets.normalize(source(), "client")
    result = asyncio.run(assets.apply(api, plan))
    assert result["status"] == "complete"
    assert [(row[0], row[1]) for row in api.calls] == [
        ("sitelinks", "add"),
        ("adextensions", "add"),
    ]
