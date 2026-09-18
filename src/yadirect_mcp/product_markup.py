"""Detect declared Schema.org Product markup without executing page scripts."""

from __future__ import annotations

import json
from contextlib import suppress
from html.parser import HTMLParser


def _product(value) -> bool:
    if isinstance(value, list):
        return any(_product(v) for v in value)
    if not isinstance(value, dict):
        return False
    types = value.get("@type", [])
    types = [types] if isinstance(types, str) else types
    if isinstance(types, list) and any(
        t
        in {"Product", "https://schema.org/Product", "http://schema.org/Product", "schema:Product"}
        for t in types
        if isinstance(t, str)
    ):
        return True
    return any(_product(v) for v in value.values())


class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = False
        self.json_parts = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "itemscope" in values and any(
            t in {"https://schema.org/Product", "http://schema.org/Product"}
            for t in (values.get("itemtype") or "").split()
        ):
            self.found = True
        if tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
            self.json_parts = []

    def handle_data(self, data):
        if self.json_parts is not None:
            self.json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.json_parts is not None:
            with suppress(ValueError, RecursionError):
                self.found = self.found or _product(json.loads("".join(self.json_parts)))
            self.json_parts = None


def present(html: str) -> bool:
    parser = Parser()
    parser.feed(html)
    return parser.found
