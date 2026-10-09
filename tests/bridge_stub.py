from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "stub-bearer-token"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        stub: BridgeStub = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            message: dict[str, Any] = json.loads(raw) if raw.strip() else {}
        except ValueError:
            self._json(400, {"error": "bad json"})
            return
        if self.headers.get("Authorization") != f"Bearer {stub.token}":
            self._json(401, {"error": "unauthorized"})
            return
        method = message.get("method")
        if method == "initialize":
            session = f"s{len(stub.sessions)}"
            stub.sessions.add(session)
            self._json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "stub-bridge"},
                    },
                },
                session,
            )
            return
        if self.headers.get("Mcp-Session-Id") not in stub.sessions:
            self._json(404, {"error": "session expired"})
            return
        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return
        if method == "tools/call":
            params = message.get("params") or {}
            stub.calls.append(params)
            self._tool(stub, params)
            return
        self._json(400, {"error": f"unexpected method {method}"})

    def _tool(self, stub: BridgeStub, params: dict[str, Any]) -> None:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "opencode_job_list":
            self._result({"jobs": stub.jobs})
        elif name == "opencode_permissions_pending":
            self._result({"requests": stub.requests})
        elif name == "opencode_job_cancel":
            if arguments.get("job_id") == "unknown":
                self._result({"error": "Unknown or expired job_id"}, is_error=True)
            else:
                self._result({"job_id": arguments.get("job_id"), "status": "cancelling"})
        elif name == "opencode_permission_reply":
            self._result({"job_id": arguments.get("job_id"), "status": "running"})
        else:
            self._result({"error": f"unknown tool {name}"}, is_error=True)

    def _result(self, payload: dict[str, Any], is_error: bool = False) -> None:
        self._json(
            200,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "isError": is_error,
                },
            },
        )

    def _json(self, status: int, payload: dict[str, Any], session: str | None = None) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, stub: BridgeStub) -> None:
        self.stub = stub
        super().__init__(("127.0.0.1", 0), _Handler)


class BridgeStub:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.jobs: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.sessions: set[str] = set()
        self._server = _Server(self)
        self.url = f"http://127.0.0.1:{self._server.server_port}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def forget_sessions(self) -> None:
        self.sessions = set()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def dead_bridge_url() -> str:
    stub = BridgeStub()
    url = stub.url
    stub.close()
    return url
