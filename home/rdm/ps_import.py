"""Ограниченный парсер литералов профиля projects/*.ps1.

Поддерживает только присваивания `$Var =` со значениями: строка, число,
$true/$false, `@( ... )` и `@{ ... }`. Используется лишь командой импорта;
в рантайме профили читаются из JSON (rdm.profiles).
"""

from __future__ import annotations

import re

_STRING = r"'(?:[^']|'')*'|\"(?:[^\"`]|`.)*\""
_TOKEN = re.compile(
    r"(?P<ws>\s+)"
    r"|(?P<comment>\#[^\n]*)"
    r"|(?P<string>" + _STRING + r")"
    r"|(?P<open_array>@\()"
    r"|(?P<open_hash>@\{)"
    r"|(?P<number>-?\d+)"
    r"|(?P<var>\$[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<punct>[=;,\}\)])"
)
_PUNCT = "punct"
_SEPARATORS = {",", ";"}


class PsImportError(ValueError):
    pass


def _snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper():
            prev = name[i - 1] if i else ""
            nxt = name[i + 1] if i + 1 < len(name) else ""
            if i and (prev.islower() or prev.isdigit() or (prev.isupper() and nxt.islower())):
                out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _unquote(token: str) -> str:
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    return token[1:-1]


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise PsImportError(f"неожиданный символ {text[pos]!r} в позиции {pos}")
        pos = match.end()
        kind = match.lastgroup
        if kind not in ("ws", "comment"):
            tokens.append((kind, match.group()))
    return tokens


def _parse_array(tokens: list[tuple[str, str]], pos: int) -> tuple[list[object], int]:
    pos += 1
    items: list[object] = []
    while True:
        if pos >= len(tokens):
            raise PsImportError("обрезанный вход: незакрытый массив @(")
        kind, value = tokens[pos]
        if kind == _PUNCT and value == ")":
            return items, pos + 1
        if kind == _PUNCT and value in _SEPARATORS:
            pos += 1
            continue
        item, pos = _parse_value(tokens, pos)
        items.append(item)


def _parse_hash(tokens: list[tuple[str, str]], pos: int) -> tuple[dict[str, object], int]:
    pos += 1
    result: dict[str, object] = {}
    while True:
        if pos >= len(tokens):
            raise PsImportError("обрезанный вход: незакрытый хэш @{")
        kind, value = tokens[pos]
        if kind == _PUNCT and value == "}":
            return result, pos + 1
        if kind == _PUNCT and value in _SEPARATORS:
            pos += 1
            continue
        if kind != "ident":
            raise PsImportError(f"ожидался ключ хэш-таблицы, получено {value!r}")
        key = _snake(value)
        pos += 1
        if pos >= len(tokens):
            raise PsImportError("обрезанный вход: ожидался '=' после ключа")
        kind, value = tokens[pos]
        if kind != _PUNCT or value != "=":
            raise PsImportError("ожидался '=' после ключа")
        pos += 1
        result[key], pos = _parse_value(tokens, pos)


def _parse_value(tokens: list[tuple[str, str]], pos: int) -> tuple[object, int]:
    if pos >= len(tokens):
        raise PsImportError("обрезанный вход: ожидалось значение")
    kind, value = tokens[pos]
    if kind == "string":
        return _unquote(value), pos + 1
    if kind == "number":
        return int(value), pos + 1
    if kind == "var":
        if value == "$true":
            return True, pos + 1
        if value == "$false":
            return False, pos + 1
        raise PsImportError(f"подстановка переменных не поддерживается: {value}")
    if kind == "open_array":
        return _parse_array(tokens, pos)
    if kind == "open_hash":
        return _parse_hash(tokens, pos)
    raise PsImportError(f"неподдерживаемое значение: {value!r}")


def parse_profile_ps1(text: str) -> dict[str, object]:
    tokens = _tokenize(text)
    result: dict[str, object] = {}
    pos = 0
    while pos < len(tokens):
        kind, value = tokens[pos]
        if kind != "var":
            raise PsImportError(f"ожидалось присваивание, получено {value!r}")
        key = _snake(value[1:])
        pos += 1
        if pos >= len(tokens):
            raise PsImportError("обрезанный вход: ожидался '=' при присваивании")
        kind, value = tokens[pos]
        if kind != _PUNCT or value != "=":
            raise PsImportError("ожидался '=' при присваивании")
        pos += 1
        result[key], pos = _parse_value(tokens, pos)
    return result