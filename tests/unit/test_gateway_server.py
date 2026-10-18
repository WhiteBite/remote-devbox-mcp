from __future__ import annotations

import http.client
import json
import queue
import socket
import threading
import time

import pytest
from rdm.gateway import server

DEFAULT_TOOLS = [("websearch__search", "websearch", "search", {"type": "object"})]


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
            time.sleep(0.05)
    raise AssertionError(f"gateway on 127.0.0.1:{port} never listened")


class FakeSession:
    def __init__(self, sid: str, tools: list | None = None) -> None:
        self.id = sid
        self.server_info_name = "gateway"
        self.instructions = "tools are namespaced as <server>__<tool>"
        self.capabilities = {"tools": {}}
        self.tools = list(tools if tools is not None else DEFAULT_TOOLS)
        self.notifications: queue.Queue = queue.Queue()
        self.calls: list[tuple] = []
        self.cancelled: list[object] = []
        self.closed = False
        self.call_result = server.CallResult(content=[{"type": "text", "text": "ok"}], is_error=False)
        self.call_delay = 0.0

    def list_tools(self) -> list:
        return list(self.tools)

    def call_tool(self, exposed_name, arguments, progress_token=None):
        self.calls.append((exposed_name, arguments, progress_token))
        if self.call_delay:
            time.sleep(self.call_delay)
        return self.call_result

    def cancel(self, rpc_id) -> None:
        self.cancelled.append(rpc_id)

    def close(self) -> None:
        self.closed = True


class FakeManager:
    def __init__(self, limit: int = 4) -> None:
        self.sessions: dict[str, FakeSession] = {}
        self.limit = limit
        self._counter = 0

    def create_session(self) -> FakeSession:
        live = [session for session in self.sessions.values() if not session.closed]
        if len(live) >= self.limit:
            raise server.SessionLimitError("session limit reached")
        self._counter += 1
        session = FakeSession(f"sess-{self._counter}")
        self.sessions[session.id] = session
        return session

    def get(self, session_id: str) -> FakeSession | None:
        session = self.sessions.get(session_id)
        if session is not None and session.closed:
            return None
        return session


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data

    def post_json(self, message: dict, headers: dict | None = None):
        body = json.dumps(message).encode("utf-8")
        merged = {"Content-Type": "application/json", **(headers or {})}
        return self.request("POST", "/mcp", body, merged)

    def initialize(self, headers: dict | None = None):
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        }
        status, response_headers, data = self.post_json(message, headers)
        return status, response_headers, json.loads(data)


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    manager = FakeManager()
    httpd = server.build_server("127.0.0.1", _ephemeral_port(), lambda: manager)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_listening(httpd.server_address[1])
    try:
        yield Client(httpd.server_address[1]), manager, httpd
    finally:
        httpd.stop()
        thread.join(timeout=5)


def _session(gateway) -> tuple[FakeSession, str]:
    client, manager, _ = gateway
    status, headers, payload = client.initialize()
    assert status == 200
    sid = headers["Mcp-Session-Id"]
    return manager.sessions[sid], sid


def _tools_call(client: Client, sid: str, name: str, rpc_id: int = 3, arguments: dict | None = None):
    message = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": {} if arguments is None else arguments},
    }
    return client.post_json(message, {"Mcp-Session-Id": sid})


def test_healthz(gateway):
    client, _, _ = gateway
    status, _, body = client.request("GET", "/healthz")
    assert status == 200
    assert body == b"ok\n"


def test_unknown_path_404(gateway):
    client, _, _ = gateway
    assert client.request("GET", "/nope")[0] == 404
    assert client.request("POST", "/nope", b"{}", {"Content-Type": "application/json"})[0] == 404


def test_build_server_refuses_non_loopback_host():
    with pytest.raises(ValueError):
        server.build_server("0.0.0.0", _ephemeral_port(), lambda: FakeManager())


def test_initialize_result_shape_and_session_header(gateway):
    client, manager, _ = gateway
    status, headers, payload = client.initialize()
    assert status == 200
    sid = headers["Mcp-Session-Id"]
    session = manager.sessions[sid]
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == 1
    result = payload["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "gateway"
    assert result["instructions"] == session.instructions


@pytest.mark.parametrize("version", ["2025-03-26", "2025-06-18"])
def test_initialize_supported_version_echoed(gateway, version):
    client, _, _ = gateway
    status, _, payload = client.initialize({"MCP-Protocol-Version": version})
    assert status == 200
    assert payload["result"]["protocolVersion"] == version


def test_initialize_unsupported_version_rejected(gateway):
    client, _, _ = gateway
    status, headers, payload = client.initialize({"MCP-Protocol-Version": "2024-11-05"})
    assert status == 400
    assert payload["error"]["code"] == -32000
    assert headers["MCP-Protocol-Version"] == "2025-03-26"


def test_unsupported_version_rejected_before_session_lookup(gateway):
    client, _, _ = gateway
    _session(gateway)
    message = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    status, _, _ = client.post_json(message, {"Mcp-Session-Id": "whatever", "MCP-Protocol-Version": "1999-01-01"})
    assert status == 400


def test_session_limit_maps_to_429(gateway):
    client, _, _ = gateway
    for _ in range(4):
        assert client.initialize()[0] == 200
    status, _, payload = client.initialize()
    assert status == 429
    assert payload["error"]["code"] == -32000


def test_missing_session_header_on_non_initialize(gateway):
    client, _, _ = gateway
    status, _, data = client.post_json({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    assert status == 400
    payload = json.loads(data)
    assert payload["error"]["code"] == -32000
    assert payload["id"] == 5


def test_unknown_session_id_404(gateway):
    client, _, _ = gateway
    status, _, data = client.post_json({"jsonrpc": "2.0", "id": 6, "method": "tools/list"}, {"Mcp-Session-Id": "ghost"})
    assert status == 404
    assert json.loads(data)["error"]["code"] == -32001


def test_parse_error_32700(gateway):
    client, _, _ = gateway
    status, _, data = client.request("POST", "/mcp", b"{oops", {"Content-Type": "application/json"})
    assert status == 400
    assert json.loads(data)["error"]["code"] == -32700


def test_batch_request_rejected(gateway):
    client, _, _ = gateway
    body = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]).encode("utf-8")
    status, _, data = client.request("POST", "/mcp", body, {"Content-Type": "application/json"})
    assert status == 400
    assert json.loads(data)["error"]["code"] == -32600


def test_unknown_method_32601(gateway):
    client, _, _ = gateway
    _, sid = _session(gateway)
    status, _, data = client.post_json({"jsonrpc": "2.0", "id": 7, "method": "resources/list"}, {"Mcp-Session-Id": sid})
    assert status == 200
    assert json.loads(data)["error"]["code"] == -32601


def test_ping(gateway):
    client, _, _ = gateway
    _, sid = _session(gateway)
    status, _, data = client.post_json({"jsonrpc": "2.0", "id": 8, "method": "ping"}, {"Mcp-Session-Id": sid})
    assert status == 200
    assert json.loads(data)["result"] == {}


def test_initialized_notification_accepted_202(gateway):
    client, _, _ = gateway
    _, sid = _session(gateway)
    status, _, body = client.post_json({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"Mcp-Session-Id": sid})
    assert status == 202
    assert body == b""


def test_cancelled_notification_routes_to_session_cancel(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    message = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 42}}
    status, _, _ = client.post_json(message, {"Mcp-Session-Id": sid})
    assert status == 202
    assert session.cancelled == [42]


def test_tools_list_namespaced_defs_verbatim_schemas(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
        "additionalProperties": False,
    }
    session.tools = [
        ("websearch__search", "websearch", "search", schema),
        ("codegraph__codegraph_explore", "codegraph", "codegraph_explore", {"type": "object"}),
    ]
    status, _, data = client.post_json({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"Mcp-Session-Id": sid})
    assert status == 200
    result = json.loads(data)["result"]
    assert result["tools"] == [
        {"name": "websearch__search", "inputSchema": schema},
        {"name": "codegraph__codegraph_explore", "inputSchema": {"type": "object"}},
    ]


def test_tools_call_routes_to_session_and_returns_content(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    message = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "websearch__search", "arguments": {"q": "hi"}, "_meta": {"progressToken": "tok-7"}},
    }
    status, _, data = client.post_json(message, {"Mcp-Session-Id": sid})
    assert status == 200
    result = json.loads(data)["result"]
    assert result == {"content": [{"type": "text", "text": "ok"}], "isError": False}
    assert session.calls == [("websearch__search", {"q": "hi"}, "tok-7")]


def test_unknown_tool_32602_not_routed(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    status, _, data = _tools_call(client, sid, "nope__nope")
    assert status == 200
    assert json.loads(data)["error"]["code"] == -32602
    assert session.calls == []


def test_upstream_crash_empty_is_error_maps_to_32603(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    session.call_result = server.CallResult(content=[], is_error=True)
    status, _, data = _tools_call(client, sid, "websearch__search")
    assert status == 200
    assert json.loads(data)["error"]["code"] == -32603


def test_policy_denial_returned_as_is_error_tool_result(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    session.call_result = server.CallResult(content=[{"type": "text", "text": "tool-not-allowed"}], is_error=True)
    status, _, data = _tools_call(client, sid, "websearch__search")
    assert status == 200
    payload = json.loads(data)
    assert "error" not in payload
    assert payload["result"] == {"content": [{"type": "text", "text": "tool-not-allowed"}], "isError": True}


def test_tools_call_timeout_32603_and_cancel(gateway, monkeypatch):
    client, _, _ = gateway
    session, sid = _session(gateway)
    monkeypatch.setattr(server, "CALL_TIMEOUT_SECONDS", 0.2)
    session.call_delay = 1.5
    status, _, data = _tools_call(client, sid, "websearch__search", rpc_id=9)
    assert status == 200
    assert json.loads(data)["error"]["code"] == -32603
    assert session.cancelled == [9]


def test_tools_call_caps_single_text_item(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    session.call_result = server.CallResult(content=[{"type": "text", "text": "x" * 40000}], is_error=False)
    status, _, data = _tools_call(client, sid, "websearch__search")
    assert status == 200
    result = json.loads(data)["result"]
    assert len(result["content"][0]["text"]) == 32000
    assert result["_meta"] == {"truncated": True}


def test_tools_call_caps_total_result_size(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    session.call_result = server.CallResult(
        content=[
            {"type": "text", "text": "a" * 30000},
            {"type": "text", "text": "b" * 30000},
            {"type": "text", "text": "c" * 30000},
        ],
        is_error=False,
    )
    status, _, data = _tools_call(client, sid, "websearch__search")
    assert status == 200
    result = json.loads(data)["result"]
    assert len(json.dumps(result["content"])) <= 64000
    assert result["content"][0]["text"] == "a" * 30000
    assert result["_meta"]["truncated"] is True


def test_session_truncated_flag_propagates_to_meta(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    session.call_result = server.CallResult(content=[{"type": "text", "text": "ok"}], is_error=False, truncated=True)
    status, _, data = _tools_call(client, sid, "websearch__search")
    assert status == 200
    result = json.loads(data)["result"]
    assert result["content"] == [{"type": "text", "text": "ok"}]
    assert result["_meta"] == {"truncated": True}


def test_delete_session_200_and_closes(gateway):
    client, manager, _ = gateway
    session, sid = _session(gateway)
    status, _, _ = client.request("DELETE", "/mcp", headers={"Mcp-Session-Id": sid})
    assert status == 200
    assert session.closed


def test_delete_missing_session_header_400(gateway):
    client, _, _ = gateway
    assert client.request("DELETE", "/mcp")[0] == 400


def test_delete_unknown_session_404(gateway):
    client, _, _ = gateway
    status, _, data = client.request("DELETE", "/mcp", headers={"Mcp-Session-Id": "ghost"})
    assert status == 404
    assert json.loads(data)["error"]["code"] == -32001


def test_post_after_delete_is_stale(gateway):
    client, _, _ = gateway
    _, sid = _session(gateway)
    client.request("DELETE", "/mcp", headers={"Mcp-Session-Id": sid})
    status, _, data = client.post_json({"jsonrpc": "2.0", "id": 10, "method": "tools/list"}, {"Mcp-Session-Id": sid})
    assert status == 404
    assert json.loads(data)["error"]["code"] == -32001


def test_sse_stream_fan_in_and_end_on_session_close(gateway):
    client, _, _ = gateway
    session, sid = _session(gateway)
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    conn.request("GET", "/mcp", headers={"Mcp-Session-Id": sid})
    response = conn.getresponse()
    try:
        assert response.status == 200
        assert response.getheader("Content-Type", "").startswith("text/event-stream")
        assert not any(name.lower().startswith("last-event") for name, _ in response.getheaders())
        first = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "one"}}
        second = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "two"}}
        session.notifications.put(first)
        session.notifications.put(second)
        assert response.readline() == b"event: message\n"
        assert json.loads(response.readline()[len(b"data: ") :]) == first
        assert response.readline() == b"\n"
        assert response.readline() == b"event: message\n"
        assert json.loads(response.readline()[len(b"data: ") :]) == second
        assert response.readline() == b"\n"
        status, _, _ = client.request("DELETE", "/mcp", headers={"Mcp-Session-Id": sid})
        assert status == 200
        assert session.closed
        assert response.read() == b""
    finally:
        conn.close()


def test_second_sse_stream_for_same_session_rejected(gateway):
    client, _, _ = gateway
    _, sid = _session(gateway)
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    conn.request("GET", "/mcp", headers={"Mcp-Session-Id": sid})
    response = conn.getresponse()
    try:
        assert response.status == 200
        status, _, _ = client.request("GET", "/mcp", headers={"Mcp-Session-Id": sid})
        assert status == 409
    finally:
        conn.close()


def test_sse_missing_session_header_400(gateway):
    client, _, _ = gateway
    assert client.request("GET", "/mcp")[0] == 400


def test_sse_unknown_session_404(gateway):
    client, _, _ = gateway
    status, _, data = client.request("GET", "/mcp", headers={"Mcp-Session-Id": "ghost"})
    assert status == 404
    assert json.loads(data)["error"]["code"] == -32001


def test_gateway_token_auth(gateway, monkeypatch):
    client, _, _ = gateway
    monkeypatch.setenv("GATEWAY_TOKEN", "sekrit-token")
    assert client.initialize()[0] == 401
    assert client.initialize({"Authorization": "Bearer wrong"})[0] == 401
    status, headers, data = client.initialize({"Authorization": "Bearer sekrit-token"})
    assert status == 200
    assert "Mcp-Session-Id" in headers
    status, _, _ = client.request("GET", "/healthz")
    assert status == 200
    status, _, _ = client.request("GET", "/mcp", headers={"Mcp-Session-Id": headers["Mcp-Session-Id"]})
    assert status == 401


def test_stop_closes_all_sessions(gateway):
    client, manager, httpd = gateway
    _, sid1 = _session(gateway)
    _, sid2 = _session(gateway)
    httpd.stop()
    assert manager.sessions[sid1].closed
    assert manager.sessions[sid2].closed


def test_healthz_and_initialize_without_session_layer(monkeypatch):
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)

    def broken_factory():
        raise ImportError("session layer not landed yet")

    httpd = server.build_server("127.0.0.1", _ephemeral_port(), broken_factory)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_listening(httpd.server_address[1])
    try:
        client = Client(httpd.server_address[1])
        status, _, body = client.request("GET", "/healthz")
        assert status == 200
        assert body == b"ok\n"
        status, _, payload = client.initialize()
        assert status == 200
        assert payload["error"]["code"] == -32603
    finally:
        httpd.stop()
        thread.join(timeout=5)
