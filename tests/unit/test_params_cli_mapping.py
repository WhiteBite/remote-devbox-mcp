from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from stitch_devbox import service

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "plugin.json"


def _profile_data(tmp_path: Path) -> dict[str, Any]:
    return {
        "project_dir": str(tmp_path),
        "git_name": "agent",
        "git_email": "agent@example.test",
        "preview_origin": "http://toolbox:8788",
        "mode": "standard",
    }


@pytest.mark.parametrize(
    ("service_fn", "cli_fn_name", "call_args", "expected_args"),
    [
        (service.ingress_control, "_ingress", ("up",), ("start",)),
        (service.ingress_control, "_ingress", ("down",), ("stop",)),
        (service.watchdog_control, "_watchdog_control", ("start",), ("start",)),
        (service.watchdog_control, "_watchdog_control", ("stop",), ("stop",)),
        (service.stack_start, "_start", ("alpha", True), ("alpha", True)),
        (service.stack_down, "_down", (), ()),
        (service.stack_full_down, "_full_down", (), ()),
        (service.profile_use, "apply_use", ("alpha",), ("alpha",)),
        (service.stop_host, "_stop_host", (), ()),
        (service.issue_tokens, "_issue_tokens", (), ()),
        (service.run_doctor, "_doctor", (), ()),
    ],
)
def test_params_map_to_cli_entry_points(
    monkeypatch, tmp_path, service_fn, cli_fn_name, call_args, expected_args
):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    rdm = service._rdm()
    recorded: list[tuple] = []
    done = threading.Event()

    def fake_fn(*args: Any) -> int:
        recorded.append(args)
        done.set()
        return 0

    monkeypatch.setattr(rdm.cli, cli_fn_name, fake_fn)

    result = service_fn(*call_args)

    assert result["accepted"] is True
    assert done.wait(5)
    assert recorded == [expected_args]


@pytest.mark.parametrize("bad_action", ["sideways", "restart", ""])
def test_ingress_control_rejects_unknown_action(bad_action):
    with pytest.raises(ValueError):
        service.ingress_control(bad_action)


@pytest.mark.parametrize("bad_action", ["up", "down", "sideways"])
def test_watchdog_control_rejects_unknown_action(bad_action):
    with pytest.raises(ValueError):
        service.watchdog_control(bad_action)


def test_profile_use_requires_name():
    with pytest.raises(ValueError):
        service.profile_use("")


def test_profile_put_invalid_json_returns_valid_false(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "alpha.json").write_text(json.dumps(_profile_data(tmp_path)), encoding="utf-8")

    result = service.profile_put("alpha", "{oops")

    assert result["valid"] is False
    assert result["errors"]
    assert json.loads((tmp_path / "alpha.json").read_text(encoding="utf-8"))["git_name"] == "agent"


def test_build_server_registration_matches_manifest_commands():
    from stitch_devbox import __main__ as plugin_main

    handlers = set(plugin_main._build_server()._handlers)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    declared = {command["name"] for command in manifest["contributions"]["commands"]}

    assert handlers - {"_migrate_db"} == declared
