"""Shared Direct creative limits; agency asset counts are enforced by callers.

Verified 2026-09-06: https://yandex.ru/dev/direct/doc/ru/ads/get
The API explicitly lists !,.;:\" as narrow characters and limits them to 15.
"""

import re

NARROW_CHARACTERS = frozenset('!,.;:"')


def validate_title(title: str, field: str) -> None:
    if len(title) > 56:
        raise ValueError(f"{field} длиннее 56 символов")
    if any(len(word) > 22 for word in title.split()):
        raise ValueError(f"{field} содержит слово длиннее 22 символов")


def validate_text(text: str, field: str) -> None:
    narrow_count = sum(character in NARROW_CHARACTERS for character in text)
    if len(text) - narrow_count > 81 or narrow_count > 15:
        raise ValueError(f"{field} превышает лимит 81+15 узких символов")
    if any(len(word) > 23 for word in text.split()):
        raise ValueError(f"{field} содержит слово длиннее 23 символов")


def validate_display_path(path: str, field: str) -> None:
    if len(path) > 20:
        raise ValueError(f"{field} длиннее 20 символов")
    if (not re.fullmatch(r"[\w\-№/%#]+", path, flags=re.UNICODE)
            or "_" in path or "--" in path or "//" in path):
        raise ValueError(f"{field} содержит запрещённые символы")
