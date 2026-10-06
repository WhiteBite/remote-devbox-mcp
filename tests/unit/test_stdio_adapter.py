from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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


def test_initialize_logs_elicitation_limitation(capsys):
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
        "read", "write", "edit", "apply_patch", "glob", "grep", "bash", "lsp",
        "todowrite", "opencode_permission_reply", "opencode_job_result",
    }
    assert tools["edit"]["mutating"] is True
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
