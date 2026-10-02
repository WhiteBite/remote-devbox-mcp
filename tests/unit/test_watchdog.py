from __future__ import annotations

import json
import subprocess

from rdm import watchdog


def _wire(monkeypatch, tmp_path, dead=False, ingress_up=True):
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    (projects / "p1.json").write_text(json.dumps({"project_dir": str(tmp_path), "git_name": "a", "git_email": "a@b"}), encoding="utf-8")
    calls: dict[str, list] = {"compose": [], "restart": [], "ingress_start": []}
    monkeypatch.setattr(watchdog, "_PROJECTS", projects)
    monkeypatch.setattr(watchdog, "_ingress_url", lambda env_map: "https://x")
    monkeypatch.setattr(watchdog, "_can_connect", lambda port: ingress_up)
    monkeypatch.setattr(watchdog.procman, "stop_ingress", lambda: None)
    monkeypatch.setattr(watchdog.procman, "start_ingress", lambda env_map, host_dir, state_dir: calls["ingress_start"].append(env_map) or 4242)
    monkeypatch.setattr(watchdog.procman, "host_services_dead", lambda active: dead)
    monkeypatch.setattr(watchdog.docker, "compose", lambda *a, **k: calls["compose"].append(a) or subprocess.CompletedProcess(["docker"], 0, stdout="", stderr=""))
    monkeypatch.setattr(watchdog.docker, "compose_ps", lambda *a, **k: "rdm-toolbox healthy")
    monkeypatch.setattr(watchdog.procman, "restart_host_services", lambda *a, **k: calls["restart"].append(a) or [])
    return calls


def _env():
    return {"ACTIVE_PROFILE": "p1", "MCP_BEARER_TOKEN": "b" * 64, "MCP_PUBLIC_TOKEN": "p" * 64}


def test_watchdog_recreates_ingress_after_three_failures(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    watchdog.run(_env(), iterations=4, prober=lambda url, token, timeout=10.0: 0)
    recreates = [c for c in calls["compose"] if "--force-recreate" in c]
    assert len(recreates) == 1


def test_watchdog_restarts_dead_host_service(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, dead=True)
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)
    assert len(calls["restart"]) == 1


def test_watchdog_bounded_iterations(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    watchdog.run(_env(), iterations=2, prober=lambda url, token, timeout=10.0: 200)
    assert [c for c in calls["compose"] if "--force-recreate" in c] == []


def test_watchdog_restarts_dead_ingress_origin(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, ingress_up=False)
    logs: list[str] = []
    monkeypatch.setattr(watchdog, "_log", lambda path, message: logs.append(message))
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 0)
    assert len(calls["ingress_start"]) == 1
    assert "ingress proxy dead: restart" in logs
    assert [c for c in calls["compose"] if "--force-recreate" in c] == []


def test_watchdog_keeps_ingress_when_origin_alive(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    watchdog.run(_env(), iterations=2, prober=lambda url, token, timeout=10.0: 200)
    assert calls["ingress_start"] == []