from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from rdm.bridge_client import BridgeClient, BridgeDenied

from stitch_devbox import jobcontrol, service
from tests.bridge_stub import TOKEN, BridgeStub, dead_bridge_url

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "plugin.json"


@pytest.fixture()
def bridge(monkeypatch):
    stub = BridgeStub()
    monkeypatch.setattr(jobcontrol, "_client", BridgeClient(stub.url, TOKEN))
    yield stub
    stub.close()


@pytest.fixture()
def events_path(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("RDM_EVENTS_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def reset_bridge_health():
    jobcontrol._remember_bridge_up()
    yield
    jobcontrol._remember_bridge_up()


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def test_jobs_live_rows_from_bridge(bridge):
    bridge.jobs = [
        {"job_id": "job-1", "tool": "bash", "status": "running", "created_at": "2026-10-09T10:00:00Z"},
        {"job_id": "job-3", "tool": "edit", "status": "completed", "created_at": "2026-10-09T09:00:00Z"},
    ]

    payload = jobcontrol.jobs_live()

    assert payload == {
        "source": "bridge",
        "bridge": "up",
        "rows": [
            {"id": "job-1", "tool": "bash", "status": "running", "startedAt": "2026-10-09T10:00:00Z"}
        ],
    }


def test_jobs_live_falls_back_to_events_when_bridge_down(events_path, monkeypatch):
    monkeypatch.setattr(jobcontrol, "_client", BridgeClient(dead_bridge_url(), TOKEN))
    now = time.time()
    _write_events(
        events_path,
        [
            {"ts": now - 60, "kind": "mcp_request", "rpc_method": "tools/call", "tool": "bash"},
            {"ts": now - 59, "kind": "mcp_response", "job_id": "j1", "status": "running"},
            {"ts": now - 30, "kind": "mcp_response", "job_id": "j2", "status": "completed"},
        ],
    )

    payload = jobcontrol.jobs_live()

    assert payload == {
        "source": "events",
        "bridge": "down",
        "rows": [
            {"id": "j1", "tool": "bash", "status": "running", "startedAt": _iso(now - 60)}
        ],
    }


def test_jobs_live_degrades_on_generic_bridge_error(events_path, monkeypatch):
    import contextlib

    from rdm.bridge_client import BridgeError

    class _ErrorClient:
        def budget(self):
            return contextlib.nullcontext()

        def job_list(self):
            raise BridgeError("HTTP 502")

    monkeypatch.setattr(jobcontrol, "_client", _ErrorClient())
    _write_events(events_path, [])

    payload = jobcontrol.jobs_live()

    assert payload == {"source": "events", "bridge": "down", "rows": []}


def test_jobs_live_denied_propagates(bridge, monkeypatch):
    monkeypatch.setattr(jobcontrol, "_client", BridgeClient(bridge.url, "wrong-token"))

    with pytest.raises(BridgeDenied):
        jobcontrol.jobs_live()


def test_permissions_pending_unions_bridge_and_tap(events_path, bridge):
    bridge.jobs = [
        {
            "job_id": "job-2",
            "tool": "edit",
            "status": "awaiting_permission",
            "created_at": "2026-10-09T10:00:00Z",
        }
    ]
    bridge.requests = [
        {"job_id": "job-2", "id": "perm-2", "permission": "edit", "patterns": ["src/**"]}
    ]
    now = time.time()
    _write_events(
        events_path,
        [
            {"ts": now - 120, "kind": "mcp_request", "rpc_method": "tools/call", "tool": "bash"},
            {
                "ts": now - 119,
                "kind": "mcp_response",
                "job_id": "j9",
                "status": "awaiting_permission",
                "permission": "bash",
                "permission_id": "perm-7",
            },
            {
                "ts": now - 110,
                "kind": "mcp_response",
                "job_id": "j8",
                "status": "awaiting_permission",
                "permission": "edit",
                "permission_id": "perm-2",
            },
            {
                "ts": now - 105,
                "kind": "mcp_response",
                "job_id": "j7",
                "status": "awaiting_permission",
                "permission": "bash",
                "permission_id": "perm-8",
            },
            {"ts": now - 100, "kind": "mcp_response", "job_id": "j7", "status": "running"},
        ],
    )

    payload = jobcontrol.permissions_pending()

    rows = {row["id"]: row for row in payload["rows"]}
    assert set(rows) == {"perm-2", "perm-7"}
    assert rows["perm-2"] == {
        "id": "perm-2",
        "tool": "edit",
        "summary": "edit: src/**",
        "startedAt": "2026-10-09T10:00:00Z",
    }
    assert rows["perm-7"] == {
        "id": "perm-7",
        "tool": "bash",
        "summary": "bash",
        "startedAt": _iso(now - 119),
    }


def test_permissions_pending_tap_only_when_bridge_down(events_path, monkeypatch):
    monkeypatch.setattr(jobcontrol, "_client", BridgeClient(dead_bridge_url(), TOKEN))
    now = time.time()
    _write_events(
        events_path,
        [
            {"ts": now - 60, "kind": "mcp_request", "rpc_method": "tools/call", "tool": "bash"},
            {
                "ts": now - 59,
                "kind": "mcp_response",
                "job_id": "j1",
                "status": "awaiting_permission",
                "permission": "bash",
                "permission_id": "perm-7",
            },
        ],
    )

    payload = jobcontrol.permissions_pending()

    assert payload == {
        "bridge": "down",
        "rows": [
            {"id": "perm-7", "tool": "bash", "summary": "bash", "startedAt": _iso(now - 59)}
        ],
    }


def test_jobs_live_skips_bridge_while_down_cache_is_warm(events_path, monkeypatch):
    import contextlib

    from rdm.bridge_client import BridgeUnreachable

    calls: list[str] = []

    class _SpyClient:
        def budget(self):
            return contextlib.nullcontext()

        def job_list(self):
            calls.append("job_list")
            raise BridgeUnreachable("refused")

    monkeypatch.setattr(jobcontrol, "_client", _SpyClient())
    _write_events(events_path, [])

    first = jobcontrol.jobs_live()
    second = jobcontrol.jobs_live()
    pending = jobcontrol.permissions_pending()

    assert calls == ["job_list"]
    assert first["bridge"] == "down"
    assert second == {"source": "events", "bridge": "down", "rows": []}
    assert pending == {"bridge": "down", "rows": []}


def test_jobs_live_probes_again_after_down_cache_expires(events_path, bridge, monkeypatch):
    import contextlib

    from rdm.bridge_client import BridgeUnreachable

    class _SpyClient:
        def budget(self):
            return contextlib.nullcontext()

        def job_list(self):
            raise BridgeUnreachable("refused")

    monkeypatch.setattr(jobcontrol, "_client", _SpyClient())
    _write_events(events_path, [])
    assert jobcontrol.jobs_live()["bridge"] == "down"

    monkeypatch.setattr(jobcontrol, "_client", BridgeClient(bridge.url, TOKEN))
    monkeypatch.setattr(jobcontrol, "_bridge_down_until", 0.0)
    assert jobcontrol.jobs_live()["bridge"] == "up"


def test_job_cancel_accepted(bridge):
    result = jobcontrol.job_cancel("job-1")

    assert result == {"accepted": True, "status": "cancelling"}
    assert bridge.calls[-1] == {"name": "opencode_job_cancel", "arguments": {"job_id": "job-1"}}


def test_job_cancel_requires_job_id():
    with pytest.raises(ValueError):
        jobcontrol.job_cancel("")


def test_job_cancel_bridge_error_not_accepted(bridge):
    result = jobcontrol.job_cancel("unknown")

    assert result["accepted"] is False
    assert "Unknown or expired job_id" in result["reason"]


def test_permission_reply_accepted(bridge):
    bridge.requests = [{"job_id": "job-2", "id": "perm-2", "permission": "edit", "patterns": []}]

    result = jobcontrol.permission_reply("perm-2", "once")

    assert result == {"accepted": True, "status": "running"}
    assert bridge.calls[-1] == {
        "name": "opencode_permission_reply",
        "arguments": {"job_id": "job-2", "permission_id": "perm-2", "reply": "once"},
    }


def test_permission_reply_rejects_unknown_decision():
    with pytest.raises(ValueError):
        jobcontrol.permission_reply("perm-2", "maybe")


def test_permission_reply_requires_permission_id():
    with pytest.raises(ValueError):
        jobcontrol.permission_reply("", "once")


def test_permission_reply_unknown_permission_not_accepted(bridge):
    result = jobcontrol.permission_reply("perm-404", "reject")

    assert result["accepted"] is False
    assert "perm-404" in result["reason"]


def test_bridge_client_reads_token_from_env_file(monkeypatch, tmp_path):
    rdm = service._rdm()
    env_file = tmp_path / ".env"
    env_file.write_text("MCP_BEARER_TOKEN=secret-token-123\n", encoding="utf-8")
    monkeypatch.setattr(rdm.cli, "ENV_FILE", env_file)
    monkeypatch.setattr(jobcontrol, "_client", None)

    client = jobcontrol._bridge()

    assert client.token == "secret-token-123"
    assert client.url == f"http://127.0.0.1:{rdm.profiles.BRIDGE_PORT}/mcp"


def test_build_server_registers_jobcontrol_commands():
    from stitch_devbox import __main__ as plugin_main

    handlers = plugin_main._build_server()._handlers

    for name in ("jobs_live", "job_cancel", "permissions_pending", "permission_reply"):
        assert name in handlers


def test_handlers_pass_frozen_contract_params(monkeypatch):
    from stitch_devbox import __main__ as plugin_main

    seen: dict[str, Any] = {}

    def fake_job_cancel(job_id: str) -> dict[str, Any]:
        seen["job_cancel"] = job_id
        return {"accepted": True}

    def fake_permission_reply(permission_id: str, decision: str) -> dict[str, Any]:
        seen["permission_reply"] = (permission_id, decision)
        return {"accepted": True}

    monkeypatch.setattr(jobcontrol, "job_cancel", fake_job_cancel)
    monkeypatch.setattr(jobcontrol, "permission_reply", fake_permission_reply)

    plugin_main._handle_job_cancel({"jobId": "job-9"})
    plugin_main._handle_permission_reply({"permissionId": "perm-1", "decision": "reject"})

    assert seen == {"job_cancel": "job-9", "permission_reply": ("perm-1", "reject")}


def test_manifest_declares_jobcontrol_commands():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    commands = {command["name"]: command["readonly"] for command in manifest["contributions"]["commands"]}

    assert commands["jobs_live"] is True
    assert commands["permissions_pending"] is True
    assert commands["job_cancel"] is False
    assert commands["permission_reply"] is False
