from __future__ import annotations

import json
import subprocess

from rdm import watchdog


def _wire(monkeypatch, tmp_path, dead=False, ingress_up=True, ps="rdm-toolbox healthy"):
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    (projects / "p1.json").write_text(json.dumps({"project_dir": str(tmp_path), "git_name": "a", "git_email": "a@b"}), encoding="utf-8")
    calls: dict[str, list] = {"compose": [], "restart": [], "ingress_start": []}
    monkeypatch.setattr(watchdog, "_PROJECTS", projects)
    monkeypatch.setattr("rdm.cli.ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(watchdog, "_log", lambda path, message: None)
    monkeypatch.setattr(watchdog.netprobe, "ingress_url", lambda env_map, compose_file=None: "https://x")
    monkeypatch.setattr(watchdog.netprobe, "can_connect", lambda port: ingress_up)
    monkeypatch.setattr(watchdog.procman, "stop_ingress", lambda: None)
    monkeypatch.setattr(watchdog.procman, "start_ingress", lambda env_map, host_dir: calls["ingress_start"].append(env_map) or 4242)
    monkeypatch.setattr(watchdog.procman, "host_services_dead", lambda active: dead)
    monkeypatch.setattr(watchdog.docker, "compose", lambda *a, **k: calls["compose"].append(a) or subprocess.CompletedProcess(["docker"], 0, stdout="", stderr=""))
    monkeypatch.setattr(watchdog.docker, "compose_ps", lambda *a, **k: ps)
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
    (tmp_path / ".env").write_text("INGRESS_TOKEN=fresh-token\n", encoding="utf-8")
    monkeypatch.setattr("rdm.cli.ENV_FILE", tmp_path / ".env")
    logs: list[str] = []
    monkeypatch.setattr(watchdog, "_log", lambda path, message: logs.append(message))
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 0)
    assert len(calls["ingress_start"]) == 1
    assert calls["ingress_start"][0]["INGRESS_TOKEN"] == "fresh-token"
    assert "ingress proxy dead: restart" in logs
    assert [c for c in calls["compose"] if "--force-recreate" in c] == []


def test_watchdog_recreates_unhealthy_toolbox(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, ps="rdm-toolbox Up (unhealthy)")
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)
    toolbox = [c for c in calls["compose"] if "toolbox" in c]
    assert len(toolbox) == 1
    assert "--force-recreate" in toolbox[0]


def test_watchdog_starts_absent_toolbox(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, ps="cloudflared-ingress Up (healthy)")
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)
    toolbox = [c for c in calls["compose"] if "toolbox" in c]
    assert len(toolbox) == 1
    assert "--force-recreate" not in toolbox[0]


def test_watchdog_leaves_starting_toolbox_alone(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, ps="rdm-toolbox Up (health: starting)")
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)
    assert [c for c in calls["compose"] if "toolbox" in c] == []


def test_watchdog_keeps_ingress_when_origin_alive(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    watchdog.run(_env(), iterations=2, prober=lambda url, token, timeout=10.0: 200)
    assert calls["ingress_start"] == []


def _wire_manifest(monkeypatch, tmp_path):
    log_root = tmp_path / "rdm-host"
    manifest_path = log_root / "rdm-manifest.json"
    monkeypatch.setattr("rdm.cli.PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr("rdm.cli.LOG_ROOT", log_root)
    monkeypatch.setattr("rdm.cli.MANIFEST_PATH", manifest_path)
    log_root.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"ingress_url": "https://stale.example.test"}), encoding="utf-8")
    return manifest_path


def test_watchdog_recreate_refreshes_manifest(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("ACTIVE_PROFILE=p1\n", encoding="utf-8")
    manifest_path = _wire_manifest(monkeypatch, tmp_path)
    watchdog.run(_env(), iterations=4, prober=lambda url, token, timeout=10.0: 0)
    assert len([c for c in calls["compose"] if "--force-recreate" in c]) == 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ingress_url"] == "https://x"


def test_watchdog_ingress_restart_refreshes_manifest(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, ingress_up=False)
    (tmp_path / ".env").write_text("ACTIVE_PROFILE=p1\n", encoding="utf-8")
    manifest_path = _wire_manifest(monkeypatch, tmp_path)
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)
    assert len(calls["ingress_start"]) == 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["ingress_url"] == "https://x"