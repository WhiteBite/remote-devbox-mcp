from __future__ import annotations

import http.client
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest
from rdm import cli, envfile, hostos, procman
from rdm.ui import api, auth, control, server

HOME_DIR = pathlib.Path(__file__).resolve().parents[2] / "home"
_SECRET_KEYS = ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN", "TUNNEL_TOKEN", "VLESS_SUB_URL")
_CHAT_BLOCK_KEYS = ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN")


def _ephemeral_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_listening(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"cockpit on 127.0.0.1:{port} never listened")


@pytest.fixture()
def cockpit(monkeypatch, tmp_path: pathlib.Path):
    state_dir = tmp_path / "ui-state"
    port = _ephemeral_port()
    env = {
        **os.environ,
        "RDM_UI_STATE_DIR": str(state_dir),
        "RDM_EVENTS_PATH": str(tmp_path / "events.jsonl"),
        "TMPDIR": str(tmp_path),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "rdm.ui", "--port", str(port)],
        cwd=HOME_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    monkeypatch.setenv("RDM_UI_STATE_DIR", str(state_dir))
    try:
        _wait_listening(port)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()


def _bootstrap_cookie(port: int) -> str:
    token = auth.write_bootstrap()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", f"/?t={token}")
    response = conn.getresponse()
    assert response.status == 302
    assert response.getheader("Location") == "/"
    cookie = response.getheader("Set-Cookie", "")
    response.read()
    conn.close()
    assert cookie.startswith("rdm_ui=")
    return cookie


def _session(cookie: str) -> str:
    return cookie.split("=", 1)[1].split(";", 1)[0]


def _get(port: int, path: str, session: str, extra: dict[str, str] | None = None):
    headers = {"Cookie": f"rdm_ui={session}", **(extra or {})}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    return urllib.request.urlopen(request, timeout=30)


def _env_values(keys: tuple[str, ...]) -> dict[str, str]:
    env_map = envfile.EnvFile.load(HOME_DIR / ".env").as_map()
    return {key: env_map[key] for key in keys if len(env_map.get(key, "")) >= 8}


def test_status_401_without_session_then_200_after_bootstrap(cockpit):
    port = cockpit
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=10)
    assert denied.value.code == 401

    cookie = _bootstrap_cookie(port)
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie

    with _get(port, "/api/status", _session(cookie)) as response:
        assert response.status == 200
        payload = json.load(response)

    assert {"compose_ps", "ports", "env", "profile", "manifest"} <= set(payload)
    text = json.dumps(payload)
    for value in _env_values(_SECRET_KEYS).values():
        assert value not in text


def test_logs_mask_seeded_token_line(cockpit, tmp_path):
    port = cockpit
    secret = "0123456789abcdef" * 4
    host_dir = tmp_path / "rdm-host"
    host_dir.mkdir()
    (host_dir / "seed.out").write_text(f"boot ok\ntoken={secret}\n", encoding="utf-8")

    session = _session(_bootstrap_cookie(port))
    with _get(port, "/api/logs?source=host", session) as response:
        assert response.status == 200
        payload = json.load(response)

    text = json.dumps(payload)
    assert secret not in text
    assert "[REDACTED]" in text

    with _get(port, "/api/logs?source=host&filter=boot", session) as response:
        filtered = json.load(response)
    entries = [entry for entry in filtered["logs"] if entry["file"] == "seed.out"]
    assert entries and entries[0]["lines"] == ["boot ok"]


def test_foreign_host_rejected(cockpit):
    port = cockpit
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("GET", "/api/status", skip_host=True)
    conn.putheader("Host", "evil.example.com")
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 403
    response.read()
    conn.close()


def test_bootstrap_rejects_foreign_token_and_replay(cockpit):
    port = cockpit
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/?t=foreign-token")
    rejected = conn.getresponse()
    assert rejected.status == 403
    rejected.read()
    conn.close()

    token = auth.write_bootstrap()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", f"/?t={token}")
    first = conn.getresponse()
    assert first.status == 302
    assert first.getheader("Location") == "/"
    assert first.getheader("Set-Cookie", "").startswith("rdm_ui=")
    first.read()
    conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", f"/?t={token}")
    replay = conn.getresponse()
    assert replay.status == 403
    replay.read()
    conn.close()


def test_invalid_bootstrap_token_with_valid_session_serves_index(cockpit):
    port = cockpit
    session = _session(_bootstrap_cookie(port))

    with _get(port, "/?t=stale-token", session) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/html"


def test_handoff_masked_by_default_reveal_needs_header_token(cockpit):
    port = cockpit
    session = _session(_bootstrap_cookie(port))

    with _get(port, "/api/handoff", session) as response:
        assert response.status == 200
        masked = json.load(response)

    with pytest.raises(urllib.error.HTTPError) as denied:
        _get(port, "/api/handoff?reveal=1", session)
    assert denied.value.code == 403

    with _get(port, "/api/handoff?reveal=1", session, {"X-RDM-Token": session}) as response:
        assert response.status == 200
        revealed = json.load(response)

    masked_text = json.dumps(masked)
    revealed_text = json.dumps(revealed)
    for value in _env_values(_SECRET_KEYS).values():
        assert value not in masked_text
    for value in _env_values(_CHAT_BLOCK_KEYS).values():
        assert value in revealed_text


def test_build_server_refuses_non_loopback_host():
    with pytest.raises(ValueError):
        server.build_server("0.0.0.0", _ephemeral_port())


def _seed_events(path: pathlib.Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def test_events_sse_streams_seeded_line(cockpit, tmp_path):
    port = cockpit
    seeded = {"kind": "mcp_request", "rpc_method": "tools/call", "tool": "edit", "rpc_id": 1}
    _seed_events(tmp_path / "events.jsonl", [seeded])

    session = _session(_bootstrap_cookie(port))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/events", headers={"Cookie": f"rdm_ui={session}"})
    response = conn.getresponse()
    try:
        assert response.status == 200
        assert response.getheader("Content-Type", "").startswith("text/event-stream")
        line = response.readline()
    finally:
        conn.close()
    assert line.startswith(b"data: ")
    assert json.loads(line[len(b"data: ") :]) == seeded


def test_jobs_and_permissions_correlated_view(cockpit, tmp_path):
    port = cockpit
    _seed_events(
        tmp_path / "events.jsonl",
        [
            {"kind": "mcp_request", "ts": 100.0, "session": "s1", "rpc_method": "tools/call", "tool": "edit", "rpc_id": 1},
            {
                "kind": "mcp_response",
                "ts": 101.0,
                "session": "s1",
                "job_id": "job-1",
                "status": "awaiting_permission",
                "permission": "edit",
            },
            {
                "kind": "mcp_request",
                "ts": 105.0,
                "session": "s1",
                "rpc_method": "tools/call",
                "tool": "opencode_permission_reply",
                "rpc_id": 2,
            },
            {"kind": "mcp_response", "ts": 106.0, "session": "s1", "job_id": "job-1", "status": "completed", "exit": 0},
        ],
    )

    session = _session(_bootstrap_cookie(port))
    with _get(port, "/api/jobs", session) as response:
        assert response.status == 200
        payload = json.load(response)
    assert [job["job_id"] for job in payload["jobs"]] == ["job-1"]
    assert payload["jobs"][0]["tool"] == "edit"
    assert payload["jobs"][0]["status"] == "completed"
    assert payload["jobs"][0]["permission"] == "edit"
    assert payload["stalled"] == []

    with _get(port, "/api/permissions", session) as response:
        assert response.status == 200
        permissions = json.load(response)
    assert [job["job_id"] for job in permissions["permissions"]] == ["job-1"]
    assert permissions["permissions"][0]["permission"] == "edit"


def test_events_jobs_permissions_require_session(cockpit):
    port = cockpit
    for path in ("/api/events", "/api/jobs", "/api/permissions"):
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10)
        assert denied.value.code == 401


def test_diff_requires_session_in_process(monkeypatch, tmp_path):
    httpd, thread, port, _ = _start_in_process(monkeypatch, tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/diff", timeout=10)
        assert denied.value.code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_diff_non_git_fallback_in_process(monkeypatch, tmp_path):
    httpd, thread, port, session = _start_in_process(monkeypatch, tmp_path, with_session=True)
    try:
        with _get(port, "/api/diff", session) as response:
            assert response.status == 200
            payload = json.load(response)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert payload == {"git": False}


def _start_in_process(monkeypatch, tmp_path: pathlib.Path, *, with_session: bool = False):
    project = tmp_path / "project"
    project.mkdir()
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = {
        "project_dir": str(project),
        "toolchain": "",
        "mode": "standard",
        "host_services": [],
        "runner_commands": [],
        "allowed_ports": [],
        "port_ranges": [],
        "port_deny": [],
        "deny_mounts": [],
        "setup_cmds": [],
        "scripts": [],
    }
    (profiles_dir / "nogit.json").write_text(json.dumps(profile), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("ACTIVE_PROFILE=nogit\n", encoding="utf-8")

    monkeypatch.setattr(cli, "ENV_FILE", env_file)
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(profiles_dir))

    sessions = auth.SessionStore()
    session = sessions.mint_session() if with_session else ""
    httpd = server.build_server("127.0.0.1", _ephemeral_port(), sessions)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_listening(httpd.server_address[1])
    return httpd, thread, httpd.server_address[1], session


def test_ui_main_accepts_bare_server_sentinel(monkeypatch):
    import rdm.ui.__main__ as ui_main

    class _Fake:
        def serve_forever(self) -> None:
            return None

    monkeypatch.setattr(server, "build_server", lambda host, port, sessions=None: _Fake())
    stdout, stderr = sys.stdout, sys.stderr
    try:
        assert ui_main.main(["--server"]) == 0
    finally:
        sys.stdout, sys.stderr = stdout, stderr


def test_compose_ps_cached_with_ttl(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(api.docker, "compose_ps", lambda *a, **k: calls.append(a) or "ps-line")
    clock = {"now": 100.0}
    monkeypatch.setattr(api, "_clock", lambda: clock["now"])
    api.invalidate_status_cache()
    assert api._compose_ps(cli.COMPOSE_FILE) == "ps-line"
    assert api._compose_ps(cli.COMPOSE_FILE) == "ps-line"
    assert len(calls) == 1
    clock["now"] += api.COMPOSE_PS_TTL_SECONDS
    assert api._compose_ps(cli.COMPOSE_FILE) == "ps-line"
    assert len(calls) == 2
    api.invalidate_status_cache()
    assert api._compose_ps(cli.COMPOSE_FILE) == "ps-line"
    assert len(calls) == 3


def test_status_rebuilds_on_action_busy_transition(monkeypatch, tmp_path):
    builds: list[int] = []
    monkeypatch.setattr(server, "build_status", lambda port: builds.append(port) or {"stub": True})
    httpd = server.build_server("127.0.0.1", _ephemeral_port())
    try:
        httpd.status()
        httpd.status()
        assert len(builds) == 1
        monkeypatch.setattr(httpd.actions, "busy", lambda: True)
        httpd.status()
        httpd.status()
        assert len(builds) == 2
    finally:
        httpd.server_close()


def test_status_invalidated_on_action_submit(monkeypatch, tmp_path):
    builds: list[int] = []
    monkeypatch.setattr(server, "build_status", lambda port: builds.append(port) or {"stub": True})
    monkeypatch.setattr(control, "run_doctor", lambda: 0)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    sessions = auth.SessionStore()
    session = sessions.mint_session()
    httpd = server.build_server("127.0.0.1", _ephemeral_port(), sessions)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        _wait_listening(port)
        httpd.status()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request(
            "POST",
            "/api/action/doctor",
            body=json.dumps({}).encode("utf-8"),
            headers={"Cookie": f"rdm_ui={session}", "X-RDM-Token": session},
        )
        response = conn.getresponse()
        assert response.status == 202
        response.read()
        conn.close()
        httpd.status()
        assert len(builds) == 2
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_watchdog_alive_reads_pidfile(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert api._watchdog_alive() is False
    pid = hostos.spawn([sys.executable, "-c", "import time; time.sleep(60)", " watch"])
    try:
        path = procman.watchdog_pids_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{pid}|{hostos.create_time(pid)}| watch\n", encoding="utf-8")
        assert api._watchdog_alive() is True
    finally:
        hostos.kill_tree(pid)
    path.write_text("2147483647|| watch\n", encoding="utf-8")
    assert api._watchdog_alive() is False
