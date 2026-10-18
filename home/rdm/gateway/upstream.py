"""Один stdio MCP-ребёнок апстрима: рукопожатие, remap id, рестарт с бэкоффом."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from rdm import childproc, hostos

RESTART_BACKOFF_START = 1.0
RESTART_BACKOFF_CAP = 60.0
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "devbox-gateway", "version": "0.3.0"}


class UpstreamError(RuntimeError):
    """Протокольная ошибка, крэш или таймаут stdio-ребёнка апстрима."""


def child_log_dir() -> Path:
    return hostos.tempdir() / "rdm-gateway"


class _Pending:
    __slots__ = ("event", "response")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None


class UpstreamChild:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str],
        env: Mapping[str, str] | None,
        on_notification: Callable[[dict[str, Any]], None],
        log_dir: Path | None = None,
    ) -> None:
        self._name = name
        self._command = command
        self._args = list(args)
        self._env = dict(env or {})
        self._on_notification = on_notification
        directory = Path(log_dir) if log_dir is not None else child_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self._log = logging.getLogger(f"rdm.gateway.upstream.{name}.{id(self):x}")
        self._log.propagate = False
        self._log.setLevel(logging.INFO)
        self._handler = logging.handlers.RotatingFileHandler(
            directory / f"gateway-{name}.log",
            maxBytes=1_000_000,
            backupCount=2,
            encoding="utf-8",
        )
        self._log.addHandler(self._handler)
        self._proc: subprocess.Popen[bytes] | None = None
        self._cycle_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[Any, _Pending] = {}
        self._next_id = 1
        self._capabilities: dict[str, Any] = {}
        self._instructions = ""
        self._tools: list[dict[str, Any]] = []
        self._dead = True
        self._closed = False
        self._attempts = 0
        self._death = threading.Event()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._stderr: threading.Thread | None = None
        self._supervisor = threading.Thread(target=self._supervise, daemon=True)
        self._supervisor.start()

    @property
    def name(self) -> str:
        return self._name

    @property
    def live(self) -> bool:
        return (
            not self._closed
            and not self._dead
            and self._proc is not None
            and self._proc.poll() is None
        )

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._capabilities

    @property
    def instructions(self) -> str:
        return self._instructions

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    def start(self, deadline: float = 10.0) -> bool:
        try:
            self._spawn()
            ends_at = time.monotonic() + deadline
            init = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
                timeout=max(0.1, ends_at - time.monotonic()),
            )
            self.notify("notifications/initialized", {})
            listed = self.request(
                "tools/list", {}, timeout=max(0.1, ends_at - time.monotonic())
            )
        except Exception:
            self._kill_proc()
            return False
        self._capabilities = init.get("capabilities") or {}
        self._instructions = init.get("instructions") or ""
        self._tools = listed.get("tools") or []
        self._dead = False
        return True

    def request(self, method: str, params: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        with self._cycle_lock:
            with self._pending_lock:
                rpc_id = self._next_id
                self._next_id += 1
                pending = _Pending()
                self._pending[rpc_id] = pending
            message: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
            if params:
                message["params"] = params
            try:
                self._send(message)
            except UpstreamError:
                with self._pending_lock:
                    self._pending.pop(rpc_id, None)
                raise
            if not pending.event.wait(timeout):
                with self._pending_lock:
                    self._pending.pop(rpc_id, None)
                raise UpstreamError(f"upstream timeout: {method}")
            response = pending.response or {}
        if "error" in response:
            error = response["error"] or {}
            raise UpstreamError(f"upstream error {error.get('code')}: {error.get('message')}")
        return response.get("result") or {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        self._send(message)

    def cancel_upstream(self, upstream_id: Any) -> None:
        self.notify("notifications/cancelled", {"requestId": upstream_id})

    def close(self) -> None:
        self._closed = True
        self._stop.set()
        self._death.set()
        self._kill_proc()
        for thread in (self._reader, self._stderr):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)
        self._handler.close()
        self._log.removeHandler(self._handler)

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed:
            raise UpstreamError("upstream child is not running")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError) as error:
                raise UpstreamError(f"upstream write failed: {error}") from error

    def _spawn(self) -> None:
        argv = [self._command, *self._args]
        resolved = childproc.resolve(argv, Path.cwd())
        env = childproc.child_env(self._env)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            resolved,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=flags,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stderr.start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw in iter(proc.stdout.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" not in message:
                self._on_notification(message)
            elif "id" in message:
                self._resolve_pending(message)
        self._on_death()

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in iter(proc.stderr.readline, b""):
            self._log.info(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def _resolve_pending(self, message: dict[str, Any]) -> None:
        with self._pending_lock:
            pending = self._pending.pop(message.get("id"), None)
        if pending is None:
            return
        pending.response = message
        pending.event.set()

    def _on_death(self) -> None:
        with self._pending_lock:
            pending = list(self._pending.items())
            self._pending.clear()
        for rpc_id, entry in pending:
            entry.response = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {
                    "code": -32603,
                    "message": f"upstream child '{self._name}' crashed",
                },
            }
            entry.event.set()
        self._dead = True
        if not self._closed:
            self._death.set()

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            hostos.kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _supervise(self) -> None:
        while not self._stop.is_set():
            self._death.wait()
            self._death.clear()
            if self._stop.is_set() or self._closed:
                return
            while not self._closed:
                delay = min(
                    RESTART_BACKOFF_START * 2 ** self._attempts, RESTART_BACKOFF_CAP
                )
                self._attempts += 1
                if self._stop.wait(delay):
                    return
                if self.start():
                    self._attempts = 0
                    self._death.clear()
                    if not self._dead:
                        break
