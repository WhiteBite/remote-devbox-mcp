"""Single-flight cockpit actions: one cli entry point at a time on a worker thread.

The ui process wraps its own stdio in rdm.ui.__main__._RedactingStream, so the
full-token prints these cli callables make never reach the browser or logs
unmasked; stdout is deliberately not captured or redirected here.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from rdm import cli
from rdm.events import sink


class ActionQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def busy(self) -> bool:
        return self._lock.locked()

    def submit(self, fn: Callable[..., int], *args: Any) -> str | None:
        if not self._lock.acquire(blocking=False):
            return None
        action_id = secrets.token_hex(8)
        threading.Thread(target=self._run, args=(action_id, fn, args), daemon=True).start()
        return action_id

    def _run(self, action_id: str, fn: Callable[..., int], args: tuple[Any, ...]) -> None:
        name = getattr(fn, "__name__", "action")
        sink.emit({"ts": time.time(), "kind": "action", "job_id": action_id, "tool": name, "status": "started"})
        rc = 1
        try:
            rc = fn(*args)
        finally:
            try:
                sink.emit(
                    {"ts": time.time(), "kind": "action", "job_id": action_id, "tool": name, "status": "finished", "exit": rc}
                )
            finally:
                self._lock.release()


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
