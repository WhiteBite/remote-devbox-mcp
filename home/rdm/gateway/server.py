"""MCP streamable-HTTP gateway server: single /mcp endpoint over a SessionManager.

The SessionManager/Session contract is the frozen S7b interface; real
implementations satisfy it structurally. Sessions receive a `notifications`
queue.Queue attribute from this module: the session pushes JSON-RPC
notifications there, GET /mcp drains it into one SSE stream per session.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from rdm.proxy.auth import constant_time_equal

logger = logging.getLogger(__name__)

LOOPBACK_HOST = "127.0.0.1"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
SERVER_VERSION = "0.1.0"
MAX_TEXT_CHARS = 32000
MAX_RESULT_CHARS = 64000
CALL_TIMEOUT_SECONDS = 300.0
SSE_POLL_SECONDS = 0.25
SSE_HEARTBEAT_SECONDS = 15.0
NOTIFICATIONS_ATTR = "notifications"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_SESSION_REQUIRED = -32000
JSONRPC_SESSION_NOT_FOUND = -32001

_STREAM_END = object()


class SessionLimitError(Exception):
    """create_session() refused: MAX_SESSIONS live."""


class StaleSessionError(Exception):
    """Session id unknown or already terminated."""


class CallTimeoutError(Exception):
    """tools/call exceeded CALL_TIMEOUT_SECONDS."""


@dataclass(frozen=True, slots=True)
class CallResult:
    content: list[Any]
    is_error: bool
    truncated: bool = False


ToolDef = tuple[str, str, str, dict[str, Any]]


class Session(Protocol):
    id: str
    server_info_name: str
    instructions: str
    capabilities: dict[str, Any]

    def list_tools(self) -> list[ToolDef]: ...

    def call_tool(self, exposed_name: str, arguments: dict[str, Any], progress_token: Any = None) -> CallResult: ...

    def cancel(self, rpc_id: Any) -> None: ...

    def close(self) -> None: ...


class SessionManager(Protocol):
    def create_session(self) -> Session: ...

    def get(self, session_id: str) -> Session | None: ...


def _notifications_queue(session: Session) -> queue.Queue:
    existing = getattr(session, NOTIFICATIONS_ATTR, None)
    if existing is not None:
        return existing
    created: queue.Queue = queue.Queue()
    setattr(session, NOTIFICATIONS_ATTR, created)
    return created


def _is_text(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)


def _cap_content(content: list[Any]) -> tuple[list[Any], bool]:
    items = [dict(item) if isinstance(item, dict) else item for item in content]
    truncated = False
    for item in items:
        if _is_text(item) and len(item["text"]) > MAX_TEXT_CHARS:
            item["text"] = item["text"][:MAX_TEXT_CHARS]
            truncated = True
    while len(json.dumps(items, ensure_ascii=False)) > MAX_RESULT_CHARS:
        index = next((i for i in reversed(range(len(items))) if _is_text(items[i])), None)
        if index is None:
            if not items:
                return items, truncated
            items.pop()
            truncated = True
            continue
        text = items[index]["text"]
        excess = len(json.dumps(items, ensure_ascii=False)) - MAX_RESULT_CHARS
        cut = min(len(text), excess + 64)
        if cut >= len(text):
            items.pop(index)
        else:
            items[index]["text"] = text[: len(text) - cut]
        truncated = True
    return items, truncated


def _call_tool(session: Session, name: str, arguments: dict[str, Any], progress_token: Any, rpc_id: Any) -> CallResult:
    box: list[CallResult] = []
    failure: list[Exception] = []

    def run() -> None:
        try:
            box.append(session.call_tool(name, arguments, progress_token))
        except Exception as error:
            failure.append(error)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(CALL_TIMEOUT_SECONDS)
    if worker.is_alive():
        session.cancel(rpc_id)
        raise CallTimeoutError(f"tool call exceeded {CALL_TIMEOUT_SECONDS}s")
    if failure:
        raise failure[0]
    return box[0]


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], manager_factory: Callable[[], SessionManager]) -> None:
        self._manager_factory = manager_factory
        self._manager: SessionManager | None = None
        self._sessions: dict[str, Session] = {}
        self._streams: set[str] = set()
        self._lock = threading.Lock()
        self._shutting_down = threading.Event()
        self._stopped = False
        super().__init__(address, _Handler)

    def manager(self) -> SessionManager:
        with self._lock:
            if self._manager is None:
                self._manager = self._manager_factory()
            return self._manager

    def register_session(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.id] = session

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def drop_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def acquire_stream(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._streams:
                return False
            self._streams.add(session_id)
            return True

    def release_stream(self, session_id: str) -> None:
        with self._lock:
            self._streams.discard(session_id)

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
        self._shutting_down.set()
        for session in sessions:
            notifications = getattr(session, NOTIFICATIONS_ATTR, None)
            if notifications is not None:
                notifications.put(_STREAM_END)
        for session in sessions:
            try:
                session.close()
            except Exception:
                logger.warning("gateway: session close failed during shutdown", exc_info=True)
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            self._respond(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path != "/mcp":
            self._respond(404, b"", "text/plain; charset=utf-8")
            return
        if not self._auth_ok():
            self._jsonrpc_error(401, JSONRPC_SESSION_REQUIRED, "unauthorized", None)
            return
        session_id = self.headers.get("Mcp-Session-Id", "")
        if not session_id:
            self._jsonrpc_error(400, JSONRPC_SESSION_REQUIRED, "missing Mcp-Session-Id header", None)
            return
        try:
            session = self._session(session_id)
        except StaleSessionError:
            self._jsonrpc_error(404, JSONRPC_SESSION_NOT_FOUND, "session not found", None)
            return
        if not self.server.acquire_stream(session_id):
            self._jsonrpc_error(409, JSONRPC_SESSION_REQUIRED, "session stream already open", None)
            return
        try:
            self._sse_stream(session, session_id)
        finally:
            self.server.release_stream(session_id)

    def do_POST(self) -> None:
        body = self._read_body()
        path = urllib.parse.urlsplit(self.path).path
        if path != "/mcp":
            self._respond(404, b"", "text/plain; charset=utf-8")
            return
        if not self._auth_ok():
            self._jsonrpc_error(401, JSONRPC_SESSION_REQUIRED, "unauthorized", None)
            return
        version = self.headers.get("MCP-Protocol-Version", "")
        if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
            extra = (("MCP-Protocol-Version", DEFAULT_PROTOCOL_VERSION),)
            self._jsonrpc_error(400, JSONRPC_SESSION_REQUIRED, "unsupported protocol version", None, extra)
            return
        try:
            message = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._jsonrpc_error(400, JSONRPC_PARSE_ERROR, "parse error", None)
            return
        if isinstance(message, list):
            self._jsonrpc_error(400, JSONRPC_INVALID_REQUEST, "batch requests are not supported", None)
            return
        if not isinstance(message, dict):
            self._jsonrpc_error(400, JSONRPC_INVALID_REQUEST, "invalid request", None)
            return
        method = message.get("method")
        rpc_id = message.get("id")
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                self._accepted()
            else:
                self._jsonrpc_error(400, JSONRPC_INVALID_REQUEST, "invalid request", None)
            return
        if method == "initialize":
            self._handle_initialize(message, version, rpc_id)
            return
        session_id = self.headers.get("Mcp-Session-Id", "")
        if not session_id:
            self._jsonrpc_error(400, JSONRPC_SESSION_REQUIRED, "missing Mcp-Session-Id header", rpc_id)
            return
        try:
            session = self._session(session_id)
        except StaleSessionError:
            self._jsonrpc_error(404, JSONRPC_SESSION_NOT_FOUND, "session not found", rpc_id)
            return
        self._dispatch(session, method, message.get("params"), rpc_id)

    def do_DELETE(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path != "/mcp":
            self._respond(404, b"", "text/plain; charset=utf-8")
            return
        if not self._auth_ok():
            self._jsonrpc_error(401, JSONRPC_SESSION_REQUIRED, "unauthorized", None)
            return
        session_id = self.headers.get("Mcp-Session-Id", "")
        if not session_id:
            self._jsonrpc_error(400, JSONRPC_SESSION_REQUIRED, "missing Mcp-Session-Id header", None)
            return
        try:
            session = self._session(session_id)
        except StaleSessionError:
            self._jsonrpc_error(404, JSONRPC_SESSION_NOT_FOUND, "session not found", None)
            return
        session.close()
        self.server.drop_session(session_id)
        notifications = getattr(session, NOTIFICATIONS_ATTR, None)
        if notifications is not None:
            notifications.put(_STREAM_END)
        self._respond(200, b"", "text/plain; charset=utf-8")

    def _handle_initialize(self, message: dict, version: str, rpc_id: Any) -> None:
        try:
            session = self.server.manager().create_session()
        except SessionLimitError:
            self._jsonrpc_error(429, JSONRPC_SESSION_REQUIRED, "session limit reached", rpc_id)
            return
        except Exception:
            self._jsonrpc_error(200, JSONRPC_INTERNAL_ERROR, "session layer unavailable", rpc_id)
            return
        negotiated = version if version in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        self.server.register_session(session)
        _notifications_queue(session)
        result = {
            "protocolVersion": negotiated,
            "capabilities": session.capabilities,
            "serverInfo": {"name": session.server_info_name, "version": SERVER_VERSION},
            "instructions": session.instructions,
        }
        extra = (("Mcp-Session-Id", session.id),)
        self._jsonrpc_result(result, rpc_id, extra)

    def _dispatch(self, session: Session, method: str, params: Any, rpc_id: Any) -> None:
        if rpc_id is None:
            if method == "notifications/cancelled" and isinstance(params, dict):
                request_id = params.get("requestId")
                if request_id is not None:
                    session.cancel(request_id)
            self._accepted()
            return
        if method == "ping":
            self._jsonrpc_result({}, rpc_id)
            return
        if method == "tools/list":
            tools = [{"name": entry[0], "inputSchema": entry[3]} for entry in session.list_tools()]
            self._jsonrpc_result({"tools": tools}, rpc_id)
            return
        if method == "tools/call":
            self._tools_call(session, params, rpc_id)
            return
        self._jsonrpc_error(200, JSONRPC_METHOD_NOT_FOUND, "method not found", rpc_id)

    def _tools_call(self, session: Session, params: Any, rpc_id: Any) -> None:
        if not isinstance(params, dict):
            self._jsonrpc_error(200, JSONRPC_INVALID_PARAMS, "invalid params", rpc_id)
            return
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str) or (arguments is not None and not isinstance(arguments, dict)):
            self._jsonrpc_error(200, JSONRPC_INVALID_PARAMS, "invalid params", rpc_id)
            return
        if not any(entry[0] == name for entry in session.list_tools()):
            self._jsonrpc_error(200, JSONRPC_INVALID_PARAMS, "unknown tool", rpc_id)
            return
        meta = params.get("_meta")
        progress_token = meta.get("progressToken") if isinstance(meta, dict) else None
        resolved: dict[str, Any] = {} if arguments is None else arguments
        try:
            call = _call_tool(session, name, resolved, progress_token, rpc_id)
        except CallTimeoutError:
            self._jsonrpc_error(200, JSONRPC_INTERNAL_ERROR, "tool call timed out", rpc_id)
            return
        except Exception:
            self._jsonrpc_error(200, JSONRPC_INTERNAL_ERROR, "internal error", rpc_id)
            return
        if call.is_error and not call.content:
            self._jsonrpc_error(200, JSONRPC_INTERNAL_ERROR, "upstream tool call failed", rpc_id)
            return
        content, truncated = _cap_content(call.content)
        result: dict[str, Any] = {"content": content, "isError": call.is_error}
        if truncated or call.truncated:
            result["_meta"] = {"truncated": True}
        self._jsonrpc_result(result, rpc_id)

    def _sse_stream(self, session: Session, session_id: str) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        notifications = _notifications_queue(session)
        last_heartbeat = time.monotonic()
        while True:
            if self.server._shutting_down.is_set():
                return
            manager = self.server._manager
            if manager is None or manager.get(session_id) is not session:
                return
            try:
                item = notifications.get(timeout=SSE_POLL_SECONDS)
            except queue.Empty:
                item = None
            if item is _STREAM_END:
                return
            if item is not None:
                try:
                    payload = json.dumps(item, ensure_ascii=False).encode("utf-8")
                    self.wfile.write(b"event: message\ndata: " + payload + b"\n\n")
                    self.wfile.flush()
                except OSError:
                    return
            now = time.monotonic()
            if now - last_heartbeat >= SSE_HEARTBEAT_SECONDS:
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except OSError:
                    return
                last_heartbeat = now

    def _session(self, session_id: str) -> Session:
        manager = self.server._manager
        session = manager.get(session_id) if manager is not None else None
        if session is None or not self.server.has_session(session_id):
            raise StaleSessionError(session_id)
        return session

    def _auth_ok(self) -> bool:
        token = os.environ.get("GATEWAY_TOKEN", "")
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        return scheme.lower() == "bearer" and constant_time_equal(value.encode("utf-8"), token.encode("utf-8"))

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return b""
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _accepted(self) -> None:
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(
        self, status: int, payload: dict, extra_headers: tuple[tuple[str, str], ...] = ()
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _jsonrpc_result(
        self, result: dict, rpc_id: Any, extra_headers: tuple[tuple[str, str], ...] = ()
    ) -> None:
        self._send_json(200, {"jsonrpc": "2.0", "id": rpc_id, "result": result}, extra_headers)

    def _jsonrpc_error(
        self,
        status: int,
        code: int,
        message: str,
        rpc_id: Any,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        payload = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}
        self._send_json(status, payload, extra_headers)


def build_server(host: str, port: int, manager_factory: Callable[[], SessionManager]) -> GatewayServer:
    if host != LOOPBACK_HOST:
        raise ValueError(f"gateway binds {LOOPBACK_HOST!r} only, refusing {host!r}")
    return GatewayServer((host, port), manager_factory)
