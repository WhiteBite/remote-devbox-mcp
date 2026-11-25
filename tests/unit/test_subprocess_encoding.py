"""Гейт: текстовый subprocess без явного encoding — источник cp1251-mojibake.

Инцидент: AGENTS.md и вывод контейнеров ломались на русской Windows —
`text=True` без `encoding=` декодирует в локальной кодировке (cp1251), а
docker/git/JSON-RPC отдают UTF-8. Правило: любой вызов subprocess.* в
текстовом режиме обязан явно задавать `encoding=` (или `errors=`).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCAN_DIRS = ("home", "arena", "stitch_devbox")
_CALLS = {"run", "Popen", "check_output", "check_call", "call"}


def _violations(tree: ast.AST, path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        owner = getattr(getattr(func, "value", None), "id", "")
        if name not in _CALLS or owner != "subprocess":
            continue
        kwargs = {}
        for keyword in node.keywords:
            if keyword.arg:
                kwargs[keyword.arg] = keyword.value
        text_mode = any(
            isinstance(kwargs.get(flag), ast.Constant) and kwargs[flag].value
            for flag in ("text", "universal_newlines")
        )
        if text_mode and "encoding" not in kwargs and "errors" not in kwargs:
            found.append(f"{path}:{node.lineno}")
    return found


def test_no_text_mode_subprocess_without_encoding():
    bad: list[str] = []
    for scan_dir in _SCAN_DIRS:
        for path in sorted((_ROOT / scan_dir).rglob("*.py")):
            if "_vendor" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            bad.extend(_violations(tree, path.relative_to(_ROOT)))
    assert bad == [], "text-mode subprocess без encoding=/errors= (cp1251-mojibake)"


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ('subprocess.run(["x"], text=True)', 1),
        ('subprocess.run(["x"], text=True, encoding="utf-8")', 0),
        ('subprocess.run(["x"], text=True, errors="replace")', 0),
        ('subprocess.Popen(["x"], text=True, bufsize=1)', 1),
        ('subprocess.check_output(["x"], universal_newlines=True)', 1),
        ('subprocess.run(["x"], capture_output=True)', 0),
    ],
)
def test_gate_discriminates(snippet: str, expected: int):
    tree = ast.parse(f"import subprocess\n{snippet}\n")
    assert len(_violations(tree, pathlib.Path("snippet.py"))) == expected
