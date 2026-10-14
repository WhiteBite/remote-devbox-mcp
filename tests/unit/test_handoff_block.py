from __future__ import annotations

import json
from pathlib import Path

import pytest

from stitch_devbox import service

_TOKENS = {
    "MCP_BEARER_TOKEN": "b" * 64,
    "MCP_PUBLIC_TOKEN": "c" * 64,
    "INGRESS_TOKEN": "d" * 64,
}


def _fixture_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    rdm = service._rdm()
    lines = ["PUBLIC_URL=https://handoff.example.com"] + [
        f"{key}={value}" for key, value in _TOKENS.items()
    ]
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(rdm.cli, "ENV_FILE", env_path)
    monkeypatch.setattr(rdm.cli, "_runner_port", lambda env_map: 8796)
    return dict(_TOKENS)


def test_handoff_block_masks_by_default(monkeypatch, tmp_path):
    tokens = _fixture_env(monkeypatch, tmp_path)

    result = service.handoff_block()

    assert set(result) == {"block", "masked"}
    assert result["masked"] is True
    for value in tokens.values():
        assert value not in result["block"]
    assert "bbbb...bbbb" in result["block"]


def test_handoff_block_reveal_returns_unmasked_tokens(monkeypatch, tmp_path):
    tokens = _fixture_env(monkeypatch, tmp_path)

    result = service.handoff_block(reveal=True)

    assert set(result) == {"block", "masked"}
    assert result["masked"] is False
    for value in tokens.values():
        assert value in result["block"]


def test_handoff_block_handler_registered():
    from stitch_devbox import __main__ as plugin_main

    assert "handoff_block" in plugin_main._build_server()._handlers


def test_handoff_block_manifest_is_readonly():
    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "plugin.json").read_text(encoding="utf-8")
    )
    commands = {
        command["name"]: command["readonly"] for command in manifest["contributions"]["commands"]
    }
    assert commands["handoff_block"] is True
