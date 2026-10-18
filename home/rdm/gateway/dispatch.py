"""Gateway tools/call dispatch: resource_link filtering, status mapping, audit.

call() times the session call, strips resource_link content blocks from the
result, and emits one metadata-only event (kind="gateway") through
rdm.events.sink: session/server/tool/rpc_id/status/latency_ms/bytes/truncated
only — never arguments or result payloads; string values are masked via
rdm.redact.redact_text.
"""

from __future__ import annotations

import json
import time
from typing import Any

from rdm.events import sink
from rdm.gateway.server import CallResult, Session
from rdm.redact import redact_text

_DENY_REASONS = frozenset(
    ("tool-not-allowed", "meta-tool-deny", "default-deny", "empty-allowlist", "arg-outside-project")
)
_UNAVAILABLE_EXACT = ("unknown tool", "unknown-tool")
_UNAVAILABLE_PREFIX = ("server unavailable", "server-unavailable")


def strip_resource_links(content: list[Any]) -> list[Any]:
    return [item for item in content if not (isinstance(item, dict) and item.get("type") == "resource_link")]


def audit(
    session_id: str,
    server: str,
    tool: str,
    rpc_id: Any,
    status: str,
    latency_ms: int,
    bytes_out: int,
    truncated: bool,
) -> None:
    event: dict[str, Any] = {
        "kind": "gateway",
        "session": session_id,
        "server": server,
        "tool": tool,
        "rpc_id": rpc_id,
        "status": status,
        "latency_ms": latency_ms,
        "bytes": bytes_out,
        "truncated": truncated,
    }
    sink.emit({key: redact_text(value) if isinstance(value, str) else value for key, value in event.items()})


def call(
    session: Session,
    session_id: str,
    exposed_name: str,
    arguments: dict[str, Any],
    progress_token: Any,
    rpc_id: Any,
) -> CallResult:
    started = time.monotonic()
    result: CallResult = session.call_tool(exposed_name, arguments, progress_token)
    latency_ms = int((time.monotonic() - started) * 1000)
    content = strip_resource_links(result.content)
    server, _, tool = exposed_name.partition("__")
    audit(
        session_id,
        server,
        tool,
        rpc_id,
        _status(result),
        latency_ms,
        len(json.dumps(content, ensure_ascii=False)),
        result.truncated,
    )
    return CallResult(content=content, is_error=result.is_error, truncated=result.truncated)


def _status(result: CallResult) -> str:
    if not result.is_error:
        return "ok"
    reason = ""
    for item in result.content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            reason = item["text"]
            break
    if reason in _DENY_REASONS:
        return "denied"
    if reason in _UNAVAILABLE_EXACT or reason.startswith(_UNAVAILABLE_PREFIX):
        return "unavailable"
    return "error"
