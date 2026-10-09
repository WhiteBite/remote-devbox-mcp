from __future__ import annotations

import json
import socket

from rdm import ports
from rdm.proxy import event_tap, mcp_tap

from tests.proxy_fakes import FakeUpstream, raw_request, request, start_ingress


def _events(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _bridge_responder(job: dict):
    def respond(conn, method, path, pairs, body) -> None:
        message = {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {"content": [{"type": "text", "text": json.dumps(job)}], "isError": False},
        }
        payload = json.dumps(message).encode()
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )

    return respond


def _tools_call(name: str = "bash") -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"command": "echo secret-arg"}},
        }
    ).encode()


def _post(listen_port: int, route_port: int, body: bytes) -> bytes:
    return raw_request(
        listen_port,
        request("POST", f"/p/{route_port}/mcp", headers=[("Content-Length", str(len(body)))], body=body),
    )


def _start_bridge(tmp_path, monkeypatch, job: dict):
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("RDM_EVENTS_PATH", str(events_path))
    fake = FakeUpstream(_bridge_responder(job))
    monkeypatch.setattr(ports, "BRIDGE_PORT", fake.port)
    server, port = start_ingress(tmp_path, self_authed={fake.port})
    return fake, server, port, events_path


def test_bridge_tools_call_emits_request_and_response_events(tmp_path, monkeypatch):
    job = {
        "job_id": "job-1",
        "status": "completed",
        "result": {"output": "sensitive-output", "metadata": {"exit": 0}},
    }
    fake, server, port, events_path = _start_bridge(tmp_path, monkeypatch, job)
    try:
        out = _post(port, fake.port, _tools_call())
        assert b"200 OK" in out
        assert b"job-1" in out
        events = _events(events_path)
        assert [event["kind"] for event in events] == ["mcp_request", "mcp_response"]
        request_event = events[0]
        assert request_event["rpc_method"] == "tools/call"
        assert request_event["tool"] == "bash"
        assert request_event["rpc_id"] == 7
        assert request_event["port"] == fake.port
        assert request_event["method"] == "POST"
        assert request_event["bytes"] == len(_tools_call())
        response_event = events[1]
        assert response_event["job_id"] == "job-1"
        assert response_event["status"] == "completed"
        assert response_event["exit"] == 0
        assert response_event["bytes"] == len(out)
        raw = events_path.read_text(encoding="utf-8")
        assert "sensitive-output" not in raw
        assert "secret-arg" not in raw
    finally:
        fake.close()
        server.shutdown()


def test_bridge_tools_call_permission_event(tmp_path, monkeypatch):
    job = {
        "job_id": "job-2",
        "status": "awaiting_permission",
        "permission": {"id": "perm-9", "permission": "edit", "patterns": ["/project/**"]},
    }
    fake, server, port, events_path = _start_bridge(tmp_path, monkeypatch, job)
    try:
        out = _post(port, fake.port, _tools_call("edit"))
        assert b"200 OK" in out
        events = _events(events_path)
        response_event = events[1]
        assert response_event["job_id"] == "job-2"
        assert response_event["status"] == "awaiting_permission"
        assert response_event["permission"] == "edit"
        assert response_event["permission_id"] == "perm-9"
        raw = events_path.read_text(encoding="utf-8")
        assert "/project/**" not in raw
    finally:
        fake.close()
        server.shutdown()


def test_bridge_initialize_taps_request_only(tmp_path, monkeypatch):
    job = {"job_id": "job-3", "status": "completed"}
    fake, server, port, events_path = _start_bridge(tmp_path, monkeypatch, job)
    try:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ).encode()
        out = _post(port, fake.port, body)
        assert b"200 OK" in out
        events = _events(events_path)
        assert [event["kind"] for event in events] == ["mcp_request"]
        assert events[0]["rpc_method"] == "initialize"
    finally:
        fake.close()
        server.shutdown()


def test_non_bridge_port_post_not_tapped(tmp_path, monkeypatch):
    events_path = tmp_path / "events.jsonl"
    monkeypatch.setenv("RDM_EVENTS_PATH", str(events_path))
    fake = FakeUpstream(_bridge_responder({"job_id": "job-4", "status": "completed"}))
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request(
                "POST",
                f"/p/{fake.port}/mcp",
                headers=[("Authorization", "Bearer tok"), ("Content-Length", str(len(_tools_call())))],
                body=_tools_call(),
            ),
        )
        assert b"200 OK" in out
        assert not events_path.exists()
    finally:
        fake.close()
        server.shutdown()


def test_bridge_response_over_cap_marks_truncated(tmp_path, monkeypatch):
    job = {
        "job_id": "job-5",
        "status": "completed",
        "result": {"output": "x" * 500, "metadata": {"exit": 0}},
    }
    fake, server, port, events_path = _start_bridge(tmp_path, monkeypatch, job)
    monkeypatch.setattr(event_tap, "CAP", 200)
    try:
        out = _post(port, fake.port, _tools_call())
        assert b"200 OK" in out
        assert b"job-5" in out
        events = _events(events_path)
        assert [event["kind"] for event in events] == ["mcp_request", "mcp_response"]
        response_event = events[1]
        assert response_event["truncated"] is True
        assert response_event["bytes"] == len(out)
        assert "job_id" not in response_event
    finally:
        fake.close()
        server.shutdown()


def test_sabotaged_response_parser_call_survives(tmp_path, monkeypatch):
    job = {"job_id": "job-6", "status": "completed", "result": {"metadata": {"exit": 0}}}
    fake, server, port, events_path = _start_bridge(tmp_path, monkeypatch, job)

    def boom(raw: bytes, cap: int) -> dict | None:
        raise RuntimeError("sabotaged parser")

    monkeypatch.setattr(mcp_tap, "parse_response", boom)
    try:
        out = _post(port, fake.port, _tools_call())
        assert b"200 OK" in out
        assert b"job-6" in out
        events = _events(events_path)
        assert [event["kind"] for event in events] == ["mcp_request", "tap_error"]
        assert events[1]["port"] == fake.port
    finally:
        fake.close()
        server.shutdown()


def test_sabotaged_request_parser_call_survives(tmp_path, monkeypatch):
    job = {"job_id": "job-7", "status": "completed", "result": {"metadata": {"exit": 0}}}
    fake, server, port, events_path = _start_bridge(tmp_path, monkeypatch, job)

    def boom(body: bytes, cap: int) -> dict | None:
        raise RuntimeError("sabotaged parser")

    monkeypatch.setattr(mcp_tap, "parse_request", boom)
    try:
        out = _post(port, fake.port, _tools_call())
        assert b"200 OK" in out
        assert b"job-7" in out
        events = _events(events_path)
        assert [event["kind"] for event in events] == ["tap_error"]
        assert events[0]["port"] == fake.port
    finally:
        fake.close()
        server.shutdown()


def test_sabotaged_capture_never_breaks_forward(tmp_path, monkeypatch):
    job = {"job_id": "job-cap", "status": "completed", "result": {"metadata": {"exit": 0}}}
    fake, server, port, events_path = _start_bridge(tmp_path, monkeypatch, job)

    class exploding_buf:
        def __len__(self) -> int:
            raise RuntimeError("sabotaged capture")

        def __iadd__(self, other):
            raise RuntimeError("sabotaged capture")

    class sabotaged_tee(event_tap.TeeSocket):
        def __init__(self, sock, cap: int) -> None:
            super().__init__(sock, cap)
            self.buf = exploding_buf()

    monkeypatch.setattr(event_tap, "TeeSocket", sabotaged_tee)
    try:
        out = _post(port, fake.port, _tools_call())
        assert b"200 OK" in out
        assert b"job-cap" in out
    finally:
        fake.close()
        server.shutdown()


def test_bridge_tools_call_keep_alive_connection_reused(tmp_path, monkeypatch):
    job = {"job_id": "job-8", "status": "completed", "result": {"metadata": {"exit": 0}}}
    fake, server, port, events_path = _start_bridge(tmp_path, monkeypatch, job)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            for _ in range(2):
                rpc = _tools_call()
                sock.sendall(
                    request(
                        "POST",
                        f"/p/{fake.port}/mcp",
                        headers=[("Content-Length", str(len(rpc)))],
                        body=rpc,
                        connection="keep-alive",
                    )
                )
                buf = b""
                while b"job-8" not in buf:
                    buf += sock.recv(65536)
                assert b"200 OK" in buf
        events = _events(events_path)
        assert [event["kind"] for event in events] == ["mcp_request", "mcp_response"] * 2
    finally:
        fake.close()
        server.shutdown()
