from __future__ import annotations

import pytest
from rdm.events import jobs


def _request(ts, tool, session="s1", job_id=None, rpc_id=1):
    event = {"kind": "mcp_request", "ts": ts, "session": session, "rpc_method": "tools/call", "tool": tool, "rpc_id": rpc_id}
    if job_id is not None:
        event["job_id"] = job_id
    return event


def _response(ts, job_id, status, session="s1", permission=None, exit=None):
    event = {"kind": "mcp_response", "ts": ts, "session": session, "job_id": job_id, "status": status}
    if permission is not None:
        event["permission"] = permission
    if exit is not None:
        event["exit"] = exit
    return event


def test_correlate_request_job_response_permission():
    events = [
        _request(100.0, "edit", rpc_id=1),
        _response(101.0, "job-1", "awaiting_permission", permission="edit"),
        _request(105.0, "opencode_permission_reply", rpc_id=2),
        _response(106.0, "job-1", "completed", exit=0),
    ]
    result = jobs.correlate(events)
    assert result["open_calls"] == []
    assert len(result["jobs"]) == 1
    job = result["jobs"][0]
    assert job["job_id"] == "job-1"
    assert job["tool"] == "edit"
    assert job["session"] == "s1"
    assert job["request_ts"] == 100.0
    assert job["last_ts"] == 106.0
    assert job["status"] == "completed"
    assert job["permission"] == "edit"
    assert job["exit"] == 0
    assert job["requests"] == 2
    assert job["responses"] == 2
    assert job["terminal"] is True


def test_correlate_matches_request_by_job_id():
    events = [
        _request(200.0, "opencode_job_result", job_id="job-7", rpc_id=3),
        _response(201.0, "job-7", "running"),
    ]
    result = jobs.correlate(events)
    assert result["open_calls"] == []
    job = result["jobs"][0]
    assert job["job_id"] == "job-7"
    assert job["tool"] == "opencode_job_result"
    assert job["status"] == "running"
    assert job["terminal"] is False


def test_correlate_positional_fallback_without_identity_fields():
    events = [
        {"kind": "mcp_request", "rpc_method": "tools/call", "tool": "bash", "rpc_id": 1},
        {"kind": "mcp_response", "job_id": "job-9", "status": "completed", "exit": 0},
    ]
    result = jobs.correlate(events)
    assert result["open_calls"] == []
    assert result["jobs"][0]["tool"] == "bash"
    assert result["jobs"][0]["terminal"] is True


def test_correlate_session_mismatch_leaves_call_open():
    events = [
        _request(10.0, "edit", session="s1"),
        _response(11.0, "job-2", "completed", session="s2"),
    ]
    result = jobs.correlate(events)
    assert result["jobs"][0]["tool"] is None
    assert result["jobs"][0]["requests"] == 0
    assert result["open_calls"][0]["tool"] == "edit"


def test_correlate_time_window_rejects_stale_match():
    events = [
        _request(10.0, "edit", session="s1"),
        _response(500.0, "job-3", "completed", session="s1"),
    ]
    result = jobs.correlate(events)
    assert result["jobs"][0]["tool"] is None
    assert result["open_calls"][0]["tool"] == "edit"


def test_correlate_ignores_non_tools_call_requests():
    events = [
        {"kind": "mcp_request", "rpc_method": "initialize", "rpc_id": 1},
        _response(11.0, "job-1", "completed"),
    ]
    result = jobs.correlate(events)
    assert result["open_calls"] == []
    assert result["jobs"][0]["requests"] == 0


def test_correlate_unparsed_response_closes_call_without_job():
    events = [
        {"kind": "mcp_request", "rpc_method": "tools/call", "tool": "bash", "rpc_id": 1},
        {"kind": "mcp_response", "truncated": True, "bytes": 999},
    ]
    result = jobs.correlate(events)
    assert result["jobs"] == []
    assert result["open_calls"] == []


def test_stall_alarm_empty_for_completed_calls():
    events = [
        _request(100.0, "bash"),
        _response(101.0, "job-1", "completed", exit=0),
    ]
    assert jobs.stall_alarm(events, now=1000.0, threshold_seconds=60.0) == []


def test_stall_alarm_flags_open_mutating_call_past_threshold():
    events = [_request(100.0, "edit")]
    alarms = jobs.stall_alarm(events, now=400.0, threshold_seconds=60.0)
    assert len(alarms) == 1
    assert alarms[0]["tool"] == "edit"
    assert alarms[0]["job_id"] is None
    assert alarms[0]["ts"] == 100.0
    assert alarms[0]["age_seconds"] == 300.0


@pytest.mark.parametrize("tool", ["write", "edit", "apply_patch", "bash"])
def test_stall_alarm_flags_each_mutating_tool(tool):
    events = [_request(100.0, tool)]
    assert len(jobs.stall_alarm(events, now=400.0, threshold_seconds=60.0)) == 1


def test_stall_alarm_quiet_below_threshold():
    events = [_request(100.0, "bash")]
    assert jobs.stall_alarm(events, now=120.0, threshold_seconds=60.0) == []


def test_stall_alarm_ignores_non_mutating_tools():
    events = [_request(100.0, "read")]
    assert jobs.stall_alarm(events, now=400.0, threshold_seconds=60.0) == []


def test_stall_alarm_flags_permission_wait_past_threshold():
    events = [
        _request(100.0, "apply_patch"),
        _response(101.0, "job-5", "awaiting_permission", permission="apply_patch"),
    ]
    alarms = jobs.stall_alarm(events, now=500.0, threshold_seconds=60.0)
    assert len(alarms) == 1
    assert alarms[0]["job_id"] == "job-5"
    assert alarms[0]["status"] == "awaiting_permission"
    assert alarms[0]["age_seconds"] == 400.0


def test_stall_alarm_skips_calls_without_timestamp():
    events = [{"kind": "mcp_request", "rpc_method": "tools/call", "tool": "bash"}]
    assert jobs.stall_alarm(events, now=400.0, threshold_seconds=60.0) == []
