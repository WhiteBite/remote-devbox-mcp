import os
import types
from pathlib import Path

import pytest
from rdm import cli, hostos, procman, profiles, watchdog


def _arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, calls: list, runner_commands: tuple) -> Path:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    pids = tmp_path / "pids.txt"
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(procman, "_pids_path", lambda name: pids)
    monkeypatch.setattr(procman, "watchdog_pids_path", lambda: tmp_path / "wd" / "pids.txt")
    monkeypatch.setattr(procman, "action_lock_path", lambda: tmp_path / "action.lock")
    monkeypatch.setattr(watchdog.netprobe, "can_connect", lambda port: True)
    monkeypatch.setattr(watchdog.netprobe, "ingress_url", lambda env_map, compose_file: "")
    monkeypatch.setattr(watchdog.docker, "compose_ps", lambda compose_file: "healthy")
    monkeypatch.setattr(
        watchdog.docker, "compose", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(profiles, "find", lambda name: tmp_path / "profile.json")
    monkeypatch.setattr(
        profiles, "load", lambda path: types.SimpleNamespace(exposed_mcps=(), runner_commands=runner_commands)
    )
    monkeypatch.setattr(
        cli,
        "_with_runner",
        lambda profile, name: types.SimpleNamespace(host_services=bool(runner_commands), exposed_mcps=()),
    )
    monkeypatch.setattr(procman, "restart_host_services", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(procman, "start_gateway", lambda *a, **k: None)
    monkeypatch.setattr(procman, "stop_gateway", lambda: None)
    return pids


def _run(tmp_path: Path) -> None:
    watchdog.run(
        {"ACTIVE_PROFILE": "p"},
        compose_file=str(tmp_path / "compose.yml"),
        iterations=1,
        prober=lambda *args: 200,
    )


def test_watchdog_starts_services_when_pids_unrecorded(monkeypatch, tmp_path):
    calls: list = []
    _arm(monkeypatch, tmp_path, calls, ("cmd",))
    _run(tmp_path)
    assert len(calls) == 1


def test_watchdog_idle_when_profile_has_no_services(monkeypatch, tmp_path):
    calls: list = []
    _arm(monkeypatch, tmp_path, calls, ())
    _run(tmp_path)
    assert calls == []


def test_watchdog_idle_when_recorded_services_owned(monkeypatch, tmp_path):
    calls: list = []
    pids = _arm(monkeypatch, tmp_path, calls, ("cmd",))
    procman._write_entries(pids, [(os.getpid(), hostos.create_time(os.getpid()), "pytest")])
    _run(tmp_path)
    assert calls == []
