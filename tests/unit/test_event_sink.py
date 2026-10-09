from __future__ import annotations

import json
import threading

from rdm.events import sink
from rdm.proxy import access_log


def _events_file(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("RDM_EVENTS_PATH", str(path))
    return path


def test_emit_threaded_appends_intact_lines(tmp_path, monkeypatch):
    path = _events_file(tmp_path, monkeypatch)
    per_thread = 200

    def worker(worker_id: int) -> None:
        for i in range(per_thread):
            sink.emit({"kind": "http", "session": f"s{worker_id}-{i}", "port": 8787})

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 * per_thread
    parsed = [json.loads(line) for line in lines]
    assert all(obj["kind"] == "http" for obj in parsed)
    assert len({obj["session"] for obj in parsed}) == 2 * per_thread


def test_emit_redacts_token_in_values(tmp_path, monkeypatch):
    path = _events_file(tmp_path, monkeypatch)
    sink.emit({"kind": "http", "method": "GET", "session": "token=supersecret42"})
    line = path.read_text(encoding="utf-8").splitlines()[0]
    assert "supersecret42" not in line
    assert "[REDACTED]" in line


def test_emit_rotates_on_size(tmp_path, monkeypatch):
    _events_file(tmp_path, monkeypatch)
    monkeypatch.setattr(access_log, "_CHECK_EVERY", 1)
    monkeypatch.setattr(access_log, "_MAX_BYTES", 10)
    sink.emit({"kind": "http", "port": 8787})
    rotated = tmp_path / "events.jsonl.1"
    assert rotated.exists()
    assert len(rotated.read_text(encoding="utf-8").splitlines()) == 1


def test_emit_drops_args_and_output(tmp_path, monkeypatch):
    path = _events_file(tmp_path, monkeypatch)
    sink.emit({"kind": "tool", "tool": "bash", "args": {"command": "ls"}, "output": "secret.txt"})
    obj = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "args" not in obj
    assert "output" not in obj
    assert obj["tool"] == "bash"


def test_emit_persists_permission_id_and_job_id(tmp_path, monkeypatch):
    path = _events_file(tmp_path, monkeypatch)
    sink.emit(
        {
            "kind": "mcp_response",
            "job_id": "job-1",
            "status": "awaiting_permission",
            "permission": "bash",
            "permission_id": "perm-4",
        }
    )
    obj = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert obj["permission_id"] == "perm-4"
    assert obj["job_id"] == "job-1"
