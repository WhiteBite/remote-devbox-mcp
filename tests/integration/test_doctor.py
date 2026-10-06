from __future__ import annotations

import json
import subprocess
import types

from rdm import doctor, hostos


def _env(tmp_path, profile="p1"):
    return {
        "MCP_BEARER_TOKEN": "b" * 64,
        "MCP_PUBLIC_TOKEN": "p" * 64,
        "INGRESS_TOKEN": "i" * 64,
        "PROJECT_DIR": str(tmp_path / "proj"),
        "ACTIVE_PROFILE": profile,
        "PUBLIC_URL": "https://devbox.example.test",
    }


def _profile_json(tmp_path, **extra):
    base = {
        "project_dir": str(tmp_path / "proj"),
        "toolchain": "",
        "git_name": "agent",
        "git_email": "a@b.test",
        "preview_origin": "http://x",
        "mode": "standard",
        "host_services": [],
        "allowed_ports": [1],
        "deny_mounts": [],
    }
    base.update(extra)
    return json.dumps(base)


def _prober(url, token, timeout=10.0):
    if url.endswith("/healthz"):
        return 200
    if url.endswith("/mcp"):
        return 401
    return 403


def _wire(monkeypatch, tmp_path, *, healthy=True, profile_json=None):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "p1.json").write_text(profile_json or _profile_json(tmp_path), encoding="utf-8")
    (tmp_path / "proj").mkdir()
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_PROJECTS", projects)
    monkeypatch.setattr(doctor, "_HOME", tmp_path)
    monkeypatch.setattr(doctor.netprobe, "ingress_url", lambda env_map, compose_file=None: env_map.get("PUBLIC_URL", ""))
    monkeypatch.setattr(doctor.netprobe, "can_connect", lambda port: healthy)
    monkeypatch.setattr(doctor, "_first_entry", lambda path: (12345, None, "rdm.proxy"))
    monkeypatch.setattr(hostos, "cmdline_matches", lambda pid, marker: True)
    monkeypatch.setattr(doctor.docker, "run", lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, stdout="", stderr=""))
    monkeypatch.setattr(doctor.docker, "compose", lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, stdout="healthy" if healthy else "starting", stderr=""))


def _write_registry(tmp_path, port):
    registry = tmp_path / "proj" / "tools" / "muffin-supervisor" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"ops": {"svc": {"port": port}}}), encoding="utf-8")


def test_doctor_fails_when_env_keys_missing(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    assert doctor.run({}, prober=_prober) == 1
    assert "[FAIL] .env keys" in capsys.readouterr().out


def test_doctor_passes_with_all_checks(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    assert doctor.run(_env(tmp_path), prober=_prober) == 0
    out = capsys.readouterr().out
    assert "[FAIL]" not in out
    assert "[PASS] bridge via ingress 200" in out


def test_doctor_counts_host_service_alive_via_create_time(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(hostos, "cmdline_matches", lambda pid, marker: marker == "rdm.proxy")
    monkeypatch.setattr(hostos, "create_time", lambda pid: 111.5)
    pids = hostos.tempdir() / "rdm-host" / "p1-pids.txt"
    pids.parent.mkdir(parents=True, exist_ok=True)
    pids.write_text("12345|111.5|python.EXE tools/muffin-supervisor/server.py\n", encoding="utf-8")
    try:
        assert doctor.run(_env(tmp_path), prober=_prober) == 0
    finally:
        pids.unlink(missing_ok=True)
    assert "[FAIL] host services alive" not in capsys.readouterr().out


def test_doctor_ingress_alive_via_create_time_when_marker_differs(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor, "_first_entry", lambda path: (12345, 111.5, "proxy --mode"))
    monkeypatch.setattr(hostos, "cmdline_matches", lambda pid, marker: False)
    monkeypatch.setattr(hostos, "create_time", lambda pid: 111.5)
    assert doctor.run(_env(tmp_path), prober=_prober) == 0
    assert "[FAIL] ingress pid" not in capsys.readouterr().out


def test_doctor_reads_gitleaks_report(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    report = hostos.tempdir() / "rdm-host" / "gitleaks-p1.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps([{"secret": "x"}]), encoding="utf-8")
    try:
        doctor.run(_env(tmp_path), prober=_prober)
    finally:
        report.unlink(missing_ok=True)
    assert "[WARN] gitleaks" in capsys.readouterr().out


def test_doctor_runner_fail_when_not_listening(monkeypatch, tmp_path, capsys):
    profile = _profile_json(tmp_path, runner_commands=[{"name": "dev", "cmd": ["python", "x.py"]}], runner_port=9000)
    _wire(monkeypatch, tmp_path, profile_json=profile)
    monkeypatch.setattr(doctor.netprobe, "can_connect", lambda port: port != 9000)
    assert doctor.run(_env(tmp_path), prober=_prober) == 1
    assert "[FAIL] runner listen 9000" in capsys.readouterr().out


def test_doctor_runner_warn_when_probe_raises(monkeypatch, tmp_path, capsys):
    profile = _profile_json(tmp_path, runner_commands=[{"name": "dev", "cmd": ["python", "x.py"]}], runner_port=9000)
    _wire(monkeypatch, tmp_path, profile_json=profile)

    def probe(port):
        if port == 9000:
            raise OSError("unreachable")
        return True

    monkeypatch.setattr(doctor.netprobe, "can_connect", probe)
    assert doctor.run(_env(tmp_path), prober=_prober) == 0
    out = capsys.readouterr().out
    assert "[WARN] runner 9000: проба невозможна" in out
    assert "[FAIL] runner listen 9000" not in out


def test_doctor_ui_port_fail_when_not_allowed(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path, profile_json=_profile_json(tmp_path, ui_port=2))
    assert doctor.run(_env(tmp_path), prober=_prober) == 1
    out = capsys.readouterr().out
    assert "[FAIL] ui_port 2" in out
    assert "devbox.py allow 2 --ui" in out


def test_doctor_warns_drift_for_blocked_registry_port(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    _write_registry(tmp_path, 9100)
    assert doctor.run(_env(tmp_path), prober=_prober) == 0
    out = capsys.readouterr().out
    assert "[WARN] drift" in out
    assert "devbox.py allow 9100" in out


def test_doctor_warns_scan_for_live_blocked_registry_port(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path)
    _write_registry(tmp_path, 9100)
    conn = types.SimpleNamespace(status="LISTEN", laddr=types.SimpleNamespace(ip="127.0.0.1", port=9100))
    fake_psutil = types.SimpleNamespace(CONN_LISTEN="LISTEN", net_connections=lambda kind="tcp": [conn])
    monkeypatch.setattr(hostos, "psutil", fake_psutil)
    doctor.run(_env(tmp_path), prober=_prober)
    out = capsys.readouterr().out
    assert "[WARN] scan" in out
    assert "devbox.py allow 9100" in out