import types
from pathlib import Path

import pytest
from rdm import cli, hostos, procman, profiles, watchdog


def _arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, calls: list) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(procman, "watchdog_pids_path", lambda: tmp_path / "wd" / "pids.txt")
    monkeypatch.setattr(procman, "action_lock_path", lambda: tmp_path / "action.lock")
    monkeypatch.setattr(watchdog.netprobe, "can_connect", lambda port: True)
    monkeypatch.setattr(watchdog.netprobe, "ingress_url", lambda env_map, compose_file: "")
    monkeypatch.setattr(watchdog.docker, "compose_ps", lambda compose_file: "healthy")
    monkeypatch.setattr(
        watchdog.docker, "compose", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(procman, "host_services_dead", lambda name: True)
    monkeypatch.setattr(profiles, "find", lambda name: tmp_path / "profile.json")
    monkeypatch.setattr(profiles, "load", lambda path: types.SimpleNamespace(exposed_mcps=()))
    monkeypatch.setattr(cli, "_with_runner", lambda profile, name: profile)
    monkeypatch.setattr(procman, "restart_host_services", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(procman, "start_gateway", lambda *a, **k: None)
    monkeypatch.setattr(procman, "stop_gateway", lambda: None)


def _run(tmp_path: Path) -> None:
    watchdog.run(
        {"ACTIVE_PROFILE": "p"},
        compose_file=str(tmp_path / "compose.yml"),
        iterations=1,
        prober=lambda *args: 200,
    )


def test_watchdog_skips_restart_while_cli_action_holds_lock(monkeypatch, tmp_path):
    calls: list = []
    _arm(monkeypatch, tmp_path, calls)
    with hostos.action_lock(procman.action_lock_path()):
        _run(tmp_path)
    assert calls == []


def test_watchdog_restarts_when_no_cli_action(monkeypatch, tmp_path):
    calls: list = []
    _arm(monkeypatch, tmp_path, calls)
    _run(tmp_path)
    assert len(calls) == 1
