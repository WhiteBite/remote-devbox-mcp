"""Pure job/permission correlation and stall detection over the event stream.

Consumes the metadata-only events persisted by rdm.events.sink (list of dicts)
and rebuilds the job timeline. Each ``tools/call`` request is paired with the
response that answered it: by shared ``job_id`` when the request carries one,
else by ``session`` + ``tool`` + time window when the response carries any of
those identity fields, else positionally (the wire is sequential per
connection). Jobs are keyed by ``job_id`` from responses; the first matched
request is the job's originator, later ones are follow-ups (permission
replies, job-result polls). ``ts`` is compared numerically (epoch seconds);
events without a numeric ``ts`` never age out.
"""

from __future__ import annotations

from typing import Any

MUTATING_TOOLS = frozenset(("write", "edit", "apply_patch", "bash"))
TERMINAL_STATUSES = frozenset(("completed", "failed", "cancelled"))
MATCH_WINDOW_SECONDS = 120.0
DEFAULT_STALL_SECONDS = 300.0
_IDENTITY_KEYS = ("session", "tool", "ts")


def correlate(events: list[dict[str, Any]]) -> dict[str, Any]:
    jobs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    pending: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("kind")
        if kind == "mcp_request" and event.get("rpc_method") == "tools/call":
            job_id = event.get("job_id")
            if job_id is not None and job_id in jobs:
                _count_request(jobs[job_id], event)
            else:
                pending.append(event)
        elif kind == "mcp_response":
            request = _match_pending(pending, event)
            job_id = event.get("job_id")
            if job_id is None:
                continue
            job = jobs.get(job_id)
            if job is None:
                job = {
                    "job_id": job_id,
                    "tool": None,
                    "session": None,
                    "request_ts": None,
                    "last_ts": None,
                    "status": None,
                    "permission": None,
                    "exit": None,
                    "requests": 0,
                    "responses": 0,
                    "terminal": False,
                }
                jobs[job_id] = job
                order.append(job_id)
            _apply_response(job, event, request)
    return {"jobs": [jobs[job_id] for job_id in order], "open_calls": pending}


def stall_alarm(
    events: list[dict[str, Any]], now: float, threshold_seconds: float = DEFAULT_STALL_SECONDS
) -> list[dict[str, Any]]:
    correlated = correlate(events)
    alarms: list[dict[str, Any]] = []
    for job in correlated["jobs"]:
        if job["tool"] not in MUTATING_TOOLS or job["terminal"]:
            continue
        ts = _numeric(job["request_ts"])
        if ts is None or now - ts <= threshold_seconds:
            continue
        alarms.append(
            {
                "job_id": job["job_id"],
                "tool": job["tool"],
                "session": job["session"],
                "ts": ts,
                "age_seconds": now - ts,
                "status": job["status"],
            }
        )
    for call in correlated["open_calls"]:
        if call.get("tool") not in MUTATING_TOOLS:
            continue
        ts = _numeric(call.get("ts"))
        if ts is None or now - ts <= threshold_seconds:
            continue
        alarms.append(
            {
                "job_id": None,
                "tool": call.get("tool"),
                "session": call.get("session"),
                "ts": ts,
                "age_seconds": now - ts,
                "status": None,
            }
        )
    return alarms


def _match_pending(pending: list[dict[str, Any]], response: dict[str, Any]) -> dict[str, Any] | None:
    job_id = response.get("job_id")
    if job_id is not None:
        for index, request in enumerate(pending):
            if request.get("job_id") == job_id:
                return pending.pop(index)
    if any(key in response for key in _IDENTITY_KEYS):
        for index in range(len(pending) - 1, -1, -1):
            request = pending[index]
            if "session" in response and request.get("session") != response["session"]:
                continue
            if "tool" in response and request.get("tool") != response["tool"]:
                continue
            response_ts = _numeric(response.get("ts"))
            request_ts = _numeric(request.get("ts"))
            if response_ts is not None and request_ts is not None:
                if response_ts - request_ts > MATCH_WINDOW_SECONDS:
                    continue
            return pending.pop(index)
        return None
    return pending.pop(0) if pending else None


def _apply_response(job: dict[str, Any], event: dict[str, Any], request: dict[str, Any] | None) -> None:
    job["responses"] += 1
    if request is not None:
        _count_request(job, request)
    for key in ("status", "permission", "exit"):
        if event.get(key) is not None:
            job[key] = event[key]
    if event.get("ts") is not None:
        job["last_ts"] = event["ts"]
    job["terminal"] = job["status"] in TERMINAL_STATUSES


def _count_request(job: dict[str, Any], request: dict[str, Any]) -> None:
    job["requests"] += 1
    if job["tool"] is None:
        job["tool"] = request.get("tool")
        job["session"] = request.get("session")
        job["request_ts"] = request.get("ts")


def _numeric(value: Any) -> float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None
