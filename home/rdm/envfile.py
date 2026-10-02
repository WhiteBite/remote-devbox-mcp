from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

_KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)=")

_CANONICAL_ORDER = (
    "PROJECT_DIR", "TOOLCHAIN", "GIT_NAME", "GIT_EMAIL", "PREVIEW_ORIGIN",
    "SELF_AUTHED_PORTS", "ALLOWED_PORTS", "OPENCODE_MCP_PERMISSIONS",
    "SETUP_SCRIPT_B64", "PUBLIC_URL", "ACTIVE_PROFILE", "MCP_BEARER_TOKEN",
    "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN", "VLESS_SUB_URL",
    "JOB_TIMEOUT_SECONDS", "TUNNEL_TOKEN", "TUNNEL_TAIL",
)
_CANONICAL_KEYS = frozenset(_CANONICAL_ORDER)


def _index_key_lines(lines: list[str]) -> dict[str, int]:
    key_pos: dict[str, int] = {}
    for i, line in enumerate(lines):
        match = _KEY_LINE.match(line)
        if match is not None and match.group(1) not in key_pos:
            key_pos[match.group(1)] = i
    return key_pos


class EnvFile:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._key_pos: dict[str, int] = {}
        self._pending: dict[str, str] = {}

    @classmethod
    def load(cls, path: str | Path) -> EnvFile:
        try:
            # utf-8-sig: .env может прийти с BOM от Windows-редакторов
            text = Path(path).read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return cls()
        normalized = text.replace("\r\n", "\n")
        lines = normalized.split("\n") if normalized else []
        if normalized.endswith("\n"):
            lines.pop()
        env = cls()
        env._lines = lines
        env._key_pos = _index_key_lines(lines)
        return env

    def get(self, key: str, default: str | None = None) -> str | None:
        pos = self._key_pos.get(key)
        if pos is not None:
            return self._lines[pos].split("=", 1)[1].strip()
        return self._pending.get(key, default)

    def set(self, key: str, value: str) -> None:
        pos = self._key_pos.get(key)
        if pos is not None:
            self._lines[pos] = f"{key}={value}"
        else:
            self._pending[key] = value

    def remove(self, key: str) -> None:
        pos = self._key_pos.pop(key, None)
        if pos is not None:
            del self._lines[pos]
            self._key_pos = _index_key_lines(self._lines)
        self._pending.pop(key, None)

    def as_map(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for i, line in enumerate(self._lines):
            match = _KEY_LINE.match(line)
            if match is not None and self._key_pos.get(match.group(1)) == i:
                result[match.group(1)] = line.split("=", 1)[1].strip()
        result.update(self._pending)
        return result

    def render(self) -> str:
        canonical = [k for k in _CANONICAL_ORDER if k in self._pending]
        manual = sorted(set(self._pending) - _CANONICAL_KEYS)
        appended = [f"{k}={self._pending[k]}" for k in [*canonical, *manual]]
        lines = [*self._lines, *appended]
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    def write(self, path: str | Path) -> None:
        target = Path(path)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.render())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
