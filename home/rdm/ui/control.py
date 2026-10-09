"""Single-flight cockpit actions: one cli entry point at a time on a worker thread.

The ui process wraps its own stdio in rdm.ui.__main__._RedactingStream, so the
full-token prints these cli callables make never reach the browser or logs
unmasked; stdout is deliberately not captured or redirected here.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from rdm import cli, redact
from rdm.events import sink

_PAYLOAD_CAP_BYTES = 64 * 1024


def _safe_emit(event: dict) -> None:
    try:
        sink.emit(event)
    except Exception:
        pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _action_payload(result: object) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    payload = {key: redact.redact_text(value) if isinstance(value, str) else value for key, value in result.items()}
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    if len(blob.encode("utf-8")) > _PAYLOAD_CAP_BYTES:
        return None
    return payload


class ActionQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, Any] | None = None

    def busy(self) -> bool:
        return self._lock.locked()

    def current(self) -> dict[str, Any] | None:
        return self._current

    def submit(self, fn: Callable[..., int | dict[str, Any]], *args: Any) -> str | None:
        if not self._lock.acquire(blocking=False):
            return None
        action_id = secrets.token_hex(8)
        threading.Thread(target=self._run, args=(action_id, fn, args), daemon=True).start()
        return action_id

    def _run(self, action_id: str, fn: Callable[..., int | dict[str, Any]], args: tuple[Any, ...]) -> None:
        name = getattr(fn, "__name__", "action")
        started = time.time()
        self._current = {"id": action_id, "cmd": name, "startedAt": _iso(started)}
        _safe_emit({"ts": started, "kind": "action", "job_id": action_id, "tool": name, "status": "started"})
        rc = 1
        payload = None
        try:
            result = fn(*args)
            if isinstance(result, dict):
                rc = int(result.get("exit", 0))
                payload = _action_payload({k: v for k, v in result.items() if k != "exit"})
            else:
                rc = int(result)
        finally:
            self._current = None
            event: dict[str, Any] = {
                "ts": time.time(),
                "kind": "action",
                "job_id": action_id,
                "tool": name,
                "status": "finished",
                "exit": rc,
            }
            if payload is not None:
                event["payload"] = payload
            _safe_emit(event)
            self._lock.release()


def reconcile_open_actions() -> int:
    """Close out action events left 'started' by a dead process.

    The queue daemon thread dies with its process, so an action interrupted
    mid-run stays 'started' in the event log forever.  Emits one
    'interrupted' event per orphaned 'started' (idempotent: 'interrupted'
    also counts as closed).
    """
    from rdm.ui import api  # noqa: PLC0415

    events = api.read_events()
    closed = {
        event.get("job_id")
        for event in events
        if event.get("kind") == "action" and event.get("status") in ("finished", "interrupted")
    }
    interrupted = 0
    for event in events:
        if event.get("kind") != "action" or event.get("status") != "started":
            continue
        if event.get("job_id") in closed:
            continue
        _safe_emit(
            {
                "ts": time.time(),
                "kind": "action",
                "job_id": event.get("job_id"),
                "tool": event.get("tool"),
                "status": "interrupted",
            }
        )
        interrupted += 1
    return interrupted


def apply_use(name: str) -> int:
    return cli.apply_use(name)


def allow_port(port: int, ui: bool) -> int:
    return cli._allow(port, ui)


def issue_tokens() -> int:
    return cli._issue_tokens()


def run_doctor() -> int:
    return cli._doctor()


def start(name: str | None, preview: bool) -> int:
    return cli._start(name, preview)


def down() -> int:
    return cli._down()


def stop_host() -> int:
    return cli._stop_host()


def ingress(action: str) -> int:
    return cli._ingress(action)
