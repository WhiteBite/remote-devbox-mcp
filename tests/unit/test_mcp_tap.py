from __future__ import annotations

import json

from rdm.proxy import mcp_tap

CAP = 1024


def _rpc(method: str, params: dict | None = None, rpc_id: int | None = 7) -> bytes:
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    if rpc_id is not None:
        message["id"] = rpc_id
    return json.dumps(message).encode()


def _wire(body: bytes, status: bytes = b"200 OK") -> bytes:
    return (
        b"HTTP/1.1 "
        + status
        + b"\r\nContent-Type: application/json\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


def _job_response(job: dict, rpc_id: int = 7) -> bytes:
    message = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {"content": [{"type": "text", "text": json.dumps(job)}], "isError": False},
    }
    return _wire(json.dumps(message).encode())


def test_parse_request_initialize() -> None:
    body = _rpc("initialize", {"protocolVersion": "2025-06-18"})
    meta = mcp_tap.parse_request(body, CAP)
    assert meta == {"rpc_method": "initialize", "rpc_id": 7, "bytes": len(body)}


def test_parse_request_tools_list() -> None:
    body = _rpc("tools/list", {})
    meta = mcp_tap.parse_request(body, CAP)
    assert meta == {"rpc_method": "tools/list", "rpc_id": 7, "bytes": len(body)}


def test_parse_request_tools_call_extracts_tool() -> None:
    body = _rpc("tools/call", {"name": "bash", "arguments": {"command": "ls"}})
    meta = mcp_tap.parse_request(body, CAP)
    assert meta == {"rpc_method": "tools/call", "tool": "bash", "rpc_id": 7, "bytes": len(body)}
    assert "arguments" not in meta


def test_parse_request_notification_has_no_rpc_id() -> None:
    body = _rpc("notifications/initialized", rpc_id=None)
    meta = mcp_tap.parse_request(body, CAP)
    assert meta == {"rpc_method": "notifications/initialized", "bytes": len(body)}


def test_parse_request_malformed_json_unparsed() -> None:
    body = b"{not json"
    meta = mcp_tap.parse_request(body, CAP)
    assert meta == {"bytes": len(body), "unparsed": True}


def test_parse_request_over_cap_truncated() -> None:
    body = _rpc("tools/call", {"name": "bash"})
    meta = mcp_tap.parse_request(body, 10)
    assert meta == {"bytes": len(body), "truncated": True}


def test_parse_request_at_cap_not_truncated() -> None:
    body = _rpc("tools/call", {"name": "bash"})
    meta = mcp_tap.parse_request(body, len(body))
    assert meta is not None and meta.get("rpc_method") == "tools/call"


def test_parse_request_empty_body_none() -> None:
    assert mcp_tap.parse_request(b"", CAP) is None


def test_parse_request_non_object_json_unparsed() -> None:
    body = b"[1, 2]"
    meta = mcp_tap.parse_request(body, CAP)
    assert meta == {"bytes": len(body), "unparsed": True}


def test_parse_request_object_without_method_unparsed() -> None:
    body = b'{"jsonrpc": "2.0", "result": {}}'
    meta = mcp_tap.parse_request(body, CAP)
    assert meta == {"bytes": len(body), "unparsed": True}


def test_parse_response_jobview_metadata_only() -> None:
    job = {
        "job_id": "job-1",
        "status": "completed",
        "result": {"output": "sensitive-output", "metadata": {"exit": 0}},
    }
    raw = _job_response(job)
    meta = mcp_tap.parse_response(raw, CAP)
    assert meta is not None
    assert meta["job_id"] == "job-1"
    assert meta["status"] == "completed"
    assert meta["exit"] == 0
    assert meta["bytes"] == len(raw)
    assert "output" not in meta
    assert "result" not in meta


def test_parse_response_permission_type_extracted() -> None:
    job = {
        "job_id": "job-2",
        "status": "awaiting_permission",
        "permission": {"id": "perm-9", "permission": "bash", "patterns": ["/project/**"]},
    }
    meta = mcp_tap.parse_response(_job_response(job), CAP)
    assert meta is not None
    assert meta["permission"] == "bash"
    assert "perm-9" not in meta.values()
    assert "patterns" not in meta


def test_parse_response_over_cap_truncated() -> None:
    raw = _job_response({"job_id": "job-3", "status": "completed"})
    meta = mcp_tap.parse_response(raw, 10)
    assert meta == {"bytes": len(raw), "truncated": True}


def test_parse_response_malformed_unparsed() -> None:
    raw = _wire(b"{not json")
    meta = mcp_tap.parse_response(raw, CAP)
    assert meta == {"bytes": len(raw), "unparsed": True}


def test_parse_response_plain_result_none() -> None:
    message = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "plain"}]}}
    assert mcp_tap.parse_response(_wire(json.dumps(message).encode()), CAP) is None


def test_parse_response_empty_none() -> None:
    assert mcp_tap.parse_response(b"", CAP) is None


def test_parse_response_body_without_head() -> None:
    job = {"job_id": "job-4", "status": "running"}
    message = {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": json.dumps(job)}]}}
    meta = mcp_tap.parse_response(json.dumps(message).encode(), CAP)
    assert meta is not None
    assert meta["job_id"] == "job-4"
    assert meta["status"] == "running"
