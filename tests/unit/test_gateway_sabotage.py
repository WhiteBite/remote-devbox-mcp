import json
import sys
import threading

import pytest
from rdm import hostos, profiles
from rdm.events import sink
from rdm.gateway import dispatch, sessions
from rdm.gateway.sessions import SessionManager, SessionPolicy
from rdm.gateway.snapshot import (
    GatewaySnapshotError,
    Pin,
    ServerSnapshot,
    Snapshot,
    load_verified,
    save_snapshot,
)

FAKE = r'''
import json, sys
mode = sys.argv[1]
rec = sys.argv[2] if len(sys.argv) > 2 else None
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
tools = [{"name": n, "inputSchema": {"type": "object"}}
         for n in ("echo", "execute_any")]
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
    elif m == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments") or {}
        if rec:
            with open(rec, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": mid, "name": name}) + "\n")
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
            {"type": "text", "text": json.dumps({"name": name, "args": args})}]}})
'''

FAKE_TOOLS = frozenset({"echo", "execute_any"})


def fake_server(name: str, rec: str | None = None):
    args = ["-c", FAKE, name] + ([rec] if rec else [])
    return (
        name,
        ServerSnapshot(
            command=sys.executable,
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


def test_default_deny_server_refused_even_when_allowlisted(tmp_path):
    manual = SessionPolicy(allowlists={"stitch-manager": frozenset({"echo"})})
    assert manual.check_call("stitch-manager", "echo", {}) == (False, "default-deny")

    rec = tmp_path / "deny.log"
    manager = make_manager(
        [fake_server("stitch-manager", str(rec))],
        allowlists={"stitch-manager": frozenset({"echo", "execute_any"})},
    )
    session = manager.create_session()
    try:
        denied = session.call_tool("stitch-manager__echo", {})
        assert denied.is_error
        assert denied.content[0]["text"] == "default-deny"
        assert not rec.exists()
    finally:
        manager.close_all()


def test_meta_tool_hard_denied_even_when_allowlisted(tmp_path):
    rec = tmp_path / "meta.log"
    manager = make_manager(
        [fake_server("alpha", str(rec))],
        allowlists={"alpha": frozenset({"echo", "execute_any"})},
    )
    session = manager.create_session()
    try:
        names = [tool[0] for tool in session.list_tools()]
        assert names == ["alpha__echo"]
        denied = session.call_tool("alpha__execute_any", {})
        assert denied.is_error
        assert denied.content[0]["text"] == "meta-tool-deny"
        ok = session.call_tool("alpha__echo", {"v": 1})
        assert not ok.is_error
        entries = [json.loads(line) for line in rec.read_text(encoding="utf-8").splitlines()]
        assert [entry["name"] for entry in entries] == ["echo"]
    finally:
        manager.close_all()


def test_concurrent_colliding_upstream_ids_route_to_own_caller(tmp_path):
    manager = make_manager([fake_server("alpha"), fake_server("beta")])
    session = manager.create_session()
    errors: list[str] = []

    def call(exposed: str, value: str) -> None:
        result = session.call_tool(exposed, {"v": value})
        if result.is_error:
            errors.append(f"{exposed}: {result.content[0]['text']}")
            return
        payload = json.loads(result.content[0]["text"])
        if payload["args"] != {"v": value}:
            errors.append(f"{exposed}: got {payload['args']}")

    try:
        for round_no in range(8):
            threads = [
                threading.Thread(target=call, args=("alpha__echo", f"alpha-{round_no}")),
                threading.Thread(target=call, args=("beta__echo", f"beta-{round_no}")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        assert errors == []
        assert session._children["alpha"]._next_id == session._children["beta"]._next_id
    finally:
        manager.close_all()


def test_close_all_kills_every_child_via_kill_tree(tmp_path, monkeypatch):
    kills: list[int] = []
    original = hostos.kill_tree

    def spy(pid: int) -> None:
        kills.append(pid)
        original(pid)

    monkeypatch.setattr(hostos, "kill_tree", spy)
    manager = make_manager([fake_server("alpha"), fake_server("beta")])
    session = manager.create_session()
    procs = []
    for child in session._children.values():
        assert child._proc is not None
        procs.append(child._proc)
    manager.close_all()
    assert manager.get(session.id) is None
    for proc in procs:
        assert proc.poll() is not None
    assert sorted(kills) == sorted(proc.pid for proc in procs)


def test_snapshot_drift_refuses_session_creation(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "opencode.json").write_text(
        json.dumps({"mcp": {"alpha": {"type": "local", "command": [sys.executable, "-c", FAKE, "alpha"]}}}),
        encoding="utf-8",
    )
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile_path = profiles_dir / "sabotage-drift.json"
    profile_path.write_text(
        json.dumps(
            {
                "project_dir": str(project),
                "exposed_mcps": [{"name": "alpha", "mode": "readonly", "tool_allow": ["echo"]}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(profiles_dir))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(sessions, "_ENV_FILE", tmp_path / ".env")
    (tmp_path / ".env").write_text("ACTIVE_PROFILE=sabotage-drift\n", encoding="utf-8")

    profile = profiles.load(profile_path)
    save_snapshot(profile_path, profile)
    load_verified(profile_path, profile)

    (project / "opencode.json").write_text(
        json.dumps({"mcp": {"alpha": {"type": "local", "command": [sys.executable, "-c", FAKE, "mutated"]}}}),
        encoding="utf-8",
    )

    manager = SessionManager()
    with pytest.raises(GatewaySnapshotError, match="drift"):
        manager.create_session()


def test_joint_exposure_egress_and_secret_rejected(tmp_path):
    profile = profiles.Profile(
        project_dir=str(tmp_path),
        git_name="sabotage",
        git_email="sabotage@example.com",
        exposed_mcps=(
            profiles.ExposedMcp(name="websearch"),
            profiles.ExposedMcp(name="stitch-manager"),
        ),
    )
    problems = profiles.validate(profile)
    assert any("R41" in problem for problem in problems)


def test_audit_event_carries_no_argument_values(tmp_path, monkeypatch):
    events: list[dict] = []
    monkeypatch.setattr(sink, "emit", events.append)
    manager = make_manager([fake_server("alpha")])
    session = manager.create_session()
    try:
        secret = "sk-sabotage-secret-do-not-leak-0123456789"
        result = dispatch.call(session, session.id, "alpha__echo", {"password": secret}, None, 5)
        assert not result.is_error
        assert secret in result.content[0]["text"]
        assert len(events) == 1
        event = events[0]
        assert set(event) == {
            "kind",
            "session",
            "server",
            "tool",
            "rpc_id",
            "status",
            "latency_ms",
            "bytes",
            "truncated",
        }
        dumped = json.dumps(event)
        assert secret not in dumped
        assert "password" not in dumped
        assert event["kind"] == "gateway"
        assert event["tool"] == "echo"
        assert event["status"] == "ok"
    finally:
        manager.close_all()
