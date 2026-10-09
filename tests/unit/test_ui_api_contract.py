from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from stitch_devbox import service

_STATUS_STUB = {
    "env": {"ACTIVE_PROFILE": "alpha"},
    "ports": {"ingress": True, "bridge": False, "runner": None},
    "watchdog": False,
    "host_services": {"alive": 2, "recorded": 3},
    "manifest": {"project_dir": "D:/work/alpha"},
    "compose_ps": "healthy",
}


def _profile_data(tmp_path: Path) -> dict[str, Any]:
    return {
        "project_dir": str(tmp_path),
        "git_name": "agent",
        "git_email": "agent@example.test",
        "preview_origin": "http://toolbox:8788",
        "mode": "standard",
    }


def _write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def test_api_exposes_public_builders():
    from rdm.ui import api

    for name in (
        "build_status",
        "jobs_payload",
        "permissions_payload",
        "exposure_payload",
        "diff_payload",
        "read_json",
        "read_events",
        "collect_logs",
    ):
        assert callable(getattr(api, name))


def test_server_reexports_api_builders():
    from rdm.ui import api, server

    assert server.build_status is api.build_status
    assert server.jobs_payload is api.jobs_payload
    assert server.permissions_payload is api.permissions_payload
    assert server.exposure_payload is api.exposure_payload
    assert server.diff_payload is api.diff_payload
    assert server.read_json is api.read_json
    assert server.collect_logs is api.collect_logs


def test_overview_rows_match_frozen_contract(monkeypatch):
    rdm = service._rdm()
    monkeypatch.setattr(rdm.ui.api, "build_status", lambda port: dict(_STATUS_STUB))

    rows = service.overview()

    assert [row["id"] for row in rows] == [
        "profile", "ingress", "bridge", "runner", "watchdog", "host_services",
    ]
    for row in rows:
        assert {"id", "title", "value", "tone", "updatedAt"} <= set(row)
        assert set(row) <= {"id", "title", "value", "tone", "hint", "updatedAt"}
        assert row["title"].startswith("stitch-devbox.card.")
        assert row["tone"] in ("ok", "warn", "down")
        datetime.fromisoformat(row["updatedAt"])
    assert len({row["updatedAt"] for row in rows}) == 1
    by_id = {row["id"]: row for row in rows}
    assert by_id["profile"]["value"] == "alpha"
    assert by_id["profile"]["tone"] == "ok"
    assert "hint" not in by_id["profile"]
    assert by_id["ingress"]["value"] == "up"
    assert by_id["ingress"]["tone"] == "ok"
    assert by_id["bridge"]["value"] == "down"
    assert by_id["bridge"]["tone"] == "down"
    assert by_id["bridge"]["hint"] == "stitch-devbox.hint.bridge"
    assert by_id["runner"]["tone"] == "down"
    assert by_id["runner"]["hint"] == "stitch-devbox.hint.runner"
    assert by_id["watchdog"]["value"] == "off"
    assert by_id["watchdog"]["tone"] == "warn"
    assert by_id["watchdog"]["hint"] == "stitch-devbox.hint.watchdog"
    assert by_id["host_services"]["value"] == "2/3"
    assert by_id["host_services"]["tone"] == "ok"
    assert "hint" not in by_id["host_services"]


def test_overview_tone_edges(monkeypatch):
    rdm = service._rdm()
    stub = {
        "env": {"ACTIVE_PROFILE": ""},
        "ports": {"ingress": False, "bridge": True, "runner": True},
        "watchdog": True,
        "host_services": {"alive": 0, "recorded": 3},
    }
    monkeypatch.setattr(rdm.ui.api, "build_status", lambda port: stub)

    by_id = {row["id"]: row for row in service.overview()}

    assert by_id["profile"]["value"] == "—"
    assert by_id["profile"]["tone"] == "warn"
    assert by_id["profile"]["hint"] == "stitch-devbox.hint.profile"
    assert by_id["ingress"]["tone"] == "down"
    assert by_id["ingress"]["hint"] == "stitch-devbox.hint.ingress"
    assert by_id["bridge"]["tone"] == "ok"
    assert by_id["runner"]["tone"] == "ok"
    assert by_id["watchdog"]["value"] == "on"
    assert by_id["watchdog"]["tone"] == "ok"
    assert "hint" not in by_id["watchdog"]
    assert by_id["host_services"]["value"] == "0/3"
    assert by_id["host_services"]["tone"] == "warn"
    assert by_id["host_services"]["hint"] == "stitch-devbox.hint.hostServices"


def test_overview_host_services_zero_recorded_is_ok(monkeypatch):
    rdm = service._rdm()
    stub = dict(_STATUS_STUB, host_services={"alive": 0, "recorded": 0})
    monkeypatch.setattr(rdm.ui.api, "build_status", lambda port: stub)

    by_id = {row["id"]: row for row in service.overview()}

    assert by_id["host_services"]["value"] == "0/0"
    assert by_id["host_services"]["tone"] == "ok"
    assert "hint" not in by_id["host_services"]


def test_profiles_list_rows_always_full_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    rdm = service._rdm()
    monkeypatch.setattr(rdm.cli, "ENV_FILE", tmp_path / "missing.env")
    (tmp_path / "good.json").write_text(json.dumps(_profile_data(tmp_path)), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")

    rows = service.profiles_list()

    assert {"good", "broken"} <= {row["name"] for row in rows}
    for row in rows:
        assert set(row) == {"name", "mode", "project_dir", "active"}
        assert isinstance(row["active"], bool)


def test_logs_payload_groups_lines_per_source(monkeypatch):
    rdm = service._rdm()
    entries = [
        {"source": "ingress", "file": "ingress.out", "lines": ["a", "b"]},
        {"source": "host", "file": "svc.out", "lines": ["c"]},
    ]
    monkeypatch.setattr(rdm.ui.api, "collect_logs", lambda source, needle: entries)

    payload = service.logs()

    assert set(payload) == {"sources"}
    for group in payload["sources"]:
        assert set(group) == {"name", "lines"}
        assert isinstance(group["name"], str)
        assert isinstance(group["lines"], list)
        assert all(isinstance(line, str) for line in group["lines"])


def test_collect_logs_entry_shape(monkeypatch, tmp_path):
    from rdm.ui import api

    monkeypatch.setattr(api.hostos, "tempdir", lambda: tmp_path)
    ingress = tmp_path / "rdm-ingress" / "ingress.out"
    ingress.parent.mkdir(parents=True, exist_ok=True)
    ingress.write_text("line one\nline two\n", encoding="utf-8")

    entries = api.collect_logs("ingress", "")

    assert entries == [{"source": "ingress", "file": "ingress.out", "lines": ["line one", "line two"]}]


def test_jobs_payload_carries_stalled(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    now = time.time()
    _write_events(
        tmp_path / "events.jsonl",
        [
            {"ts": now - 400, "kind": "mcp_request", "rpc_method": "tools/call", "job_id": "j1", "tool": "bash", "session": "s1"},
            {"ts": now - 399, "kind": "mcp_response", "job_id": "j1", "tool": "bash", "status": "running"},
        ],
    )

    payload = service.jobs()

    assert set(payload) == {"jobs", "stalled"}
    assert any(alarm.get("job_id") == "j1" for alarm in payload["stalled"])


def test_permissions_payload_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    now = time.time()
    _write_events(
        tmp_path / "events.jsonl",
        [
            {"ts": now, "kind": "mcp_request", "rpc_method": "tools/call", "job_id": "j1", "tool": "bash", "session": "s1"},
            {"ts": now, "kind": "mcp_response", "job_id": "j1", "tool": "bash", "status": "awaiting_permission", "permission": "bash:rm -rf"},
        ],
    )

    payload = service.permissions()

    assert set(payload) == {"permissions", "stalled"}
    assert [job["job_id"] for job in payload["permissions"]] == ["j1"]


def test_action_status_maps_recent_from_action_events(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    events_path = tmp_path / "events.jsonl"
    now = time.time()
    _write_events(
        events_path,
        [
            {"ts": now - 40, "kind": "action", "job_id": "a1", "tool": "start", "status": "started"},
            {"ts": now - 30, "kind": "action", "job_id": "a1", "tool": "start", "status": "finished", "exit": 0, "payload": {"chatBlock": "b"}},
            {"ts": now - 20, "kind": "action", "job_id": "a2", "tool": "down", "status": "started"},
            {"ts": now - 10, "kind": "action", "job_id": "a2", "tool": "down", "status": "interrupted"},
            {"ts": now, "kind": "mcp_response", "job_id": "j1", "tool": "bash", "status": "running"},
        ],
    )
    monkeypatch.setenv("RDM_EVENTS_PATH", str(events_path))

    payload = service.action_status()

    assert set(payload) == {"busy", "recent"}
    assert payload["busy"] is False
    assert [row["id"] for row in payload["recent"]] == ["a1", "a2"]
    finished = payload["recent"][0]
    assert set(finished) == {"id", "cmd", "status", "startedAt", "finishedAt", "exit", "payload"}
    assert finished["cmd"] == "start"
    assert finished["status"] == "finished"
    assert finished["exit"] == 0
    assert finished["payload"] == {"chatBlock": "b"}
    datetime.fromisoformat(finished["startedAt"])
    datetime.fromisoformat(finished["finishedAt"])
    interrupted = payload["recent"][1]
    assert set(interrupted) == {"id", "cmd", "status", "startedAt", "finishedAt"}
    assert interrupted["status"] == "interrupted"


def _wait_recent_finished() -> dict[str, Any]:
    deadline = time.monotonic() + 5
    payload = service.action_status()
    while payload["busy"] or not payload["recent"]:
        assert time.monotonic() < deadline, "action did not finish"
        time.sleep(0.01)
        payload = service.action_status()
    return payload["recent"][-1]


def test_action_status_current_while_action_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    rdm = service._rdm()
    release = threading.Event()

    def slow_ingress(action: str) -> int:
        release.wait(5)
        return 0

    monkeypatch.setattr(rdm.cli, "_ingress", slow_ingress)

    result = service.ingress_control("up")
    assert result["accepted"] is True

    deadline = time.monotonic() + 5
    payload = service.action_status()
    while "current" not in payload:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        payload = service.action_status()

    assert payload["busy"] is True
    assert set(payload["current"]) == {"id", "cmd", "startedAt"}
    assert payload["current"]["id"] == result["actionId"]
    datetime.fromisoformat(payload["current"]["startedAt"])

    release.set()
    deadline = time.monotonic() + 5
    while service.action_status()["busy"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_events_tail_returns_event_dicts(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    now = time.time()
    _write_events(
        tmp_path / "events.jsonl",
        [{"ts": now, "kind": "mcp_response", "job_id": "j1", "tool": "bash", "status": "running"}],
    )

    events = service.events_tail(10)

    assert len(events) == 1
    assert events[0]["job_id"] == "j1"


def test_read_json_missing_file_returns_none(tmp_path):
    from rdm.ui import api

    assert api.read_json(tmp_path / "missing.json") is None
    (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")
    assert api.read_json(tmp_path / "broken.json") is None
    (tmp_path / "good.json").write_text('{"a": 1}', encoding="utf-8")
    assert api.read_json(tmp_path / "good.json") == {"a": 1}


def test_rw_commands_return_accepted_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    rdm = service._rdm()
    monkeypatch.setattr(rdm.cli, "_ingress", lambda action: 0)

    result = service.ingress_control("up")

    assert set(result) == {"accepted", "actionId"}
    assert result["accepted"] is True
    assert isinstance(result["actionId"], str)

    deadline = time.monotonic() + 5
    while service.action_status()["busy"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_rw_command_rejected_while_busy(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    rdm = service._rdm()
    release = threading.Event()

    def slow_ingress(action: str) -> int:
        release.wait(5)
        return 0

    monkeypatch.setattr(rdm.cli, "_ingress", slow_ingress)

    assert service.ingress_control("up")["accepted"] is True
    rejected = service.ingress_control("down")

    assert set(rejected) == {"accepted", "reason"}
    assert rejected["accepted"] is False
    assert rejected["reason"]

    release.set()
    deadline = time.monotonic() + 5
    while service.action_status()["busy"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_profile_get_returns_frozen_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "alpha.json").write_text(json.dumps(_profile_data(tmp_path)), encoding="utf-8")

    result = service.profile_get("alpha")

    assert set(result) == {"json", "valid", "errors"}
    assert json.loads(result["json"])["git_name"] == "agent"
    assert result["valid"] is True
    assert result["errors"] == []


def test_profile_get_broken_json_is_invalid(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    (tmp_path / "broken.json").write_text("{oops", encoding="utf-8")

    result = service.profile_get("broken")

    assert set(result) == {"json", "valid", "errors"}
    assert result["json"] == "{oops"
    assert result["valid"] is False
    assert len(result["errors"]) == 1
    assert result["errors"][0].startswith("json:")


def test_profile_get_surfaces_validation_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    broken = dict(_profile_data(tmp_path), project_dir="")
    (tmp_path / "alpha.json").write_text(json.dumps(broken), encoding="utf-8")

    result = service.profile_get("alpha")

    assert result["valid"] is False
    assert any(problem.startswith("R1") for problem in result["errors"])


def test_profile_get_unknown_profile_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))

    with pytest.raises(ValueError):
        service.profile_get("ghost")


def test_issue_tokens_surfaces_chat_block_in_recent_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    rdm = service._rdm()

    def fake_issue_tokens() -> int:
        print("chat block body")
        return 0

    monkeypatch.setattr(rdm.cli, "_issue_tokens", fake_issue_tokens)

    result = service.issue_tokens()

    assert set(result) == {"accepted", "actionId"}
    finished = _wait_recent_finished()
    assert finished["status"] == "finished"
    assert finished["exit"] == 0
    assert finished["payload"] == {"chatBlock": "chat block body\n"}


def test_action_payload_is_redacted(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    rdm = service._rdm()

    def fake_issue_tokens() -> int:
        print("BRIDGE_TOKEN=abcdef1234567890abcdef1234567890")
        return 0

    monkeypatch.setattr(rdm.cli, "_issue_tokens", fake_issue_tokens)

    service.issue_tokens()
    finished = _wait_recent_finished()

    chat_block = finished["payload"]["chatBlock"]
    assert "abcdef1234567890" not in chat_block
    assert "[REDACTED]" in chat_block


def test_action_payload_over_cap_is_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_queue", None)
    monkeypatch.setenv("RDM_EVENTS_PATH", str(tmp_path / "events.jsonl"))
    rdm = service._rdm()

    def fake_issue_tokens() -> int:
        print("x" * (64 * 1024 + 1))
        return 0

    monkeypatch.setattr(rdm.cli, "_issue_tokens", fake_issue_tokens)

    service.issue_tokens()
    finished = _wait_recent_finished()

    assert finished["status"] == "finished"
    assert "payload" not in finished
