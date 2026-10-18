from __future__ import annotations

import json
import threading

import pytest
from rdm.events import sink
from rdm.gateway import dispatch, server
from rdm.gateway.server import CallResult

from tests.unit.test_gateway_server import Client, FakeManager, _ephemeral_port, _wait_listening


class StubSession:
    def __init__(self, result: CallResult) -> None:
        self.id = "sess-1"
        self.result = result
        self.calls: list[tuple] = []

    def call_tool(self, exposed_name, arguments, progress_token=None):
        self.calls.append((exposed_name, arguments, progress_token))
        return self.result


@pytest.fixture()
def emitted(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(sink, "emit", events.append)
    return events


def test_strip_resource_links_keeps_other_block_types():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "resource_link", "uri": "file:///notes", "name": "notes"},
        {"type": "image", "data": "b64", "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "file:///other"}},
    ]
    assert dispatch.strip_resource_links(content) == [
        {"type": "text", "text": "hello"},
        {"type": "image", "data": "b64", "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "file:///other"}},
    ]
    assert len(content) == 4


def test_strip_resource_links_empty_and_resource_link_only():
    assert dispatch.strip_resource_links([]) == []
    only = [{"type": "resource_link", "uri": "file:///notes"}]
    assert dispatch.strip_resource_links(only) == []


def test_call_strips_resource_links_and_preserves_flags(emitted):
    session = StubSession(
        CallResult(
            content=[
                {"type": "text", "text": "ok"},
                {"type": "resource_link", "uri": "file:///notes", "name": "notes"},
            ],
            is_error=False,
            truncated=True,
        )
    )
    result = dispatch.call(session, "sess-1", "websearch__search", {"q": "hi"}, "tok-1", 7)
    assert result.content == [{"type": "text", "text": "ok"}]
    assert result.is_error is False
    assert result.truncated is True
    assert session.calls == [("websearch__search", {"q": "hi"}, "tok-1")]


def test_call_emits_single_metadata_only_audit_event(emitted):
    content = [{"type": "text", "text": "ok"}]
    session = StubSession(CallResult(content=content, is_error=False))
    dispatch.call(session, "sess-1", "websearch__search", {"q": "hi"}, None, 7)
    assert len(emitted) == 1
    event = emitted[0]
    assert set(event) == {
        "kind",
        "session",
        "server",
        "tool",
        "rpc_id",
        "status",
        "latency_ms",
        "bytes",
        "truncated",
    }
    assert event["kind"] == "gateway"
    assert event["session"] == "sess-1"
    assert event["server"] == "websearch"
    assert event["tool"] == "search"
    assert event["rpc_id"] == 7
    assert event["status"] == "ok"
    assert event["latency_ms"] >= 0
    assert event["bytes"] == len(json.dumps(content, ensure_ascii=False))
    assert event["truncated"] is False


def test_audit_masks_token_like_server_and_tool(emitted):
    dispatch.audit("sess-1", "a" * 64, "f" * 64, 1, "ok", 5, 10, False)
    event = emitted[0]
    assert event["server"] == "[REDACTED]"
    assert event["tool"] == "[REDACTED]"
    assert event["session"] == "sess-1"
    assert event["status"] == "ok"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("tool-not-allowed", "denied"),
        ("meta-tool-deny", "denied"),
        ("default-deny", "denied"),
        ("empty-allowlist", "denied"),
        ("arg-outside-project", "denied"),
        ("server unavailable: websearch", "unavailable"),
        ("unknown tool", "unavailable"),
        ("upstream child died", "error"),
    ],
)
def test_call_status_matrix(emitted, text, status):
    session = StubSession(CallResult(content=[{"type": "text", "text": text}], is_error=True))
    dispatch.call(session, "sess-1", "websearch__search", {}, None, 1)
    assert emitted[0]["status"] == status


def test_call_error_without_text_content_audits_error(emitted):
    session = StubSession(CallResult(content=[], is_error=True))
    dispatch.call(session, "sess-1", "websearch__search", {}, None, 1)
    assert emitted[0]["status"] == "error"


def test_audit_persists_gateway_event_through_real_sink(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("RDM_EVENTS_PATH", str(path))
    dispatch.audit("sess-1", "websearch", "search", 3, "ok", 12, 34, False)
    obj = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert obj["kind"] == "gateway"
    assert obj["server"] == "websearch"
    assert obj["tool"] == "search"
    assert obj["status"] == "ok"
    assert obj["bytes"] == 34


def test_server_tools_call_routes_through_dispatch(monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(sink, "emit", events.append)
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    manager = FakeManager()
    httpd = server.build_server("127.0.0.1", _ephemeral_port(), lambda: manager)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_listening(httpd.server_address[1])
    try:
        client = Client(httpd.server_address[1])
        status, headers, _ = client.initialize()
        assert status == 200
        sid = headers["Mcp-Session-Id"]
        manager.sessions[sid].call_result = CallResult(
            content=[
                {"type": "text", "text": "ok"},
                {"type": "resource_link", "uri": "file:///notes", "name": "notes"},
            ],
            is_error=False,
        )
        message = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "websearch__search", "arguments": {"q": "hi"}},
        }
        status, _, data = client.post_json(message, {"Mcp-Session-Id": sid})
        assert status == 200
        assert json.loads(data)["result"] == {"content": [{"type": "text", "text": "ok"}], "isError": False}
        assert len(events) == 1
        event = events[0]
        assert event["kind"] == "gateway"
        assert event["session"] == sid
        assert event["server"] == "websearch"
        assert event["tool"] == "search"
        assert event["rpc_id"] == 3
        assert event["status"] == "ok"
    finally:
        httpd.stop()
        thread.join(timeout=5)
