from __future__ import annotations

import http.client
import json
import socket
import threading

import pytest
from rdm import profiles
from rdm.ui import server


def _ephemeral_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _profile_data(project_dir: str) -> dict:
    return {
        "project_dir": project_dir,
        "toolchain": "",
        "git_name": "agent",
        "git_email": "agent@example.test",
        "preview_origin": "http://toolbox:8788",
        "ui_port": None,
        "mode": "standard",
        "host_services": [],
        "runner_commands": [],
        "runner_port": 8796,
        "allowed_ports": [],
        "port_ranges": [],
        "port_deny": [],
        "deny_mounts": [],
        "setup_cmds": [],
        "scripts": [],
    }


@pytest.fixture()
def layout(monkeypatch, tmp_path):
    env_dir = tmp_path / "env-projects"
    env_dir.mkdir()
    home = tmp_path / "isolated-home"
    repo_projects = tmp_path / "repo" / "projects"
    repo_projects.mkdir(parents=True)
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(env_dir))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(profiles, "app_dir", lambda: tmp_path / "repo" / "home")
    return {"env_dir": env_dir, "home": home, "repo_projects": repo_projects}


@pytest.fixture()
def cockpit():
    port = _ephemeral_port()
    srv = server.build_server("127.0.0.1", port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, srv.sessions.mint_session()
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _request(port: int, method: str, path: str, session: str | None, body: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Cookie": f"rdm_ui={session}"} if session else {}
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data) if data else None


def _seed_repo_profile(layout, tmp_path) -> dict:
    (tmp_path / "proj").mkdir()
    data = _profile_data(str(tmp_path / "proj"))
    (layout["repo_projects"] / "fallback.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def test_repo_fallback_edit_lands_in_repo_file_not_home(layout, cockpit, tmp_path):
    data = _seed_repo_profile(layout, tmp_path)
    profile_path = layout["repo_projects"] / "fallback.json"
    port, session = cockpit

    status, payload = _request(port, "GET", "/api/profile/fallback", session)
    assert status == 200
    assert payload["name"] == "fallback"
    assert payload["path"] == str(profile_path)
    assert payload["source_dir"] == str(layout["repo_projects"])
    assert payload["raw"] == data

    status, payload = _request(port, "PUT", "/api/profile/fallback", session, {**data, "git_name": "editor"})
    assert status == 200

    on_disk = json.loads(profile_path.read_text(encoding="utf-8"))
    assert on_disk["git_name"] == "editor"
    assert not (layout["home"] / ".devbox" / "projects").exists()


def test_invalid_edit_leaves_file_byte_identical(layout, cockpit, tmp_path):
    _seed_repo_profile(layout, tmp_path)
    profile_path = layout["repo_projects"] / "fallback.json"
    before = profile_path.read_bytes()
    port, session = cockpit

    invalid = _profile_data(str(tmp_path / "proj")) | {"git_email": "no-at-sign"}
    status, payload = _request(port, "PUT", "/api/profile/fallback", session, invalid)

    assert status == 400
    assert "R5" in payload["error"]
    assert profile_path.read_bytes() == before
    assert list(profile_path.parent.glob(".*.tmp")) == []


def test_write_raw_atomic_no_partial_file_on_validation_failure(layout, tmp_path):
    (tmp_path / "proj").mkdir()
    target = layout["repo_projects"] / "atomic.json"
    target.write_text(json.dumps(_profile_data(str(tmp_path / "proj")), indent=2), encoding="utf-8")
    before = target.read_bytes()

    invalid = _profile_data(str(tmp_path / "proj")) | {"port_ranges": [[70000, 71000]]}
    with pytest.raises(ValueError):
        profiles.write_raw(target, invalid)

    assert target.read_bytes() == before
    assert list(target.parent.glob(".*.tmp")) == []


def test_write_raw_writes_payload(tmp_path):
    (tmp_path / "proj").mkdir()
    target = tmp_path / "edited.json"
    data = _profile_data(str(tmp_path / "proj")) | {"git_name": "editor"}

    profiles.write_raw(target, data)

    assert json.loads(target.read_text(encoding="utf-8")) == data


def test_write_raw_allows_warn_only_problems(tmp_path):
    (tmp_path / "proj").mkdir()
    target = tmp_path / "warned.json"
    data = _profile_data(str(tmp_path / "proj"))
    del data["preview_origin"]

    profiles.write_raw(target, data)

    assert json.loads(target.read_text(encoding="utf-8")) == data


def test_profile_endpoints_require_session(layout, cockpit, tmp_path):
    data = _seed_repo_profile(layout, tmp_path)
    port, _ = cockpit

    status, _ = _request(port, "GET", "/api/profile/fallback", None)
    assert status == 401

    status, _ = _request(port, "PUT", "/api/profile/fallback", None, data)
    assert status == 401
