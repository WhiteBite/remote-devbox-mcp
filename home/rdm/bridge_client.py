"""Streamable-HTTP MCP client for the devbox bridge, stdlib only.

Vendored from arena/mcp_client.py, cut to the control-plane needs: one
session per client, JSON or SSE responses, per-call timeout capped by an
optional shared command budget (``budget``) so a plugin command never blocks
the host RPC loop. Typed errors: ``BridgeUnreachable`` (no connection or
budget exhausted), ``BridgeDenied`` (401/403), ``BridgeError`` (tool-level
failure).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_URL = "http://127.0.0.1:8787/mcp"
CALL_TIMEOUT_SECONDS = 5.0
COMMAND_DEADLINE_SECONDS = 5.5


class BridgeError(RuntimeError):
    """Bridge tool call failed (unknown job, rejected reply, protocol error)."""


class BridgeUnreachable(BridgeError):
    """Bridge is not accepting connections or the command budget ran out."""


class BridgeDenied(BridgeError):
    """Bridge rejected the bearer token."""


class _SessionExpired(Exception):
    pass


class BridgeClient:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        token: str = "",
        timeout: float = CALL_TIMEOUT_SECONDS,
        deadline: float = COMMAND_DEADLINE_SECONDS,
    ) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self.deadline = deadline
        self.session_id: str | None = None
        self._id = 0
        self._deadline_at: float | None = None

    @contextlib.contextmanager
    def budget(self) -> Iterator[None]:
        """Bound every call inside the block by one shared wall-clock budget."""
        self._deadline_at = time.monotonic() + self.deadline
        try:
            yield
        finally:
            self._deadline_at = None

    def job_list(self) -> list[dict[str, Any]]:
        jobs = self._call("opencode_job_list", {}).get("jobs")
        return jobs if isinstance(jobs, list) else []

    def job_cancel(self, job_id: str) -> dict[str, Any]:
        return self._call("opencode_job_cancel", {"job_id": job_id})

    def permissions_pending(self) -> list[dict[str, Any]]:
        requests = self._call("opencode_permissions_pending", {}).get("requests")
        return requests if isinstance(requests, list) else []

    def permission_reply(self, permission_id: str, decision: str) -> dict[str, Any]:
        job_id = None
        for request in self.permissions_pending():
            if request.get("id") == permission_id:
                job_id = request.get("job_id")
                break
        if not isinstance(job_id, str) or not job_id:
            raise BridgeError(f"no pending permission matches {permission_id!r}")
        return self._call(
            "opencode_permission_reply",
            {"job_id": job_id, "permission_id": permission_id, "reply": decision},
        )

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.session_id is None:
            self._initialize()
        try:
            result = self._post_tool(name, arguments)
        except _SessionExpired:
            self.session_id = None
            self._initialize()
            result = self._post_tool(name, arguments)
        payload = _payload(result)
        if result.get("isError"):
            raise BridgeError(str(payload.get("error") or "bridge tool error"))
        return payload

    def _initialize(self) -> None:
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "rdm-bridge-client", "version": "1.0"},
                },
            }
        )
        if not isinstance(response, dict) or "error" in response or not isinstance(response.get("result"), dict):
            raise BridgeError(f"bridge initialize failed: {response!r}"[:200])
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        if response is None:
            raise BridgeError("bridge returned an empty tools/call response")
        if "error" in response:
            raise BridgeError(str(response["error"].get("message") or response["error"]))
        result = response.get("result")
        if not isinstance(result, dict):
            raise BridgeError("bridge tools/call response has no result object")
        return result

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout()) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                body = response.read().decode("utf-8", "replace")
                content_type = (response.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code in (401, 403):
                raise BridgeDenied(f"HTTP {exc.code}: {detail}") from None
            if exc.code == 404 and self.session_id:
                raise _SessionExpired() from None
            raise BridgeError(f"HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise BridgeUnreachable(f"bridge unreachable: {exc}") from None
        if not body.strip():
            return None
        if "text/event-stream" in content_type:
            return _sse_message(body)
        try:
            message = json.loads(body)
        except ValueError:
            raise BridgeError("bridge response is not valid JSON") from None
        if not isinstance(message, dict):
            raise BridgeError("bridge response is not a JSON-RPC object") from None
        return message

    def _timeout(self) -> float:
        if self._deadline_at is None:
            return self.timeout
        remaining = self._deadline_at - time.monotonic()
        if remaining <= 0:
            raise BridgeUnreachable("bridge command deadline exceeded")
        return min(self.timeout, remaining)

    def _next_id(self) -> int:
        self._id += 1
        return self._id


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    for block in result.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            data = json.loads(block.get("text") or "")
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise BridgeError("bridge tool result carries no JSON payload")


def _sse_message(body: str) -> dict[str, Any] | None:
    message: dict[str, Any] | None = None
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            parsed = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
            message = parsed
    return message
