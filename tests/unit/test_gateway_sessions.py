import json
import sys
import threading
import time

import pytest
from rdm.gateway import sessions, upstream
from rdm.gateway.server import SessionLimitError
from rdm.gateway.sessions import SessionManager, SessionPolicy
from rdm.gateway.snapshot import Pin, ServerSnapshot, Snapshot

FAKE = r'''
import json, os, sys, time
mode = sys.argv[1]
rec = sys.argv[2] if len(sys.argv) > 2 else None
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
tools = [{"name": n, "inputSchema": {"type": "object"}}
         for n in ("echo", "boom", "slow", "notify", "list_mcp_commands")]
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
            os._exit(3)
        if rec:
            with open(rec, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": mid, "name": name}) + "\n")
        if name == "slow":
            time.sleep(float(args.get("secs", 0.3)))
        if name == "notify":
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": meta.get("progressToken"), "progress": 1}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
            {"type": "text", "text": json.dumps({"name": name, "args": args})}]}})
'''

FAKE_TOOLS = frozenset({"echo", "boom", "slow", "notify", "list_mcp_commands"})


def fake_server(name: str, rec: str | None = None, command: str | None = None):
    args = ["-c", FAKE, name] + ([rec] if rec else [])
    return (
        name,
        ServerSnapshot(
            command=command or sys.executable,
            args=tuple(args),
            env=(),
            pins=Pin(specifier="fake", sha256="0" * 64),
        ),
    )


def make_manager(servers=(), allowlists=None, **kwargs) -> SessionManager:
    snap = Snapshot(servers=tuple(servers))
    if allowlists is None:
        allowlists = {name: FAKE_TOOLS for name, _ in snap.servers}
    return SessionManager(
        snapshot_loader=lambda: snap,
        policy=SessionPolicy(allowlists=allowlists),
        **kwargs,
    )


def test_exclusive_children_per_session(tmp_path):
    manager = make_manager([fake_server("alpha")])
    first = manager.create_session()
    second = manager.create_session()
    try:
        first_pid = first._children["alpha"].pid
        second_pid = second._children["alpha"].pid
        assert first_pid and second_pid and first_pid != second_pid
        assert first._children["alpha"].live and second._children["alpha"].live
    finally:
        manager.close_all()


def test_dead_upstream_degrades_session(tmp_path):
    manager = make_manager([fake_server("alpha", command="definitely-not-a-command-xyz")])
    session = manager.create_session()
    try:
        assert session.list_tools() == []
        assert session.capabilities == {}
        result = session.call_tool("alpha__echo", {})
        assert result.is_error
        assert result.content[0]["text"] == "server unavailable: alpha"
    finally:
        manager.close_all()


def test_concurrent_calls_route_across_children_with_colliding_ids(tmp_path):
    manager = make_manager([fake_server("alpha"), fake_server("beta")])
    session = manager.create_session()
    results: dict[str, object] = {}

    def call(exposed: str, value: str) -> None:
        results[exposed] = session.call_tool(exposed, {"v": value})

    threads = [
        threading.Thread(target=call, args=("alpha__echo", "alpha-val")),
        threading.Thread(target=call, args=("beta__echo", "beta-val")),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        first = results["alpha__echo"]
        second = results["beta__echo"]
        assert not first.is_error and not second.is_error
        assert json.loads(first.content[0]["text"])["args"] == {"v": "alpha-val"}
        assert json.loads(second.content[0]["text"])["args"] == {"v": "beta-val"}
    finally:
        manager.close_all()


def test_progress_notification_lands_in_session_queue(tmp_path):
    manager = make_manager([fake_server("alpha")])
    session = manager.create_session()
    try:
        result = session.call_tool("alpha__notify", {}, progress_token="tk9")
        assert not result.is_error
        note = None
        deadline = time.time() + 5
        while time.time() < deadline and note is None:
            try:
                note = session.notifications.get(timeout=0.1)
            except Exception:
                pass
        assert note is not None
        assert note["method"] == "notifications/progress"
        assert note["params"]["progressToken"] == "tk9"
    finally:
        manager.close_all()


def test_cancel_routes_to_owning_child_with_upstream_id(tmp_path):
    rec = tmp_path / "cancel.log"
    manager = make_manager([fake_server("alpha", str(rec))])
    session = manager.create_session()
    holder: dict = {}

    def slow_call() -> None:
        holder["result"] = session.call_tool("alpha__slow", {"secs": 1.5}, rpc_id=77)

    thread = threading.Thread(target=slow_call)
    try:
        thread.start()
        upstream_id = None
        deadline = time.time() + 5
        while time.time() < deadline and upstream_id is None:
            if rec.exists():
                for line in rec.read_text(encoding="utf-8").splitlines():
                    entry = json.loads(line)
                    if "id" in entry:
                        upstream_id = entry["id"]
            time.sleep(0.05)
        assert upstream_id is not None
        session.cancel(77)
        seen = False
        deadline = time.time() + 5
        while time.time() < deadline and not seen:
            seen = any(
                json.loads(line).get("cancelled") == upstream_id
                for line in rec.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            time.sleep(0.05)
        assert seen
    finally:
        manager.close_all()
        thread.join(timeout=10)


def test_list_filtering_allowlist_and_meta_deny(tmp_path):
    allow = {"alpha": frozenset({"echo", "slow", "list_mcp_commands"})}
    manager = make_manager([fake_server("alpha")], allowlists=allow)
    session = manager.create_session()
    try:
        names = [tool[0] for tool in session.list_tools()]
        assert names == ["alpha__echo", "alpha__slow"]
        result = session.call_tool("alpha__list_mcp_commands", {})
        assert result.is_error
    finally:
        manager.close_all()


def test_policy_denial_never_touches_upstream(tmp_path):
    rec = tmp_path / "deny.log"
    manager = make_manager(
        [fake_server("alpha", str(rec))], allowlists={"alpha": frozenset({"echo"})}
    )
    session = manager.create_session()
    try:
        denied = session.call_tool("alpha__boom", {})
        assert denied.is_error
        assert not rec.exists()
        ok = session.call_tool("alpha__echo", {"v": 1})
        assert not ok.is_error
        entries = [json.loads(line) for line in rec.read_text(encoding="utf-8").splitlines()]
        assert [entry["name"] for entry in entries] == ["echo"]
    finally:
        manager.close_all()


def test_close_all_kills_all_children(tmp_path):
    manager = make_manager([fake_server("alpha"), fake_server("beta")])
    first = manager.create_session()
    second = manager.create_session()
    procs = []
    for session in (first, second):
        for child in session._children.values():
            assert child._proc is not None
            procs.append(child._proc)
    manager.close_all()
    assert manager.get(first.id) is None and manager.get(second.id) is None
    for proc in procs:
        assert proc.poll() is not None


def test_fifth_session_rejected(tmp_path):
    manager = make_manager()
    created = [manager.create_session() for _ in range(4)]
    with pytest.raises(SessionLimitError):
        manager.create_session()
    manager.close_all()
    assert all(manager.get(session.id) is None for session in created)


def test_idle_reaper_closes_stale_session(tmp_path):
    manager = make_manager([fake_server("alpha")], idle_seconds=0.5)
    stale = manager.create_session()
    fresh = manager.create_session()
    child = stale._children["alpha"]
    assert child._proc is not None
    try:
        time.sleep(0.8)
        fresh.touch()
        manager.reap_idle()
        assert manager.get(stale.id) is None
        assert manager.get(fresh.id) is fresh
        assert child._proc.poll() is not None
    finally:
        manager.close_all()


def test_crash_mid_session_degrades_only_that_server(tmp_path):
    manager = make_manager([fake_server("alpha"), fake_server("beta")])
    session = manager.create_session()
    try:
        crashed = session.call_tool("alpha__boom", {})
        assert crashed.is_error
        assert "-32603" in crashed.content[0]["text"]
        unavailable = session.call_tool("alpha__echo", {})
        assert unavailable.is_error
        assert unavailable.content[0]["text"] == "server unavailable: alpha"
        ok = session.call_tool("beta__echo", {"v": "still-alive"})
        assert not ok.is_error
        assert json.loads(ok.content[0]["text"])["args"] == {"v": "still-alive"}
    finally:
        manager.close_all()


def test_default_manager_builds_defaults_lazily(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "_ENV_FILE", tmp_path / ".env")
    manager = SessionManager()
    assert manager._snapshot_loader is None and manager._policy is None
    session = manager.create_session()
    try:
        assert session.list_tools() == []
        assert session.capabilities == {}
    finally:
        manager.close_all()


def test_session_surface_instructions_and_capabilities(tmp_path):
    manager = make_manager([fake_server("alpha"), fake_server("beta")])
    session = manager.create_session()
    try:
        assert session.server_info_name == "devbox-gateway"
        assert "alpha" in session.instructions and "beta" in session.instructions
        assert "<server>__<tool>" in session.instructions
        assert session.capabilities == {"tools": {"listChanged": True}}
    finally:
        manager.close_all()


def test_oversized_text_item_capped_and_truncated(tmp_path):
    manager = make_manager([fake_server("alpha")])
    session = manager.create_session()
    try:
        result = session.call_tool("alpha__echo", {"v": "x" * 1_000_100})
        assert not result.is_error
        assert result.truncated
        assert len(result.content[0]["text"]) == sessions.MAX_TEXT_ITEM_CHARS
    finally:
        manager.close_all()


def test_upstream_inflight_id_lifecycle(tmp_path):
    child = upstream.UpstreamChild(
        "inflight", sys.executable, ["-c", FAKE, "inflight"], None, lambda note: None,
        log_dir=tmp_path,
    )
    assert child.start(deadline=10)
    holder: dict = {}

    def slow() -> None:
        holder["result"] = child.request("tools/call", {"name": "slow", "arguments": {"secs": 0.5}}, timeout=10)

    thread = threading.Thread(target=slow)
    try:
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and child.inflight_id is None:
            time.sleep(0.05)
        assert child.inflight_id is not None
        thread.join(timeout=10)
        assert child.inflight_id is None
    finally:
        child.close()
