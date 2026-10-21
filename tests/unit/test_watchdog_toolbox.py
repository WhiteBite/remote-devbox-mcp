import types
from pathlib import Path

import pytest
from rdm import cli, procman, watchdog


def _arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(watchdog.hostos, "tempdir", lambda: tmp_path)
    monkeypatch.setattr(procman, "_pids_path", lambda name: tmp_path / "pids.txt")
    monkeypatch.setattr(procman, "watchdog_pids_path", lambda: tmp_path / "wd" / "pids.txt")
    monkeypatch.setattr(procman, "action_lock_path", lambda: tmp_path / "action.lock")
    monkeypatch.setattr(procman, "host_services_dead", lambda name: False)
    monkeypatch.setattr(procman, "host_services_recorded", lambda name: True)
    monkeypatch.setattr(watchdog.netprobe, "can_connect", lambda port: True)
    monkeypatch.setattr(watchdog.netprobe, "ingress_url", lambda env_map, compose_file: "http://tunnel.test")
    monkeypatch.setattr(watchdog.docker, "compose_ps", lambda compose_file: "")
    calls: list = []

    def _compose(*args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(watchdog.docker, "compose", _compose)
    return calls


def test_toolbox_missing_logged_once_but_retried_every_cycle(monkeypatch, tmp_path):
    calls = _arm(monkeypatch, tmp_path)

    watchdog.run(
        {"ACTIVE_PROFILE": ""},
        compose_file=str(tmp_path / "compose.yml"),
        iterations=3,
        prober=lambda *args: 200,
    )

    log = (tmp_path / "rdm-watchdog" / "watchdog.log").read_text(encoding="utf-8")
    assert log.count("toolbox state: missing") == 1
    assert len([c for c in calls if c[:1] == ("up",)]) == 3


def _arm_unhealthy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list:
    calls = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(watchdog.docker, "compose_ps", lambda compose_file: "rdm-toolbox Up unhealthy")
    return calls


def test_toolbox_unhealthy_below_threshold_not_recreated(monkeypatch, tmp_path):
    calls = _arm_unhealthy(monkeypatch, tmp_path)

    watchdog.run(
        {"ACTIVE_PROFILE": ""},
        compose_file=str(tmp_path / "compose.yml"),
        iterations=2,
        prober=lambda *args: 200,
    )

    assert [c for c in calls if "--force-recreate" in c] == []


def test_toolbox_unhealthy_recreated_after_threshold_with_cooldown(monkeypatch, tmp_path):
    calls = _arm_unhealthy(monkeypatch, tmp_path)
    monkeypatch.setattr(watchdog, "TOOLBOX_RECREATE_COOLDOWN_SECONDS", 0.0)

    watchdog.run(
        {"ACTIVE_PROFILE": ""},
        compose_file=str(tmp_path / "compose.yml"),
        iterations=6,
        prober=lambda *args: 200,
    )

    assert len([c for c in calls if "--force-recreate" in c]) == 2


def test_toolbox_unhealthy_cooldown_blocks_second_recreate(monkeypatch, tmp_path):
    calls = _arm_unhealthy(monkeypatch, tmp_path)

    watchdog.run(
        {"ACTIVE_PROFILE": ""},
        compose_file=str(tmp_path / "compose.yml"),
        iterations=6,
        prober=lambda *args: 200,
    )

    assert len([c for c in calls if "--force-recreate" in c]) == 1
