from __future__ import annotations

import json
import subprocess

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


def _profile_json(tmp_path):
    return json.dumps(
        {
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
    )


def _prober(url, token, timeout=10.0):
    if url.endswith("/healthz"):
        return 200
    if url.endswith("/mcp"):
        return 401
    return 403


def _wire(monkeypatch, tmp_path, *, healthy=True):
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    (tmp_path / "proj").mkdir()
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "_PROJECTS", projects)
    monkeypatch.setattr(doctor, "_HOME", tmp_path)
    monkeypatch.setattr(doctor, "_ingress_url", lambda env_map: env_map.get("PUBLIC_URL", ""))
    monkeypatch.setattr(doctor, "_can_connect", lambda port: healthy)
    monkeypatch.setattr(doctor, "_first_pid", lambda path: 12345)
    monkeypatch.setattr(hostos, "cmdline_matches", lambda pid, marker: True)
    monkeypatch.setattr(doctor.docker, "run", lambda *a, **k: subprocess.CompletedProcess(["docker"], 0, stdout="", stderr=""))
    monkeypatch.setattr(doctor.docker, "compose_ps", lambda *a, **k: "rdm-toolbox healthy" if healthy else "rdm-toolbox starting")


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