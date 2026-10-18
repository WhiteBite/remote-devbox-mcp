"""Сессии шлюза: эксклюзивные stdio-дети на клиентскую сессию, remap id, teardown."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

from rdm import envfile, freeze, profiles
from rdm.gateway import policy, snapshot
from rdm.gateway.server import CallResult, SessionLimitError, ToolDef
from rdm.gateway.upstream import UpstreamChild, UpstreamError, child_log_dir

SPAWN_DEADLINE = 10.0
UPSTREAM_CALL_TIMEOUT = 300.0
MAX_TEXT_ITEM_CHARS = 1_000_000
SERVER_INFO_NAME = "devbox-gateway"
_ENV_FILE = freeze.app_dir() / ".env"


class SessionPolicy:
    __slots__ = ("allowlists", "project_dir")

    def __init__(
        self,
        allowlists: Mapping[str, frozenset[str]] | None = None,
        project_dir: str | None = None,
    ) -> None:
        self.allowlists: Mapping[str, frozenset[str]] = dict(allowlists or {})
        self.project_dir = project_dir

    def check_call(
        self, server: str, tool: str, args: Mapping[str, Any] | None
    ) -> tuple[bool, str]:
        return policy.check_call(
            server, tool, self.allowlists.get(server), args, self.project_dir
        )


def _active_profile() -> tuple[profiles.Profile | None, Path | None]:
    env_map = envfile.EnvFile.load(_ENV_FILE).as_map()
    name = env_map.get("ACTIVE_PROFILE", "")
    if not name:
        return None, None
    path = profiles.find(name)
    if path is None:
        return None, None
    return profiles.load(path), path


def _empty_snapshot() -> snapshot.Snapshot:
    return snapshot.Snapshot()


def _instructions(children: Mapping[str, UpstreamChild]) -> str:
    names = ", ".join(sorted(children))
    if not names:
        return "No MCP servers are exposed."
    return f"Exposed MCP servers: {names}. Tools are exposed as <server>__<tool>."


def _merge_capabilities(children: Mapping[str, UpstreamChild]) -> dict[str, Any]:
    has_tools = False
    flags: dict[str, bool] = {}
    for child in children.values():
        entry = child.capabilities.get("tools")
        if entry is None:
            continue
        has_tools = True
        if isinstance(entry, dict):
            for flag, value in entry.items():
                if isinstance(value, bool):
                    flags[flag] = flags.get(flag, False) or value
    return {"tools": flags} if has_tools else {}


def _error_result(reason: str) -> CallResult:
    return CallResult(content=[{"type": "text", "text": reason}], is_error=True)


def _from_upstream_result(result: Mapping[str, Any]) -> CallResult:
    content = list(result.get("content") or [])
    truncated = False
    capped: list[Any] = []
    for item in content:
        if (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
            and len(item["text"]) > MAX_TEXT_ITEM_CHARS
        ):
            item = {**item, "text": item["text"][:MAX_TEXT_ITEM_CHARS]}
            truncated = True
        capped.append(item)
    return CallResult(
        content=capped, is_error=bool(result.get("isError")), truncated=truncated
    )


class Session:
    def __init__(
        self,
        session_id: str,
        policy: SessionPolicy,
        on_close: Callable[[str], None],
    ) -> None:
        self.id = session_id
        self.server_info_name = SERVER_INFO_NAME
        self.instructions = ""
        self.capabilities: dict[str, Any] = {}
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._policy = policy
        self._on_close = on_close
        self._children: dict[str, UpstreamChild] = {}
        self._registry: dict[str, str] = {}
        self._tool_defs: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self._pending: dict[Any, UpstreamChild] = {}
        self._pending_lock = threading.Lock()
        self._call_counter = 0
        self._idle_stamp = time.monotonic()
        self._closed = False

    def touch(self) -> None:
        self._idle_stamp = time.monotonic()

    def list_tools(self) -> list[ToolDef]:
        tools: list[ToolDef] = []
        for registered, exposed in self._registry.items():
            info = self._tool_defs.get(exposed)
            if info is None:
                continue
            server, tool, schema = info
            child = self._children.get(server)
            if child is None or not child.live:
                continue
            tools.append((registered, server, tool, schema))
        return tools

    def call_tool(
        self,
        exposed_name: str,
        arguments: dict[str, Any],
        progress_token: Any = None,
        rpc_id: Any = None,
    ) -> CallResult:
        self.touch()
        parsed = policy.parse_exposed_tool_name(
            self._registry.get(exposed_name, exposed_name)
        )
        if parsed is None:
            return _error_result("unknown tool")
        server, tool = parsed
        allowed, reason = self._policy.check_call(server, tool, arguments)
        if not allowed:
            return _error_result(reason)
        child = self._children.get(server)
        if child is None or not child.live:
            return _error_result(f"server unavailable: {server}")
        params: dict[str, Any] = {"name": tool, "arguments": arguments}
        if progress_token is not None:
            params["_meta"] = {"progressToken": progress_token}
        call_id = rpc_id if rpc_id is not None else self._next_call_id()
        with self._pending_lock:
            self._pending[call_id] = child
        try:
            result = child.request("tools/call", params, timeout=UPSTREAM_CALL_TIMEOUT)
        except UpstreamError as error:
            return _error_result(str(error))
        finally:
            with self._pending_lock:
                self._pending.pop(call_id, None)
        return _from_upstream_result(result)

    def cancel(self, rpc_id: Any) -> None:
        with self._pending_lock:
            child = self._pending.get(rpc_id)
        if child is None:
            return
        upstream_id = child.inflight_id
        if upstream_id is not None:
            child.cancel_upstream(upstream_id)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for child in self._children.values():
            child.close()
        self._on_close(self.id)

    def _next_call_id(self) -> int:
        with self._pending_lock:
            self._call_counter += 1
            return self._call_counter

    def _register(self, children: Mapping[str, UpstreamChild]) -> None:
        self._children = dict(children)
        exposed: list[str] = []
        for server, child in children.items():
            allowed = self._policy.allowlists.get(server) or frozenset()
            for tool in child.tools:
                name = tool.get("name")
                if (
                    not isinstance(name, str)
                    or name not in allowed
                    or name in policy.META_TOOL_DENY
                ):
                    continue
                exposed_name = policy.exposed_tool_name(server, name)
                self._tool_defs[exposed_name] = (
                    server,
                    name,
                    tool.get("inputSchema") or {},
                )
                exposed.append(exposed_name)
        self._registry = policy.register_tools(exposed)
        self.instructions = _instructions(children)
        self.capabilities = _merge_capabilities(children)


class SessionManager:
    def __init__(
        self,
        snapshot_loader: Callable[[], snapshot.Snapshot] | None = None,
        policy: SessionPolicy | None = None,
        max_sessions: int = 4,
        idle_seconds: float = 1800.0,
    ) -> None:
        self._snapshot_loader = snapshot_loader
        self._policy = policy
        self._max_sessions = max_sessions
        self._idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._creating = 0
        self._next_id = 1

    def create_session(self) -> Session:
        self._ensure_defaults()
        with self._lock:
            if len(self._sessions) + self._creating >= self._max_sessions:
                raise SessionLimitError(f"session limit reached ({self._max_sessions})")
            self._creating += 1
            session_id = f"sess-{self._next_id}"
            self._next_id += 1
        try:
            snap = self._snapshot_loader()
            session = Session(session_id, self._policy, self._drop)
            children = self._spawn_children(snap, session, session_id)
            try:
                session._register(children)
            except Exception:
                for child in children.values():
                    child.close()
                raise
            with self._lock:
                self._creating -= 1
                self._sessions[session_id] = session
            return session
        except BaseException:
            with self._lock:
                self._creating -= 1
            raise

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def reap_idle(self) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [
                session
                for session in self._sessions.values()
                if now - session._idle_stamp > self._idle_seconds
            ]
        for session in stale:
            session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.close()

    def _drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _ensure_defaults(self) -> None:
        if self._snapshot_loader is not None and self._policy is not None:
            return
        profile, path = _active_profile()
        if self._snapshot_loader is None:
            if profile is None or path is None:
                self._snapshot_loader = _empty_snapshot
            else:
                self._snapshot_loader = partial(snapshot.load_verified, path, profile)
        if self._policy is None:
            allowlists = (
                {m.name: frozenset(m.tool_allow) for m in profile.exposed_mcps}
                if profile is not None
                else {}
            )
            self._policy = SessionPolicy(
                allowlists=allowlists,
                project_dir=profile.project_dir if profile is not None else None,
            )

    def _spawn_children(
        self,
        snap: snapshot.Snapshot,
        session: Session,
        session_id: str,
    ) -> dict[str, UpstreamChild]:
        children: dict[str, UpstreamChild] = {}
        lock = threading.Lock()
        log_dir = child_log_dir() / session_id

        def spawn(name: str, server: snapshot.ServerSnapshot) -> None:
            child = UpstreamChild(
                name,
                server.command,
                list(server.args),
                dict(server.env),
                session.notifications.put,
                log_dir=log_dir,
            )
            if child.start(deadline=SPAWN_DEADLINE):
                with lock:
                    children[name] = child
            else:
                child.close()

        threads = [
            threading.Thread(target=spawn, args=(name, server), daemon=True)
            for name, server in snap.servers
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return children
