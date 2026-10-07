from __future__ import annotations

import http.client
import json
import pathlib
import socket
import threading
import time

import pytest
from rdm.ui import auth, control, server


def _ephemeral_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_idle(cockpit: server.CockpitServer, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while cockpit.actions.busy():
        if time.monotonic() >= deadline:
            raise AssertionError("action queue stayed busy")
        time.sleep(0.01)


@pytest.fixture()
def cockpit(monkeypatch, tmp_path: pathlib.Path):
    monkeypatch.setenv("RDM_UI_STATE_DIR", str(tmp_path / "ui-state"))
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    instance = server.build_server("127.0.0.1", _ephemeral_port())
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        thread.join(timeout=5)


def _session(cockpit: server.CockpitServer) -> str:
    token = auth.write_bootstrap()
    conn = http.client.HTTPConnection("127.0.0.1", cockpit.server_address[1], timeout=10)
    conn.request("GET", f"/?t={token}")
    response = conn.getresponse()
    cookie = response.getheader("Set-Cookie", "")
    response.read()
    conn.close()
    assert cookie.startswith("rdm_ui=")
    return cookie.split("=", 1)[1].split(";", 1)[0]


def _post(port: int, path: str, session: str, body: object, header_token: str | None = None):
    headers = {"Cookie": f"rdm_ui={session}"}
    if header_token is not None:
        headers["X-RDM-Token"] = header_token
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=headers)
    return conn


def test_action_cookie_only_post_is_forbidden(cockpit):
    port = cockpit.server_address[1]
    session = _session(cockpit)

    conn = _post(port, "/api/action/doctor", session, {})
    response = conn.getresponse()
    assert response.status == 403
    payload = json.load(response)
    conn.close()

    assert payload["error"]
    assert not cockpit.actions.busy()


def test_action_accepted_with_header_token(cockpit, monkeypatch):
    seen: list[str] = []

    def fake_use(name: str) -> int:
        seen.append(name)
        return 0

    monkeypatch.setattr(control, "apply_use", fake_use)
    port = cockpit.server_address[1]
    session = _session(cockpit)

    conn = _post(port, "/api/action/use", session, {"name": "demo"}, header_token=session)
    response = conn.getresponse()
    assert response.status == 202
    payload = json.load(response)
    conn.close()

    assert payload["action_id"]
    _wait_idle(cockpit)
    assert seen == ["demo"]


def test_action_conflict_while_queue_busy(cockpit, monkeypatch):
    release = threading.Event()

    def slow_doctor() -> int:
        release.wait(5)
        return 0

    monkeypatch.setattr(control, "run_doctor", slow_doctor)
    port = cockpit.server_address[1]
    session = _session(cockpit)

    conn = _post(port, "/api/action/doctor", session, {}, header_token=session)
    accepted = conn.getresponse()
    assert accepted.status == 202
    json.load(accepted)
    conn.close()

    conn = _post(port, "/api/action/doctor", session, {}, header_token=session)
    busy = conn.getresponse()
    assert busy.status == 409
    payload = json.load(busy)
    conn.close()

    assert payload["error"]
    release.set()
    _wait_idle(cockpit)


def test_action_unknown_verb_is_not_found(cockpit):
    port = cockpit.server_address[1]
    session = _session(cockpit)

    conn = _post(port, "/api/action/explode", session, {}, header_token=session)
    response = conn.getresponse()
    assert response.status == 404
    payload = json.load(response)
    conn.close()

    assert payload["error"]
    assert not cockpit.actions.busy()


def test_action_progress_streams_on_events(cockpit, monkeypatch):
    def fake_doctor() -> int:
        return 0

    monkeypatch.setattr(control, "run_doctor", fake_doctor)
    port = cockpit.server_address[1]
    session = _session(cockpit)

    conn = _post(port, "/api/action/doctor", session, {}, header_token=session)
    response = conn.getresponse()
    assert response.status == 202
    action_id = json.load(response)["action_id"]
    conn.close()
    _wait_idle(cockpit)

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/events", headers={"Cookie": f"rdm_ui={session}"})
    stream = conn.getresponse()
    events: list[dict] = []
    try:
        assert stream.status == 200
        while len(events) < 2:
            line = stream.readline()
            if not line:
                raise AssertionError("sse stream ended before action events")
            if line.startswith(b"data: "):
                events.append(json.loads(line[len(b"data: ") :]))
    finally:
        conn.close()

    assert [event["status"] for event in events] == ["started", "finished"]
    assert all(event["kind"] == "action" for event in events)
    assert all(event["job_id"] == action_id for event in events)
    assert events[-1]["exit"] == 0
