"""Metadata-only JSONL event sink for the ingress proxy.

Fixed schema, one JSON object per line: ts, kind, session, port, method,
rpc_method, tool, rpc_id, job_id, status, permission, exit, latency_ms,
bytes, truncated, unparsed. Only these keys are ever persisted; payload
fields (args, output, tokens, file contents) are dropped on emit, and every
string value is masked through rdm.redact.redact_text before encoding.
Best-effort: OS and encode errors are swallowed, never raised into the proxy.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from typing import Any

from rdm.proxy import access_log
from rdm.redact import redact_text

_FIELDS = frozenset(
    (
        "ts",
        "kind",
        "session",
        "port",
        "method",
        "rpc_method",
        "tool",
        "rpc_id",
        "job_id",
        "status",
        "permission",
        "exit",
        "latency_ms",
        "bytes",
        "truncated",
        "unparsed",
    )
)


def default_events_path() -> pathlib.Path:
    override = os.environ.get("RDM_EVENTS_PATH")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(tempfile.gettempdir()) / "rdm-ingress" / "events.jsonl"


def emit(event: dict[str, Any]) -> None:
    try:
        clean = {
            key: redact_text(value) if isinstance(value, str) else value
            for key, value in event.items()
            if key in _FIELDS
        }
        access_log.append_text(default_events_path(), json.dumps(clean, ensure_ascii=False) + "\n")
    except (OSError, UnicodeError):
        pass
