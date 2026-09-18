"""Direct phrase syntax and conservative comparison of stored negatives.

https://yandex.ru/support/direct/ru/keywords/symbols-and-operators
"""

from __future__ import annotations

import re


def validate(value: str, field: str, *, negative: bool = False) -> None:
    if not re.fullmatch(r'[\w\s!+\-"\[\]()|]+', value) or "_" in value:
        raise ValueError(f"{field}: недопустимые символы или операторы во фразе {value!r}")
    if negative and (value.startswith("-") or re.search(r"\s-", value)):
        raise ValueError(f"{field}: минус-фраза передаётся без оператора минус")
    if negative and re.search(r"[()|]", value):
        raise ValueError(f"{field}: передайте варианты минус-фразы отдельными элементами")
    stack = []
    quoted = False
    for i, char in enumerate(value):
        if char == '"':
            quoted = not quoted
        elif char in "[(":
            stack.append((char, i))
        elif char in "])":
            if not stack or stack[-1][0] != {"]": "[", ")": "("}[char]:
                raise ValueError(f"{field}: несогласованные скобки")
            _, start = stack.pop()
            if not value[start + 1 : i].strip():
                raise ValueError(f"{field}: пустые скобки")
        elif char == "|":
            if not any(item[0] == "(" for item in stack):
                raise ValueError(f"{field}: варианты с | заключите в круглые скобки")
            if (
                not value[:i].rstrip()
                or value[:i].rstrip()[-1] in "(|"
                or not value[i + 1 :].lstrip()
                or value[i + 1 :].lstrip()[0] in ")|"
            ):
                raise ValueError(f"{field}: пустой вариант оператора |")
    if stack or quoted or re.search(r'"\s*"', value):
        raise ValueError(f"{field}: несогласованные скобки или кавычки")
    if re.search(r"[!+](?![^\W_])|(?<=[^\W_])[!+]", value):
        raise ValueError(f"{field}: ! и + должны стоять непосредственно перед словом")
    if not re.search(r"[^\W_]", value):
        raise ValueError(f"{field}: отсутствуют слова")


_STOP_WORDS = frozenset(
    [
        "в",
        "во",
        "на",
        "с",
        "со",
        "к",
        "ко",
        "у",
        "о",
        "об",
        "от",
        "до",
        "по",
        "за",
        "из",
        "без",
        "для",
        "при",
        "и",
        "или",
        "а",
        "но",
        "не",
        "ни",
        "кто",
        "что",
        "как",
        "где",
        "когда",
        "какой",
        "такой",
        "свои",
        "своим",
        "своими",
        "свой",
    ]
)


def canonical(value: str) -> str:
    value = " ".join(value.casefold().replace("ё", "е").split())
    return re.sub(r"(?<!\w)[!+]([^\W_]+)", lambda m: m[1] if m[1] in _STOP_WORDS else m[0], value)


def equivalent(actual: list[str], expected: list[str]) -> bool:
    return {canonical(v) for v in actual} == {canonical(v) for v in expected}
