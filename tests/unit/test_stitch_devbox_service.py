from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from stitch_devbox import service

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "plugin.json"

_REAL_ENV = {
    "USERPROFILE": r"C:\Users\real",
    "HOMEDRIVE": "C:",
    "HOMEPATH": r"\Users\real",
    "TEMP": r"C:\Users\real\AppData\Local\Temp",
    "TMP": r"C:\Users\real\AppData\Local\Temp",
}


def _sandbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", r"C:\sandbox\home")
    monkeypatch.setenv("USERPROFILE", r"C:\sandbox\home")
    monkeypatch.setenv("HOMEDRIVE", "C:")
    monkeypatch.setenv("HOMEPATH", r"\sandbox\home")
    monkeypatch.setenv("TEMP", r"C:\sandbox\temp")
    monkeypatch.setenv("TMP", r"C:\sandbox\temp")


def _profile_data(tmp_path: Path) -> dict[str, Any]:
    return {
        "project_dir": str(tmp_path),
        "git_name": "agent",
        "git_email": "agent@example.test",
        "preview_origin": "http://toolbox:8788",
        "mode": "standard",
    }


@pytest.mark.skipif(os.name != "nt", reason="registry-backed env adoption is Windows-only")
def test_adopt_real_home_restores_temp_and_home_vars(monkeypatch):
    monkeypatch.setattr(service, "_real_user_env", lambda: dict(_REAL_ENV), raising=False)
    _sandbox_env(monkeypatch)

    service._adopt_real_home()

    assert os.environ["HOME"] == r"C:\Users\real"
    assert os.environ["USERPROFILE"] == r"C:\Users\real"
    assert os.environ["HOMEDRIVE"] == "C:"
    assert os.environ["HOMEPATH"] == r"\Users\real"
    assert os.environ["TEMP"] == r"C:\Users\real\AppData\Local\Temp"
    assert os.environ["TMP"] == r"C:\Users\real\AppData\Local\Temp"


@pytest.mark.skipif(os.name != "nt", reason="registry-backed env adoption is Windows-only")
def test_adopt_real_home_skips_when_host_driver(monkeypatch):
    monkeypatch.setattr(service, "_real_user_env", lambda: dict(_REAL_ENV), raising=False)
    monkeypatch.setattr(service, "_host_capabilities", frozenset({"host_driver"}), raising=False)
    _sandbox_env(monkeypatch)

    service._adopt_real_home()

    assert os.environ["TEMP"] == r"C:\sandbox\temp"
    assert os.environ["USERPROFILE"] == r"C:\sandbox\home"


def test_set_host_capabilities_stores_supported(monkeypatch):
    monkeypatch.setattr(service, "_host_capabilities", frozenset(), raising=False)

    service.set_host_capabilities(["host_driver", "plugin_rpc"])

    assert service._host_capabilities == frozenset({"host_driver", "plugin_rpc"})


def _patch_ingress(monkeypatch: pytest.MonkeyPatch, recorded: list[str], done: threading.Event) -> None:
    rdm = service._rdm()

    def fake_ingress(action: str) -> int:
        recorded.append(action)
        done.set()
        return 0

    monkeypatch.setattr(rdm.cli, "_ingress", fake_ingress)


def test_ingress_control_maps_up_to_start(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    recorded: list[str] = []
    done = threading.Event()
    _patch_ingress(monkeypatch, recorded, done)

    result = service.ingress_control("up")

    assert result["accepted"] is True
    assert done.wait(5)
    assert recorded == ["start"]


def test_ingress_control_maps_down_to_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    recorded: list[str] = []
    done = threading.Event()
    _patch_ingress(monkeypatch, recorded, done)

    result = service.ingress_control("down")

    assert result["accepted"] is True
    assert done.wait(5)
    assert recorded == ["stop"]


def test_ingress_control_rejects_unknown_action():
    with pytest.raises(ValueError):
        service.ingress_control("sideways")


def _patch_cli_fn(monkeypatch: pytest.MonkeyPatch, fn_name: str, recorded: list[str], done: threading.Event) -> None:
    rdm = service._rdm()

    def fake_fn(action: str) -> int:
        recorded.append(action)
        done.set()
        return 0

    monkeypatch.setattr(rdm.cli, fn_name, fake_fn)


def test_watchdog_control_passes_start(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    recorded: list[str] = []
    done = threading.Event()
    _patch_cli_fn(monkeypatch, "_watchdog_control", recorded, done)

    result = service.watchdog_control("start")

    assert result["accepted"] is True
    assert done.wait(5)
    assert recorded == ["start"]


def test_watchdog_control_passes_stop(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    recorded: list[str] = []
    done = threading.Event()
    _patch_cli_fn(monkeypatch, "_watchdog_control", recorded, done)

    result = service.watchdog_control("stop")

    assert result["accepted"] is True
    assert done.wait(5)
    assert recorded == ["stop"]


def test_watchdog_control_rejects_unknown_action():
    with pytest.raises(ValueError):
        service.watchdog_control("up")


def test_stack_full_down_enqueues_cli_full_down(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    recorded: list[str] = []
    done = threading.Event()
    rdm = service._rdm()

    def fake_full_down() -> int:
        recorded.append("called")
        done.set()
        return 0

    monkeypatch.setattr(rdm.cli, "_full_down", fake_full_down)

    result = service.stack_full_down()

    assert result["accepted"] is True
    assert done.wait(5)
    assert recorded == ["called"]


def test_overview_rows_carry_id_tone_and_i18n_titles(monkeypatch):
    rdm = service._rdm()
    payload = {
        "env": {"ACTIVE_PROFILE": "alpha"},
        "ports": {"ingress": True, "bridge": False, "runner": None},
        "watchdog": False,
        "host_services": {"alive": 2, "recorded": 3},
        "manifest": {"project_dir": "D:/work/alpha"},
        "compose_ps": "healthy",
    }
    monkeypatch.setattr(rdm.ui.api, "build_status", lambda port: payload)

    rows = service.overview()

    assert [row["id"] for row in rows] == [
        "profile", "ingress", "bridge", "runner", "watchdog", "host_services",
    ]
    assert [row["title"] for row in rows] == [
        "stitch-devbox.card.profile",
        "stitch-devbox.card.ingress",
        "stitch-devbox.card.bridge",
        "stitch-devbox.card.runner",
        "stitch-devbox.card.watchdog",
        "stitch-devbox.card.hostServices",
    ]
    assert rows[0]["value"] == "alpha"
    assert rows[1]["value"] == "up"
    assert rows[2]["value"] == "down"
    assert rows[5]["value"] == "2/3"
    assert [row["tone"] for row in rows] == ["ok", "ok", "down", "down", "warn", "ok"]
    assert rows[2]["hint"] == "stitch-devbox.hint.bridge"


def test_logs_text_empty_uses_i18n_key(monkeypatch):
    rdm = service._rdm()
    monkeypatch.setattr(rdm.ui.api, "collect_logs", lambda source, needle: [])

    assert service.logs_text() == {"text": "stitch-devbox.logsEmpty"}


def test_profiles_list_rows_always_full_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    rdm = service._rdm()
    monkeypatch.setattr(rdm.cli, "ENV_FILE", tmp_path / "missing.env")
    (tmp_path / "good.json").write_text(json.dumps(_profile_data(tmp_path)), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")

    rows = service.profiles_list()

    by_name = {row["name"]: row for row in rows}
    assert {"good", "broken"} <= set(by_name)
    for row in rows:
        assert set(row) == {"name", "mode", "project_dir", "active"}
        assert isinstance(row["active"], bool)
    assert by_name["good"]["mode"] == "standard"
    assert by_name["good"]["project_dir"] == str(tmp_path)
    assert by_name["good"]["active"] is False
    assert by_name["broken"]["mode"] == "?"
    assert by_name["broken"]["project_dir"] == ""


def test_jobs_payload_surfaces_stall_alarm(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    now = time.time()
    events = [
        {"ts": now - 400, "kind": "mcp_request", "rpc_method": "tools/call", "job_id": "j1", "tool": "bash", "session": "s1"},
        {"ts": now - 399, "kind": "mcp_response", "job_id": "j1", "tool": "bash", "status": "running"},
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    payload = service.jobs()

    assert "stalled" in payload
    assert any(alarm.get("job_id") == "j1" for alarm in payload["stalled"])


def test_logs_groups_lines_per_source(monkeypatch):
    rdm = service._rdm()
    entries = [
        {"source": "ingress", "file": "ingress.out", "lines": ["a", "b"]},
        {"source": "ingress", "file": "ingress.err", "lines": ["c"]},
        {"source": "host", "file": "svc.out", "lines": ["d"]},
    ]
    calls: list[tuple[str, str]] = []

    def fake_collect(source: str, needle: str) -> list[dict[str, Any]]:
        calls.append((source, needle))
        return entries

    monkeypatch.setattr(rdm.ui.api, "collect_logs", fake_collect)

    result = service.logs("ingress", "ERR", 2)

    assert calls == [("ingress", "err")]
    assert result == {
        "sources": [
            {"name": "ingress", "lines": ["b", "c"]},
            {"name": "host", "lines": ["d"]},
        ]
    }


def test_profile_put_writes_valid_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    path = tmp_path / "alpha.json"
    path.write_text(json.dumps(_profile_data(tmp_path)), encoding="utf-8")
    updated = dict(_profile_data(tmp_path), git_name="agent2")

    result = service.profile_put("alpha", json.dumps(updated))

    assert result == {"valid": True, "errors": []}
    assert json.loads(path.read_text(encoding="utf-8"))["git_name"] == "agent2"


def test_profile_put_returns_validation_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "alpha.json").write_text(json.dumps(_profile_data(tmp_path)), encoding="utf-8")
    broken = dict(_profile_data(tmp_path), project_dir="")

    result = service.profile_put("alpha", json.dumps(broken))

    assert result["valid"] is False
    assert any(problem.startswith("R1") for problem in result["errors"])


def test_profile_put_rejects_invalid_name(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))

    with pytest.raises(ValueError):
        service.profile_put("bad name!", json.dumps(_profile_data(tmp_path)))


def test_cockpit_open_returns_bootstrap_url(monkeypatch):
    rdm = service._rdm()
    from rdm.ui import auth

    spawned: list[tuple[dict[str, str], str]] = []

    def fake_start_ui(env_map: dict[str, str], home_dir: Path) -> int:
        spawned.append((env_map, str(home_dir)))
        return 123

    monkeypatch.setattr(rdm.procman, "start_ui", fake_start_ui)
    monkeypatch.setattr(rdm.netprobe, "can_connect", lambda port: True)
    monkeypatch.setattr(auth, "write_bootstrap", lambda: "tok123")

    result = service.cockpit_open()

    assert result == {"url": f"http://127.0.0.1:{rdm.ports.COCKPIT_PORT}/?t=tok123"}
    assert len(spawned) == 1


def test_reconcile_open_actions_marks_orphans_interrupted(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    now = time.time()
    lines = [
        {"ts": now, "kind": "action", "job_id": "a1", "tool": "start", "status": "started"},
        {"ts": now, "kind": "action", "job_id": "a2", "tool": "down", "status": "started"},
        {"ts": now, "kind": "action", "job_id": "a2", "tool": "down", "status": "finished", "exit": 0},
    ]
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    from rdm.ui import control

    assert control.reconcile_open_actions() == 1

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    interrupted = [event for event in events if event.get("status") == "interrupted"]
    assert len(interrupted) == 1
    assert interrupted[0]["job_id"] == "a1"
    assert control.reconcile_open_actions() == 0


def test_reconcile_actions_counts_interrupted(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    now = time.time()
    line = {"ts": now, "kind": "action", "job_id": "a1", "tool": "start", "status": "started"}
    (tmp_path / "events.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    assert service.reconcile_actions() == {"interrupted": 1}


def test_handle_init_wires_capabilities(monkeypatch):
    from stitch_devbox import __main__ as plugin_main

    monkeypatch.setattr(service, "_host_capabilities", frozenset(), raising=False)
    monkeypatch.setattr(service, "reconcile_actions", lambda: {"interrupted": 0})

    result = plugin_main._handle_init({"plugin_id": "stitch-devbox", "supported": ["host_driver"]})

    assert result["plugin_id"] == "stitch-devbox"
    assert service._host_capabilities == frozenset({"host_driver"})


def test_build_server_registers_contract_commands():
    from stitch_devbox import __main__ as plugin_main

    handlers = plugin_main._build_server()._handlers

    for name in (
        "cockpit_open",
        "profile_put",
        "logs",
        "ingress_control",
        "watchdog_control",
        "stack_full_down",
        "stack_start",
        "stack_down",
        "profiles_list",
        "action_status",
    ):
        assert name in handlers


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_declares_host_driver_and_contract_commands():
    manifest = _manifest()

    assert manifest["capabilities"] == ["host_driver"]
    commands = {command["name"]: command["readonly"] for command in manifest["contributions"]["commands"]}
    assert commands["cockpit_open"] is False
    assert commands["profile_put"] is False
    assert commands["watchdog_control"] is False
    assert commands["stack_full_down"] is False
    assert commands["logs"] is True


def test_manifest_i18n_resolves_service_keys_in_both_locales():
    strings = _manifest()["contributions"]["i18n"]
    card_keys = ("profile", "ingress", "bridge", "runner", "watchdog", "hostServices", "project", "compose")
    hint_keys = ("profile", "ingress", "bridge", "runner", "watchdog", "hostServices")

    for locale in ("ru", "en"):
        plugin_strings = strings[locale]["stitch-devbox"]
        for key in card_keys:
            assert plugin_strings["card"][key]
        for key in hint_keys:
            assert plugin_strings["hint"][key]
        assert plugin_strings["logsEmpty"]
        assert plugin_strings["permissions"]


def test_profiles_rows_carry_frozen_contract_keys():
    for row in service.profiles_list():
        assert {"name", "mode", "project_dir", "active"} <= set(row)
        assert isinstance(row["active"], bool)


def test_profile_put_creates_missing_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    project = tmp_path / "proj"
    project.mkdir()
    body = {
        "project_dir": str(project),
        "mode": "readonly",
        "toolchain": "",
        "git_name": "tester",
        "git_email": "tester@example.com",
    }

    result = service.profile_put("brand-new", json.dumps(body))

    assert result == {"valid": True, "errors": []}
    assert (tmp_path / "brand-new.json").exists()


def test_profile_delete_removes_and_rejects_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    project = tmp_path / "proj"
    project.mkdir()
    body = {
        "project_dir": str(project),
        "mode": "readonly",
        "toolchain": "",
        "git_name": "tester",
        "git_email": "tester@example.com",
    }
    service.profile_put("doomed", json.dumps(body))

    assert service.profile_delete("doomed") == {"deleted": True}
    assert not (tmp_path / "doomed.json").exists()
    with pytest.raises(ValueError):
        service.profile_delete("doomed")
