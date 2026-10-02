"""Size-rotated access log for the host proxy, with control characters escaped."""

from __future__ import annotations

import datetime
import os
import pathlib
import tempfile
import threading
from typing import TextIO

_handles: dict[pathlib.Path, TextIO] = {}
_lock = threading.Lock()
_writes = 0
_MAX_BYTES = 10 * 1024 * 1024
_CHECK_EVERY = 512


def default_path() -> pathlib.Path:
    override = os.environ.get("ACCESS_LOG")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(tempfile.gettempdir()) / "rdm-ingress" / "access.log"


def _escape(raw: bytes) -> str:
    out: list[str] = []
    for byte in raw:
        char = chr(byte)
        if 0x21 <= byte <= 0x7E:
            out.append(char)
        else:
            out.append(f"\\x{byte:02x}")
    return "".join(out)


def log(path: pathlib.Path, port: int, method: str, target: str, auth_mode: str) -> None:
    global _writes
    try:
        line = (
            f"{datetime.datetime.now().isoformat()} {port} "
            f"{_escape(method.encode('latin-1'))} {_escape(target.encode('latin-1'))[:120]} {auth_mode}\n"
        )
        with _lock:
            handle = _handles.get(path)
            if handle is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(path, "a", encoding="utf-8")
                _handles[path] = handle
            handle.write(line)
            handle.flush()
            _writes += 1
            if _writes % _CHECK_EVERY == 0 and path.stat().st_size > _MAX_BYTES:
                handle.close()
                del _handles[path]
                path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass
