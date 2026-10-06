from __future__ import annotations

import ast
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]

REQUIRED = [
    "ARENA.md",
    "arena/README.md",
    "arena/AGENT_INSTRUCTIONS.md",
    "arena/SANDBOX_FACTS.md",
    "arena/mcp_client.py",
    "arena/mcp",
    "arena/mcp.conf.example",
    "skills/remote-devbox/SKILL.md",
]


def test_agent_bootstrap_artifacts_present():
    missing = [rel for rel in REQUIRED if not (REPO / rel).is_file()]
    assert missing == []


def test_skill_frontmatter_is_valid():
    text = (REPO / "skills/remote-devbox/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front = text.split("---", 2)[1]
    assert re.search(r"^name:\s*remote-devbox\s*$", front, re.M)
    assert re.search(r"^description:", front, re.M)


def test_entrypoints_link_the_skill():
    for rel in ("README.md", "ARENA.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "skills/remote-devbox/SKILL.md" in text


def _top_level_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_mcp_client_is_stdlib_only():
    assert _top_level_imports(REPO / "arena/mcp_client.py") <= set(sys.stdlib_module_names)


def test_stdio_adapter_is_stdlib_only():
    modules = _top_level_imports(REPO / "arena/mcp-stdio-adapter.py")
    assert modules <= set(sys.stdlib_module_names) | {"mcp_client"}