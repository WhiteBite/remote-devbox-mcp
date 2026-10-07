from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "arena" / "mcp-stdio-adapter.py"


def load_adapter():
    spec = importlib.util.spec_from_file_location("stdio_adapter_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


UPSTREAM_TOOLS = [
    {"name": "read", "description": "чтение файла", "inputSchema": {"type": "object"}},
    {"name": "edit", "description": "точечная замена", "inputSchema": {"type": "object"}},
    {"name": "bash", "description": "команды", "inputSchema": {"type": "object"}},
]


def job_response(job: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(job)}]}


def request(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    req = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        req["params"] = params
    return req


class FakeClient:
    def __init__(self, tools=None, responses=()):
        self.tools = UPSTREAM_TOOLS if tools is None else tools
        self.responses = list(responses)
        self.calls = []

    def initialize(self):
        return {"protocolVersion": "2025-06-18", "serverInfo": {}, "capabilities": {}}

    def list_tools(self):
        if isinstance(self.tools, Exception):
            raise self.tools
        return self.tools

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_initialize_returns_protocol_and_server_info():
    mod = load_adapter()
    resp = mod.handle(request("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {"roots": {}},
        "clientInfo": {"name": "opencode", "version": "1.0"},
    }), FakeClient(), trust=False, readonly=False)
    result = resp["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "devbox-bridge-adapter"
    assert result["capabilities"] == {"tools": {}}
    assert mod.CLIENT_CAPABILITIES == {"roots": {}}


def test_initialize_notes_elicitation_support(capsys):
    mod = load_adapter()
    mod.handle(request("initialize", {"capabilities": {"elicitation": {}}}),
               FakeClient(), trust=False, readonly=False)
    err = capsys.readouterr().err
    assert "elicitation" in err
    assert "--trust" in err


def test_initialized_notification_gets_no_response():
    mod = load_adapter()
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert mod.handle(note, FakeClient(), trust=False, readonly=False) is None


def test_tools_list_proxies_upstream_and_annotates_mutating():
    mod = load_adapter()
    client = FakeClient()
    resp = mod.handle(request("tools/list"), client, trust=False, readonly=False)
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert "read" in tools
    assert tools["read"]["mutating"] is False
    assert tools["edit"]["mutating"] is True
    assert tools["bash"]["mutating"] is True
    assert tools["edit"]["inputSchema"] == {"type": "object"}


def test_tools_list_falls_back_to_known_tools_when_upstream_down():
    mod = load_adapter()
    client = FakeClient(tools=RuntimeError("нет соединения"))
    resp = mod.handle(request("tools/list"), client, trust=False, readonly=False)
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    assert set(tools) == {
        "read", "write", "edit", "apply_patch", "glob", "grep", "bash",
        "webfetch", "todowrite", "lsp", "opencode_native_info",
        "opencode_job_list", "opencode_job_result", "opencode_job_cancel",
        "opencode_permissions_pending", "opencode_permission_reply",
    }
    assert tools["edit"]["mutating"] is True
    assert tools["webfetch"]["mutating"] is True
    assert tools["opencode_permission_reply"]["mutating"] is False


def test_tools_call_read_returns_plain_result_text():
    mod = load_adapter()
    client = FakeClient(responses=[
        {"content": [{"type": "text", "text": "line 1\nline 2"}]},
    ])
    resp = mod.handle(request("tools/call", {
        "name": "read", "arguments": {"filePath": "src/Main.java"}}),
        client, trust=False, readonly=False)
    assert resp["result"]["content"] == [{"type": "text", "text": "line 1\nline 2"}]
    assert resp["result"]["isError"] is False
    assert client.calls == [("read", {"filePath": "src/Main.java"})]


def test_tools_call_edit_settles_job_under_trust():
    mod = load_adapter()
    client = FakeClient(responses=[
        job_response({"job_id": "j-1", "status": "awaiting_permission",
                      "permission": {"id": "p-1", "permission": "edit",
                                     "patterns": ["src/Main.java"]}}),
        job_response({"job_id": "j-1", "status": "completed",
                      "result": {"output": "patched src/Main.java",
                                 "metadata": {"exit": 0}}}),
    ])
    resp = mod.handle(request("tools/call", {
        "name": "edit",
        "arguments": {"filePath": "src/Main.java", "oldString": "a", "newString": "b"}}),
        client, trust=True, readonly=False)
    assert resp["result"]["content"][0]["text"] == "patched src/Main.java"
    assert resp["result"]["isError"] is False
    assert client.calls[0][0] == "edit"
    assert client.calls[1] == ("opencode_permission_reply",
                               {"job_id": "j-1", "permission_id": "p-1", "reply": "once"})


def test_tools_call_edit_without_trust_reports_error():
    mod = load_adapter()
    client = FakeClient(responses=[
        job_response({"job_id": "j-2", "status": "awaiting_permission",
                      "permission": {"id": "p-2", "permission": "edit",
                                     "patterns": ["src/Main.java"]}}),
    ])
    resp = mod.handle(request("tools/call", {"name": "edit", "arguments": {}}),
                      client, trust=False, readonly=False)
    assert resp["result"]["isError"] is True
    assert "--trust" in resp["result"]["content"][0]["text"]
    assert client.calls == [("edit", {})]


def test_readonly_refuses_edit_without_upstream_call():
    mod = load_adapter()
    client = FakeClient()
    resp = mod.handle(request("tools/call", {"name": "edit", "arguments": {}}),
                      client, trust=False, readonly=True)
    assert resp["result"]["isError"] is True
    assert "readonly" in resp["result"]["content"][0]["text"]
    assert client.calls == []


def test_tools_call_upstream_failure_is_error_result():
    mod = load_adapter()
    client = FakeClient(responses=[RuntimeError("HTTP 503: туннель недоступен")])
    resp = mod.handle(request("tools/call", {"name": "read", "arguments": {}}),
                      client, trust=False, readonly=False)
    assert resp["result"]["isError"] is True
    assert "503" in resp["result"]["content"][0]["text"]


def test_unknown_method_returns_method_not_found():
    mod = load_adapter()
    resp = mod.handle(request("resources/list"), FakeClient(),
                      trust=False, readonly=False)
    assert resp["error"]["code"] == -32601
    assert resp["id"] == 1


def elicit_recorder(action: str):
    calls = []

    def elicit(message, requested_schema):
        calls.append((message, requested_schema))
        return {"action": action}

    return elicit, calls


def run_serve(mod, client, messages, *, trust=False, readonly=False,
              monkeypatch, capsys):
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    monkeypatch.setattr(sys, "stdin", stdin)
    mod.serve(client, trust=trust, readonly=readonly)
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


AWAITING_EDIT = {
    "job_id": "j-1", "status": "awaiting_permission",
    "permission": {"id": "p-1", "permission": "edit", "patterns": ["src/Main.java"]},
}


def test_tools_call_edit_elicits_and_accepts():
    mod = load_adapter()
    mod.handle(request("initialize", {"capabilities": {"elicitation": {}}}),
               FakeClient(), trust=False, readonly=False)
    client = FakeClient(responses=[
        job_response(AWAITING_EDIT),
        job_response({"job_id": "j-1", "status": "completed",
                      "result": {"output": "patched src/Main.java",
                                 "metadata": {"exit": 0}}}),
    ])
    elicit, calls = elicit_recorder("accept")
    resp = mod.handle(request("tools/call", {"name": "edit", "arguments": {}}),
                      client, trust=False, readonly=False, elicit=elicit)
    assert resp["result"]["isError"] is False
    assert resp["result"]["content"][0]["text"] == "patched src/Main.java"
    assert client.calls[1] == ("opencode_permission_reply",
                               {"job_id": "j-1", "permission_id": "p-1",
                                "reply": "once"})
    assert len(calls) == 1
    assert "edit" in calls[0][0]
    assert "src/Main.java" in calls[0][0]
    assert calls[0][1]["type"] == "object"


@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_tools_call_edit_elicits_and_declines(action):
    mod = load_adapter()
    mod.handle(request("initialize", {"capabilities": {"elicitation": {}}}),
               FakeClient(), trust=False, readonly=False)
    client = FakeClient(responses=[
        job_response(AWAITING_EDIT),
        job_response({"job_id": "j-1", "status": "cancelled"}),
    ])
    elicit, _ = elicit_recorder(action)
    resp = mod.handle(request("tools/call", {"name": "edit", "arguments": {}}),
                      client, trust=False, readonly=False, elicit=elicit)
    assert resp["result"]["isError"] is True
    assert "отклонена" in resp["result"]["content"][0]["text"]
    assert client.calls[1] == ("opencode_permission_reply",
                               {"job_id": "j-1", "permission_id": "p-1",
                                "reply": "reject"})


def test_tools_call_edit_without_elicitation_capability_keeps_trust_error():
    mod = load_adapter()
    mod.handle(request("initialize", {"capabilities": {"roots": {}}}),
               FakeClient(), trust=False, readonly=False)
    client = FakeClient(responses=[job_response(AWAITING_EDIT)])
    elicit, calls = elicit_recorder("accept")
    resp = mod.handle(request("tools/call", {"name": "edit", "arguments": {}}),
                      client, trust=False, readonly=False, elicit=elicit)
    assert resp["result"]["isError"] is True
    assert "--trust" in resp["result"]["content"][0]["text"]
    assert calls == []
    assert client.calls == [("edit", {})]


def test_serve_elicitation_accept_roundtrip(monkeypatch, capsys):
    mod = load_adapter()
    client = FakeClient(responses=[
        job_response(AWAITING_EDIT),
        job_response({"job_id": "j-1", "status": "completed",
                      "result": {"output": "patched src/Main.java",
                                 "metadata": {"exit": 0}}}),
    ])
    out = run_serve(mod, client, [
        request("initialize", {"capabilities": {"elicitation": {}}}, req_id=1),
        request("tools/call", {"name": "edit", "arguments": {}}, req_id=2),
        {"jsonrpc": "2.0", "id": "elicit-1", "result": {"action": "accept"}},
    ], monkeypatch=monkeypatch, capsys=capsys)
    assert [m.get("method") for m in out] == [None, "elicitation/create", None]
    elicit_req = out[1]
    assert elicit_req["id"] == "elicit-1"
    assert "edit" in elicit_req["params"]["message"]
    assert "src/Main.java" in elicit_req["params"]["message"]
    assert elicit_req["params"]["requestedSchema"]["type"] == "object"
    assert out[2]["id"] == 2
    assert out[2]["result"]["isError"] is False
    assert out[2]["result"]["content"][0]["text"] == "patched src/Main.java"
    assert client.calls[1] == ("opencode_permission_reply",
                               {"job_id": "j-1", "permission_id": "p-1",
                                "reply": "once"})


def test_serve_elicitation_decline_roundtrip(monkeypatch, capsys):
    mod = load_adapter()
    client = FakeClient(responses=[
        job_response(AWAITING_EDIT),
        job_response({"job_id": "j-1", "status": "cancelled"}),
    ])
    out = run_serve(mod, client, [
        request("initialize", {"capabilities": {"elicitation": {}}}, req_id=1),
        request("tools/call", {"name": "edit", "arguments": {}}, req_id=2),
        {"jsonrpc": "2.0", "id": "elicit-1", "result": {"action": "decline"}},
    ], monkeypatch=monkeypatch, capsys=capsys)
    assert out[1]["method"] == "elicitation/create"
    assert out[2]["id"] == 2
    assert out[2]["result"]["isError"] is True
    assert "отклонена" in out[2]["result"]["content"][0]["text"]
    assert client.calls[1] == ("opencode_permission_reply",
                               {"job_id": "j-1", "permission_id": "p-1",
                                "reply": "reject"})


def test_serve_trust_skips_elicitation(monkeypatch, capsys):
    mod = load_adapter()
    client = FakeClient(responses=[
        job_response(AWAITING_EDIT),
        job_response({"job_id": "j-1", "status": "completed",
                      "result": {"output": "patched", "metadata": {"exit": 0}}}),
    ])
    out = run_serve(mod, client, [
        request("initialize", {"capabilities": {"elicitation": {}}}, req_id=1),
        request("tools/call", {"name": "edit", "arguments": {}}, req_id=2),
    ], trust=True, monkeypatch=monkeypatch, capsys=capsys)
    assert [m.get("id") for m in out] == [1, 2]
    assert all("method" not in m for m in out)
    assert out[1]["result"]["isError"] is False


def test_serve_handles_requests_while_elicitation_pending(monkeypatch, capsys):
    mod = load_adapter()
    client = FakeClient(responses=[
        job_response(AWAITING_EDIT),
        job_response({"job_id": "j-1", "status": "completed",
                      "result": {"output": "patched", "metadata": {"exit": 0}}}),
    ])
    out = run_serve(mod, client, [
        request("initialize", {"capabilities": {"elicitation": {}}}, req_id=1),
        request("tools/call", {"name": "edit", "arguments": {}}, req_id=2),
        request("tools/list", req_id=3),
        {"jsonrpc": "2.0", "id": "elicit-1", "result": {"action": "accept"}},
    ], monkeypatch=monkeypatch, capsys=capsys)
    assert [m.get("id") for m in out] == [1, "elicit-1", 3, 2]
    assert out[1]["method"] == "elicitation/create"
    assert [t["name"] for t in out[2]["result"]["tools"]][0] == "read"
    assert out[3]["result"]["isError"] is False


def test_serve_elicitation_error_response_fails_closed(monkeypatch, capsys):
    mod = load_adapter()
    client = FakeClient(responses=[job_response(AWAITING_EDIT)])
    out = run_serve(mod, client, [
        request("initialize", {"capabilities": {"elicitation": {}}}, req_id=1),
        request("tools/call", {"name": "edit", "arguments": {}}, req_id=2),
        {"jsonrpc": "2.0", "id": "elicit-1",
         "error": {"code": -32601, "message": "elicitation не поддержан"}},
    ], monkeypatch=monkeypatch, capsys=capsys)
    assert out[1]["method"] == "elicitation/create"
    assert out[2]["id"] == 2
    assert out[2]["result"]["isError"] is True
    assert "elicitation" in out[2]["result"]["content"][0]["text"]
    assert client.calls == [("edit", {})]
