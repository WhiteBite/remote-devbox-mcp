import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rdm.events import sink
from rdm.proxy import access_log

_HOME_DIR = str(Path(access_log.__file__).resolve().parents[2])

_LOCK_CHILD = (
    "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
    "from rdm.proxy import access_log; "
    "print('ACQUIRED' if access_log.try_lock(Path(sys.argv[2])) else 'BLOCKED')"
)

_APPEND_CHILD = (
    "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
    "from rdm.proxy import access_log; "
    "p = Path(sys.argv[2]); tag = sys.argv[3]; "
    "[access_log.append_text(p, tag + str(i) + 'x' * 200 + '\\n') for i in range(50)]"
)


def _child(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _LOCK_CHILD, _HOME_DIR, *args],
        capture_output=True, text=True, timeout=60,
    )


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_try_lock_blocks_second_process(tmp_path):
    path = tmp_path / "access.log"
    fd = access_log._acquire(path)
    try:
        assert _child(str(path)).stdout.strip() == "BLOCKED"
    finally:
        access_log._release(fd)
    assert _child(str(path)).stdout.strip() == "ACQUIRED"


def test_cross_process_appends_stay_intact(tmp_path):
    path = tmp_path / "events.jsonl"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _APPEND_CHILD, _HOME_DIR, str(path), f"w{k}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        for k in range(2)
    ]
    for proc in procs:
        proc.wait(timeout=60)
        assert proc.returncode == 0
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    assert all(re.fullmatch(r"w[01]\d+x{200}", line) for line in lines)


def test_append_text_serializes_through_lockfile(tmp_path):
    path = tmp_path / "access.log"
    access_log.append_text(path, "one\n")
    assert (tmp_path / "access.log.lock").exists()


def test_apply_retention_deletes_old_rotated(tmp_path):
    path = tmp_path / "access.log"
    rotated = tmp_path / "access.log.1"
    path.write_text("fresh\n", encoding="utf-8")
    rotated.write_text("old\n", encoding="utf-8")
    _age(rotated, 10)
    assert access_log.apply_retention(path, days=7) == ["access.log.1"]
    assert not rotated.exists()
    assert path.exists()


def test_apply_retention_keeps_recent_rotated(tmp_path):
    path = tmp_path / "access.log"
    rotated = tmp_path / "access.log.1"
    path.write_text("fresh\n", encoding="utf-8")
    rotated.write_text("recent\n", encoding="utf-8")
    _age(rotated, 1)
    assert access_log.apply_retention(path, days=7) == []
    assert rotated.exists()


def test_apply_retention_purges_stale_active_log(tmp_path):
    path = tmp_path / "access.log"
    path.write_text("stale\n", encoding="utf-8")
    _age(path, 10)
    assert access_log.apply_retention(path, days=7) == ["access.log.1"]
    assert not path.exists()
    assert not (tmp_path / "access.log.1").exists()


def test_cleanup_prunes_events_via_default_path(tmp_path, monkeypatch):
    events = tmp_path / "events.jsonl"
    rotated = tmp_path / "events.jsonl.1"
    monkeypatch.setenv("RDM_EVENTS_PATH", str(events))
    events.write_text('{"kind":"http"}\n', encoding="utf-8")
    rotated.write_text('{"kind":"old"}\n', encoding="utf-8")
    _age(rotated, 30)
    assert sink.cleanup() == ["events.jsonl.1"]
    assert events.exists()


def test_cleanup_prunes_access_via_default_path(tmp_path, monkeypatch):
    path = tmp_path / "access.log"
    rotated = tmp_path / "access.log.1"
    monkeypatch.setenv("ACCESS_LOG", str(path))
    path.write_text("x\n", encoding="utf-8")
    rotated.write_text("y\n", encoding="utf-8")
    _age(rotated, 30)
    assert access_log.cleanup() == ["access.log.1"]
