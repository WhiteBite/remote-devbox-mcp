"""Pure MCP JSON-RPC metadata extraction for the ingress observability tap.

Both parsers take the bytes exactly as observed on the wire and return a
metadata dict, or None when there is nothing to observe (empty input, or a
response that carries no JobView). Oversized input is reported as
``truncated`` and undecodable input as ``unparsed`` — never as silence.
Payload fields (arguments, result.output) are never part of the result.
"""

from __future__ import annotations

import json
from typing import Any


def parse_request(body: bytes, cap: int) -> dict[str, Any] | None:
    """Extract JSON-RPC request metadata; None only for an empty body."""
    if not body:
        return None
    if len(body) > cap:
        return {"bytes": len(body), "truncated": True}
    try:
        message = json.loads(body)
    except ValueError:
        return {"bytes": len(body), "unparsed": True}
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return {"bytes": len(body), "unparsed": True}
    meta: dict[str, Any] = {"rpc_method": message["method"], "bytes": len(body)}
    rpc_id = message.get("id")
    if rpc_id is not None:
        meta["rpc_id"] = rpc_id
    if message["method"] == "tools/call":
        params = message.get("params")
        if isinstance(params, dict) and params.get("name") is not None:
            meta["tool"] = params["name"]
    return meta


def parse_response(raw: bytes, cap: int) -> dict[str, Any] | None:
    """Extract JobView metadata (job_id/status/permission/exit) from a response.

    ``raw`` is the response as sent to the client, HTTP head included; the head
    is stripped before decoding. Chunked bodies arrive still framed and decode
    as unparsed. None means: empty input, or a decodable response without a
    JobView.
    """
    if not raw:
        return None
    if len(raw) > cap:
        return {"bytes": len(raw), "truncated": True}
    payload = raw.partition(b"\r\n\r\n")[2] if b"\r\n\r\n" in raw else raw
    try:
        message = json.loads(payload)
    except ValueError:
        return {"bytes": len(raw), "unparsed": True}
    if not isinstance(message, dict):
        return {"bytes": len(raw), "unparsed": True}
    job = _jobview(message.get("result"))
    if job is None:
        return None
    meta: dict[str, Any] = {"bytes": len(raw), "job_id": job.get("job_id")}
    if job.get("status") is not None:
        meta["status"] = job["status"]
    permission = _permission_type(job.get("permission"))
    if permission is not None:
        meta["permission"] = permission
    exit_code = _exit_code(job.get("result"))
    if exit_code is not None:
        meta["exit"] = exit_code
    return meta


def _jobview(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    for block in result.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and "job_id" in data:
            return data
    return None


def _permission_type(permission: Any) -> str | None:
    if not isinstance(permission, dict):
        return None
    value = permission.get("permission") or permission.get("type")
    return value if isinstance(value, str) else None


def _exit_code(result: Any) -> Any:
    if not isinstance(result, dict):
        return None
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata.get("exit")
