import json
import sys
import threading
import time
from pathlib import Path

import pytest
from rdm.gateway import upstream
from rdm.gateway.upstream import UpstreamChild, UpstreamError

FAKE = r'''
import json, os, sys, time
mode = sys.argv[1]
rec = sys.argv[2] if len(sys.argv) > 2 else None
active = 0
max_active = 0
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
tools = [{"name": n, "inputSchema": {"type": "object"}} for n in ("echo", "boom", "slow", "notify")]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m = msg.get("method")
    mid = msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18",
              "capabilities": {"tools": {"listChanged": True}},
              "serverInfo": {"name": mode, "version": "0"}, "instructions": "fake"}})
    elif m == "notifications/initialized":
        pass
    elif m == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
    elif m == "notifications/cancelled":
        if rec:
            with open(rec, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"cancelled": msg["params"]["requestId"]}) + "\n")
    elif m == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments") or {}
        meta = msg["params"].get("_meta") or {}
        if name == "boom":
            sys.stderr.write("boom-stderr-line\n")
            sys.stderr.flush()
            os._exit(3)
        active_now = 0
        if name == "slow":
            globals()["active"] += 1
            active_now = active
            globals()["max_active"] = max(max_active, active_now)
        if rec:
            with open(rec, "a", encoding="utf-8") as fh:
                payload = {"id": mid, "name": name, "t": time.time()}
                if name == "slow":
                    payload["max_active"] = max_active
                fh.write(json.dumps(payload) + "\n")
        if name == "slow":
            time.sleep(float(args.get("secs", 0.3)))
            globals()["active"] -= 1
        if name == "notify":
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": meta.get("progressToken"), "progress": 1}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
            {"type": "text", "text": json.dumps({"name": name, "args": args})}]}})
'''


def make_child(tmp_path: Path, name: str, rec: str | None = None) -> tuple[UpstreamChild, list[dict]]:
    notes: list[dict] = []
    child = UpstreamChild(
        name,
        sys.executable,
        ["-c", FAKE, name] + ([rec] if rec else []),
        None,
        notes.append,
        log_dir=tmp_path,
    )
    return child, notes


def test_start_handshake_and_tools(tmp_path):
    child, _ = make_child(tmp_path, "echo1")
    try:
        assert child.start(deadline=10)
        assert child.live
        assert child.capabilities == {"tools": {"listChanged": True}}
        assert child.instructions == "fake"
        assert [tool["name"] for tool in child.tools] == ["echo", "boom", "slow", "notify"]
        result = child.request("tools/call", {"name": "echo", "arguments": {"a": 1}}, timeout=5)
        assert json.loads(result["content"][0]["text"])["args"] == {"a": 1}
    finally:
        child.close()


def test_response_routing_with_colliding_upstream_ids(tmp_path):
    first, _ = make_child(tmp_path, "route-a")
    second, _ = make_child(tmp_path, "route-b")
    assert first.start(deadline=10) and second.start(deadline=10)
    results: dict[str, dict] = {}

    def call(child: UpstreamChild, key: str, value: str) -> None:
        results[key] = child.request("tools/call", {"name": "echo", "arguments": {"v": value}}, timeout=5)

    threads = [
        threading.Thread(target=call, args=(first, "a", "alpha")),
        threading.Thread(target=call, args=(second, "b", "beta")),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert json.loads(results["a"]["content"][0]["text"])["args"] == {"v": "alpha"}
        assert json.loads(results["b"]["content"][0]["text"])["args"] == {"v": "beta"}
    finally:
        first.close()
        second.close()


def test_progress_notification_forwarded(tmp_path):
    child, notes = make_child(tmp_path, "notify1")
    assert child.start(deadline=10)
    try:
        child.request(
            "tools/call",
            {"name": "notify", "arguments": {}, "_meta": {"progressToken": "tk1"}},
            timeout=5,
        )
        deadline = time.time() + 5
        while time.time() < deadline and not notes:
            time.sleep(0.05)
        assert notes and notes[0]["method"] == "notifications/progress"
        assert notes[0]["params"]["progressToken"] == "tk1"
    finally:
        child.close()


def test_cancel_upstream_reaches_child_with_upstream_id(tmp_path):
    rec = tmp_path / "cancel.log"
    child, _ = make_child(tmp_path, "cancel1", str(rec))
    assert child.start(deadline=10)
    holder: dict = {}

    def slow_call() -> None:
        try:
            child.request("tools/call", {"name": "slow", "arguments": {"secs": 1.5}}, timeout=10)
        except UpstreamError as error:
            holder["error"] = str(error)

    thread = threading.Thread(target=slow_call)
    try:
        thread.start()
        deadline = time.time() + 5
        first_id = None
        while time.time() < deadline and first_id is None:
            if rec.exists():
                for line in rec.read_text(encoding="utf-8").splitlines():
                    entry = json.loads(line)
                    if "id" in entry:
                        first_id = entry["id"]
            time.sleep(0.05)
        assert first_id is not None
        child.cancel_upstream(first_id)
        deadline = time.time() + 5
        seen = False
        while time.time() < deadline and not seen:
            seen = any(
                json.loads(line).get("cancelled") == first_id
                for line in rec.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            time.sleep(0.05)
        assert seen
    finally:
        child.close()
        thread.join(timeout=10)


def test_concurrency_one_per_child(tmp_path):
    rec = tmp_path / "serial.log"
    child, _ = make_child(tmp_path, "serial1", str(rec))
    assert child.start(deadline=10)
    threads = [
        threading.Thread(
            target=lambda: child.request("tools/call", {"name": "slow", "arguments": {"secs": 0.3}}, timeout=10)
        )
        for _ in range(3)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        entries = [json.loads(line) for line in rec.read_text(encoding="utf-8").splitlines()]
        slow_entries = [entry for entry in entries if entry.get("name") == "slow"]
        assert len(slow_entries) == 3
        assert max(entry["max_active"] for entry in slow_entries) == 1
        starts = sorted(entry["t"] for entry in slow_entries)
        assert starts[1] - starts[0] >= 0.25 and starts[2] - starts[1] >= 0.25
    finally:
        child.close()


def test_crash_mid_call_fails_pending_with_32603(tmp_path):
    child, _ = make_child(tmp_path, "crash1")
    assert child.start(deadline=10)
    try:
        with pytest.raises(UpstreamError) as excinfo:
            child.request("tools/call", {"name": "boom", "arguments": {}}, timeout=10)
        assert "-32603" in str(excinfo.value)
    finally:
        child.close()


def test_backoff_restart_brings_child_back(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "RESTART_BACKOFF_START", 0.05)
    child, _ = make_child(tmp_path, "restart1")
    assert child.start(deadline=10)
    try:
        with pytest.raises(UpstreamError):
            child.request("tools/call", {"name": "boom", "arguments": {}}, timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline and not child.live:
            time.sleep(0.05)
        assert child.live
        result = child.request("tools/call", {"name": "echo", "arguments": {"z": 9}}, timeout=5)
        assert json.loads(result["content"][0]["text"])["args"] == {"z": 9}
    finally:
        child.close()


def test_stderr_lands_in_rotating_log(tmp_path):
    child, _ = make_child(tmp_path, "stderr1")
    assert child.start(deadline=10)
    try:
        with pytest.raises(UpstreamError):
            child.request("tools/call", {"name": "boom", "arguments": {}}, timeout=10)
        deadline = time.time() + 5
        log = tmp_path / "gateway-stderr1.log"
        while time.time() < deadline and not (log.exists() and "boom-stderr-line" in log.read_text(encoding="utf-8")):
            time.sleep(0.05)
        assert "boom-stderr-line" in log.read_text(encoding="utf-8")
    finally:
        child.close()


def test_close_kills_tree(tmp_path):
    child, _ = make_child(tmp_path, "close1")
    assert child.start(deadline=10)
    proc = child._proc
    assert proc is not None
    child.close()
    assert not child.live
    assert proc.poll() is not None


def test_start_failure_returns_false_without_orphan(tmp_path):
    child, _ = make_child(tmp_path, "bad1")
    child._command = "definitely-not-a-command-xyz"
    assert child.start(deadline=3) is False
    assert not child.live
    child.close()
