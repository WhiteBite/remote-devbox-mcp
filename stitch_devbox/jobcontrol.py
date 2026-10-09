"""Bridge control-plane commands: jobs_live, job_cancel, permissions_pending, permission_reply.

Contract deviation: job_cancel and permission_reply bypass the ActionQueue
single-flight — bridge control calls must stay answerable while a host action
runs; the queue rule remains for host-mutating cli actions only.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from . import service

TERMINAL_STATUSES = frozenset(("completed", "failed", "cancelled"))
DECISIONS = frozenset(("once", "reject"))
BRIDGE_DOWN_TTL_SECONDS = 15.0

_client: Any = None
_bridge_down_until = 0.0


def _bridge_known_down() -> bool:
    return time.monotonic() < _bridge_down_until


def _remember_bridge_down() -> None:
    global _bridge_down_until
    _bridge_down_until = time.monotonic() + BRIDGE_DOWN_TTL_SECONDS


def _remember_bridge_up() -> None:
    global _bridge_down_until
    _bridge_down_until = 0.0


def jobs_live() -> dict[str, Any]:
    from rdm.bridge_client import BridgeDenied, BridgeError, BridgeUnreachable

    if _bridge_known_down():
        return {"rows": _events_job_rows(), "source": "events", "bridge": "down"}
    client = _bridge()
    try:
        with client.budget():
            views = client.job_list()
    except BridgeDenied:
        raise
    except BridgeUnreachable:
        _remember_bridge_down()
        return {"rows": _events_job_rows(), "source": "events", "bridge": "down"}
    except BridgeError:
        return {"rows": _events_job_rows(), "source": "events", "bridge": "down"}
    _remember_bridge_up()
    rows = [_job_row(view) for view in views if view.get("status") not in TERMINAL_STATUSES]
    return {"rows": rows, "source": "bridge", "bridge": "up"}


def job_cancel(job_id: str) -> dict[str, Any]:
    if not job_id:
        raise ValueError("jobId is required")
    from rdm.bridge_client import BridgeError

    client = _bridge()
    try:
        with client.budget():
            view = client.job_cancel(job_id)
    except BridgeError as exc:
        return {"accepted": False, "reason": str(exc)}
    return {"accepted": True, "status": view.get("status", "")}


def permissions_pending() -> dict[str, Any]:
    from rdm.bridge_client import BridgeDenied, BridgeError, BridgeUnreachable

    rows: list[dict[str, Any]] = []
    bridge = "up"
    if _bridge_known_down():
        bridge = "down"
    else:
        try:
            rows = _bridge_permission_rows()
            _remember_bridge_up()
        except BridgeDenied:
            raise
        except BridgeUnreachable:
            _remember_bridge_down()
            bridge = "down"
        except BridgeError:
            bridge = "down"
    known = {row["id"] for row in rows}
    for row in _tap_permission_rows():
        if row["id"] not in known:
            rows.append(row)
    return {"rows": rows, "bridge": bridge}


def permission_reply(permission_id: str, decision: str) -> dict[str, Any]:
    if not permission_id:
        raise ValueError("permissionId is required")
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    from rdm.bridge_client import BridgeError

    client = _bridge()
    try:
        with client.budget():
            view = client.permission_reply(permission_id, decision)
    except BridgeError as exc:
        return {"accepted": False, "reason": str(exc)}
    return {"accepted": True, "status": view.get("status", "")}


def _bridge() -> Any:
    global _client
    if _client is None:
        from rdm.bridge_client import BridgeClient

        rdm = service._rdm()
        token = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).get("MCP_BEARER_TOKEN") or ""
        _client = BridgeClient(f"http://127.0.0.1:{rdm.profiles.BRIDGE_PORT}/mcp", token)
    return _client


def _job_row(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(view.get("job_id") or ""),
        "tool": str(view.get("tool") or ""),
        "status": str(view.get("status") or ""),
        "startedAt": str(view.get("created_at") or ""),
    }


def _events_job_rows() -> list[dict[str, Any]]:
    rdm = service._rdm()
    from rdm.events import jobs as job_events

    events = rdm.ui.api.read_events()
    rows = []
    for job in job_events.correlate(events)["jobs"]:
        if job.get("terminal"):
            continue
        rows.append(
            {
                "id": str(job.get("job_id") or ""),
                "tool": str(job.get("tool") or ""),
                "status": str(job.get("status") or ""),
                "startedAt": _iso(job.get("request_ts")),
            }
        )
    return rows


def _bridge_permission_rows() -> list[dict[str, Any]]:
    client = _bridge()
    with client.budget():
        views = client.job_list()
        requests = client.permissions_pending()
    by_job = {view.get("job_id"): view for view in views}
    rows = []
    for request in requests:
        view = by_job.get(request.get("job_id")) or {}
        rows.append(
            {
                "id": str(request.get("id") or ""),
                "tool": str(view.get("tool") or ""),
                "summary": _summary(request),
                "startedAt": str(view.get("created_at") or ""),
            }
        )
    return rows


def _tap_permission_rows() -> list[dict[str, Any]]:
    rdm = service._rdm()
    from rdm.events import jobs as job_events

    events = rdm.ui.api.read_events()
    tools = {job.get("job_id"): job.get("tool") for job in job_events.correlate(events)["jobs"]}
    last: dict[Any, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") == "mcp_response" and event.get("job_id") is not None:
            last[event["job_id"]] = event
    rows = []
    for job_id, event in last.items():
        if event.get("status") != "awaiting_permission":
            continue
        permission_id = event.get("permission_id")
        if not permission_id:
            continue
        rows.append(
            {
                "id": str(permission_id),
                "tool": str(tools.get(job_id) or ""),
                "summary": str(event.get("permission") or ""),
                "startedAt": _iso(event.get("ts")),
            }
        )
    return rows


def _summary(request: dict[str, Any]) -> str:
    permission = str(request.get("permission") or "")
    patterns = ", ".join(str(pattern) for pattern in request.get("patterns") or [])
    return f"{permission}: {patterns}" if patterns else permission


def _iso(value: Any) -> str:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value) if value else ""
