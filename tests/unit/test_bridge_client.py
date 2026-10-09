from __future__ import annotations

import pytest
from rdm.bridge_client import BridgeClient, BridgeDenied, BridgeError, BridgeUnreachable

from tests.bridge_stub import TOKEN, BridgeStub, dead_bridge_url


@pytest.fixture()
def bridge():
    stub = BridgeStub()
    yield stub
    stub.close()


def test_job_list_returns_bridge_jobs(bridge):
    bridge.jobs = [
        {"job_id": "job-1", "tool": "bash", "status": "running", "created_at": "2026-10-09T10:00:00Z"},
        {"job_id": "job-2", "tool": "edit", "status": "completed", "created_at": "2026-10-09T09:00:00Z"},
    ]
    client = BridgeClient(bridge.url, TOKEN)

    jobs = client.job_list()

    assert [job["job_id"] for job in jobs] == ["job-1", "job-2"]


def test_permissions_pending_returns_requests(bridge):
    bridge.requests = [{"job_id": "job-1", "id": "perm-2", "permission": "edit", "patterns": ["src/**"]}]

    client = BridgeClient(bridge.url, TOKEN)

    assert client.permissions_pending() == bridge.requests


def test_wrong_token_is_denied(bridge):
    client = BridgeClient(bridge.url, "wrong-token")

    with pytest.raises(BridgeDenied):
        client.job_list()


def test_unreachable_bridge_raises(bridge):
    client = BridgeClient(dead_bridge_url(), TOKEN)

    with pytest.raises(BridgeUnreachable):
        client.job_list()


def test_session_expiry_reinitializes_and_retries(bridge):
    client = BridgeClient(bridge.url, TOKEN)
    assert client.job_list() == []

    bridge.forget_sessions()

    assert client.job_list() == []


def test_tool_level_error_raises_bridge_error(bridge):
    client = BridgeClient(bridge.url, TOKEN)

    with pytest.raises(BridgeError, match="Unknown or expired job_id"):
        client.job_cancel("unknown")


def test_job_cancel_returns_job_view(bridge):
    client = BridgeClient(bridge.url, TOKEN)

    view = client.job_cancel("job-1")

    assert view == {"job_id": "job-1", "status": "cancelling"}
    assert bridge.calls[-1] == {"name": "opencode_job_cancel", "arguments": {"job_id": "job-1"}}


def test_permission_reply_resolves_job_id_from_pending(bridge):
    bridge.requests = [{"job_id": "job-2", "id": "perm-2", "permission": "edit", "patterns": ["src/**"]}]
    client = BridgeClient(bridge.url, TOKEN)

    view = client.permission_reply("perm-2", "once")

    assert view == {"job_id": "job-2", "status": "running"}
    assert bridge.calls[-1] == {
        "name": "opencode_permission_reply",
        "arguments": {"job_id": "job-2", "permission_id": "perm-2", "reply": "once"},
    }


def test_permission_reply_unknown_permission_raises(bridge):
    client = BridgeClient(bridge.url, TOKEN)

    with pytest.raises(BridgeError, match="perm-404"):
        client.permission_reply("perm-404", "once")


def test_budget_expires_instead_of_blocking(bridge):
    client = BridgeClient(bridge.url, TOKEN, deadline=0.0)

    with pytest.raises(BridgeUnreachable, match="deadline"):
        with client.budget():
            client.job_list()


def test_budget_spans_multiple_calls(bridge, monkeypatch):
    import rdm.bridge_client as bridge_client

    clock = {"now": 1000.0}
    monkeypatch.setattr(bridge_client.time, "monotonic", lambda: clock["now"])
    client = BridgeClient(bridge.url, TOKEN, deadline=5.0)

    with client.budget():
        assert client.job_list() == []
        clock["now"] += 6.0
        with pytest.raises(BridgeUnreachable):
            client.permissions_pending()
