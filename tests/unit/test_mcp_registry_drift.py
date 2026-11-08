from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from rdm.prompt import CLIENTS

_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data" / "harness-kit-registry.json"
_LOCK_PATH = _REGISTRY_PATH.parent / "harness-kit.lock"

_CLIENT_TO_HARNESS = {
    "claude-code": "claude",
    "opencode": "opencode",
    "codex": "codex",
    "cursor": "cursor",
    "vscode": "vscode",
    "cline": "cline",
    "roo": "roo",
    "gemini": "gemini",
    "zed": "zed",
    "factory": "factory",
    "kiro": "kiro",
    "windsurf": "windsurf",
}


def _mcp_block(client: str) -> dict:
    registry = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    harness = registry["harnesses"][_CLIENT_TO_HARNESS[client]]
    mcp = harness.get("mcp")
    assert mcp is not None, f"{client}: registry row has no mcp block"
    return mcp


def test_vendored_registry_matches_the_lock():
    lock = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
    data = _REGISTRY_PATH.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == lock["files"]["registry/harnesses.json"]


def test_every_client_has_a_registry_mapping():
    assert set(CLIENTS) == set(_CLIENT_TO_HARNESS)


@pytest.mark.parametrize("client", sorted(_CLIENT_TO_HARNESS))
def test_client_config_path_matches_registry(client: str):
    mcp = _mcp_block(client)
    spec = CLIENTS[client]
    paths = [mcp["config"].get("project"), mcp["config"].get("user")]
    assert any(path and path in spec.path_line for path in paths), (
        f"{client}: no registry mcp path appears in path_line {spec.path_line!r}"
    )


@pytest.mark.parametrize("client", sorted(_CLIENT_TO_HARNESS))
def test_client_container_matches_registry(client: str):
    mcp = _mcp_block(client)
    spec = CLIENTS[client]
    haystack = spec.block + "\n" + spec.path_line
    assert mcp["container"] in haystack, (
        f"{client}: registry container {mcp['container']!r} not found in the client block"
    )
