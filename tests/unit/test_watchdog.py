from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from rdm import hostos, procman, watchdog

_SLEEP_60 = "import time; time.sleep(60)"


def _wire(monkeypatch, tmp_path, dead=False, ingress_up=True, ps="rdm-toolbox healthy"):
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    (projects / "p1.json").write_text(json.dumps({"project_dir": str(tmp_path), "git_name": "a", "git_email": "a@b"}), encoding="utf-8")
    calls: dict[str, list] = {"compose": [], "restart": [], "ingress_start": []}
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(projects))
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
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path / "projects"))
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


def test_watchdog_single_instance_guard_exits_when_foreign_alive(monkeypatch, tmp_path, capsys):
    calls = _wire(monkeypatch, tmp_path)
    pid = hostos.spawn([sys.executable, "-c", _SLEEP_60, " watch"])
    try:
        path = procman.watchdog_pids_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}|{hostos.create_time(pid)}| watch\n", encoding="utf-8")
        watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)
        assert "уже запущен" in capsys.readouterr().out
        assert calls["compose"] == []
        assert procman._read_entries(path) == [(pid, hostos.create_time(pid), " watch")]
    finally:
        hostos.kill_tree(pid)


def test_watchdog_guard_treats_venv_launcher_pid_as_self(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, ps="rdm-toolbox Up (unhealthy)")
    path = procman.watchdog_pids_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = os.getppid()
    path.write_text(f"{parent}|{hostos.create_time(parent)}| watch\n", encoding="utf-8")

    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)

    assert calls["compose"] != []


def test_watchdog_guard_passes_on_stale_pidfile(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, ps="rdm-toolbox Up (unhealthy)")
    path = procman.watchdog_pids_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("2147483647|| watch\n", encoding="utf-8")
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)
    assert calls["compose"] != []
    assert not path.exists()


def test_watchdog_guard_passes_on_own_pid(monkeypatch, tmp_path):
    calls = _wire(monkeypatch, tmp_path, ps="rdm-toolbox Up (unhealthy)")
    path = procman.watchdog_pids_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}|{hostos.create_time(os.getpid())}| watch\n", encoding="utf-8")
    watchdog.run(_env(), iterations=1, prober=lambda url, token, timeout=10.0: 200)
    assert calls["compose"] != []
    assert not path.exists()