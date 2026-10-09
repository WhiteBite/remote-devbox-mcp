"""Size-rotated access log for the host proxy, with control characters escaped.

Appends are serialized across processes through a per-file lockfile: the
proxy, the cockpit and one-shot CLI writers share events.jsonl and access.log.
Rotated copies older than RETENTION_DAYS are pruned by apply_retention.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import tempfile
import threading
import time
from typing import TextIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_handles: dict[pathlib.Path, TextIO] = {}
_lock = threading.Lock()
_held_locks: list[int] = []
_writes = 0
_MAX_BYTES = 10 * 1024 * 1024
_CHECK_EVERY = 512
RETENTION_DAYS = 7


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


def _lock_path(path: pathlib.Path) -> pathlib.Path:
    return path.with_name(path.name + ".lock")


def _acquire(path: pathlib.Path) -> int:
    fd = os.open(_lock_path(path), os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _release(fd: int) -> None:
    try:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def try_lock(path: pathlib.Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_lock_path(path), os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _held_locks.append(fd)
    return True


def _rotate(path: pathlib.Path) -> None:
    handle = _handles.pop(path, None)
    if handle is not None:
        handle.close()
    try:
        path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def append_text(path: pathlib.Path, line: str) -> None:
    global _writes
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        guard = _acquire(path)
        try:
            handle = _handles.get(path)
            if handle is None:
                handle = open(path, "a", encoding="utf-8")
                _handles[path] = handle
            handle.write(line)
            handle.flush()
            _writes += 1
            if _writes % _CHECK_EVERY == 0 and path.stat().st_size > _MAX_BYTES:
                _rotate(path)
        finally:
            _release(guard)


def apply_retention(path: pathlib.Path, days: int = RETENTION_DAYS) -> list[str]:
    cutoff = time.time() - days * 86400
    rotated = path.with_name(path.name + ".1")
    removed: list[str] = []
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        guard = _acquire(path)
        try:
            if path.exists() and path.stat().st_size > 0 and path.stat().st_mtime < cutoff:
                _rotate(path)
            if rotated.exists() and rotated.stat().st_mtime < cutoff:
                try:
                    rotated.unlink()
                    removed.append(rotated.name)
                except OSError:
                    pass
        finally:
            _release(guard)
    return removed


def cleanup(days: int = RETENTION_DAYS) -> list[str]:
    return apply_retention(default_path(), days)


def log(path: pathlib.Path, port: int, method: str, target: str, auth_mode: str) -> None:
    try:
        line = (
            f"{datetime.datetime.now().isoformat()} {port} "
            f"{_escape(method.encode('latin-1'))} {_escape(target.encode('latin-1'))[:120]} {auth_mode}\n"
        )
        append_text(path, line)
    except OSError:
        pass
