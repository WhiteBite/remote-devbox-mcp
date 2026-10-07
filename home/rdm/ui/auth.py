"""Cockpit-local auth: own secret, session store, one-shot bootstrap token.

This module owns the cockpit's secret and MUST NOT import rdm.tokens or
reference any agent-known token: the agent-facing bearer, host and ingress
secrets are printed to the agent in the hand-off block, so reusing any of
them here would make the agent a cockpit principal.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

from rdm import freeze

_BOOTSTRAP_TTL_SECONDS = 30
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


def ui_state_dir() -> Path:
    override = os.environ.get("RDM_UI_STATE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(tempfile.gettempdir()) / "rdm-ui"
    return Path.home() / ".devbox" / "rdm-ui"


def static_dir() -> Path:
    return freeze.app_dir() / "ui-static"


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_or_create_secret() -> str:
    path = ui_state_dir() / "secret"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = ""
    if _HEX64.match(value):
        return value
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = secrets.token_hex(32)
    _atomic_write(path, fresh + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return fresh


class SessionStore:
    def __init__(self) -> None:
        self._sessions: set[str] = set()

    def mint_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions.add(token)
        return token

    def verify_session(self, token: str) -> bool:
        return token in self._sessions


def write_bootstrap() -> str:
    token = secrets.token_urlsafe(32)
    path = ui_state_dir() / "bootstrap.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"token": token, "exp": time.time() + _BOOTSTRAP_TTL_SECONDS})
    _atomic_write(path, payload)
    return token


def consume_bootstrap(token: str) -> bool:
    path = ui_state_dir() / "bootstrap.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return False
    path.unlink(missing_ok=True)
    if not isinstance(payload, dict) or payload.get("token") != token:
        return False
    exp = payload.get("exp")
    return isinstance(exp, int | float) and time.time() < exp


def host_origin_ok(headers: dict[str, str], port: int) -> bool:
    host = headers.get("Host", "")
    if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return False
    origin = headers.get("Origin")
    if origin is None:
        return True
    return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def cookie_header(session: str) -> str:
    return f"Set-Cookie: rdm_ui={session}; HttpOnly; SameSite=Strict; Path=/"
