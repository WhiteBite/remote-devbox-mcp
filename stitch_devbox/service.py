"""Command layer for the stitch-devbox service plugin.

Thin adapter over the remote-devbox-mcp ``rdm`` core: the plugin imports
``rdm`` in-process (``<repo>/home`` on ``sys.path``) and reuses the cockpit
payload builders and single-flight action queue. Read commands return what the
cockpit UI shows; write commands enqueue the same CLI entry points the tray
and cockpit use, with stdout redirected to stderr so the JSON-RPC channel
stays clean.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOME_DIR = _REPO_ROOT / "home"
if _HOME_DIR.is_dir() and str(_HOME_DIR) not in sys.path:
    sys.path.insert(0, str(_HOME_DIR))

_rd: Any = None
_queue: Any = None


def _rdm() -> Any:
    """Lazily import the rdm package; raise a clear error when unavailable."""
    global _rd
    if _rd is None:
        try:
            import rdm  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "rdm core not importable from the devbox repo checkout"
            ) from exc
        _rd = True
    import rdm.cli  # noqa: PLC0415
    import rdm.ui.server  # noqa: PLC0415

    return rdm


def health_check() -> dict[str, Any]:
    """Cheap liveness probe: rdm importable, active profile, stack hint."""
    try:
        rdm = _rdm()
        env_map = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map()
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "active_profile": env_map.get("ACTIVE_PROFILE", ""),
        "repo": str(_REPO_ROOT),
    }


def status() -> dict[str, Any]:
    rdm = _rdm()
    payload = rdm.ui.server._build_status(0)
    payload.pop("env", None)
    return payload


def overview() -> list[dict[str, Any]]:
    """Row list for the status card grid (key/value cards)."""
    rdm = _rdm()
    server = rdm.ui.server
    payload = server._build_status(0)
    env_map = payload.get("env", {}) or {}
    ports = payload.get("ports", {}) or {}
    manifest = payload.get("manifest", {}) or {}
    compose = payload.get("compose_ps")
    rows = [
        {"title": "Профиль", "value": env_map.get("ACTIVE_PROFILE", "—")},
        {"title": "Ingress", "value": "up" if ports.get("ingress") else "down"},
        {"title": "Bridge", "value": "up" if ports.get("bridge") else "down"},
        {"title": "Runner", "value": "up" if ports.get("runner") else "down"},
        {"title": "Watchdog", "value": "on" if payload.get("watchdog") else "off"},
        {
            "title": "Host-сервисы",
            "value": f"{(payload.get('host_services') or {}).get('alive', 0)}/"
            f"{(payload.get('host_services') or {}).get('recorded', 0)}",
        },
    ]
    if manifest:
        rows.append({"title": "Проект", "value": manifest.get("project_dir", "")})
    if compose:
        rows.append({"title": "Compose", "value": str(compose)})
    return rows


def jobs() -> dict[str, Any]:
    return _rdm().ui.server._jobs_payload()


def permissions() -> dict[str, Any]:
    return _rdm().ui.server._permissions_payload()


def exposure() -> dict[str, Any]:
    payload = _rdm().ui.server._exposure_payload()
    return payload if payload is not None else {"profile": None}


def diff() -> dict[str, Any]:
    return _rdm().ui.server._diff_payload()


def manifest() -> dict[str, Any]:
    payload = _rdm().ui.server._read_json(_rdm().cli.MANIFEST_PATH)
    return payload if isinstance(payload, dict) else {}


def events_tail(limit: int = 50) -> list[dict[str, Any]]:
    events = _rdm().ui.server._read_events()
    return events[-max(1, min(int(limit), 500)):]


def logs(source: str = "", filter: str = "") -> dict[str, Any]:  # noqa: A002
    entries = _rdm().ui.server._collect_logs(source, filter.lower())
    return {"logs": entries}


def logs_text(limit: int = 80) -> dict[str, Any]:
    """Markdown block: tail of every log source, for the page."""
    entries = _rdm().ui.server._collect_logs("", "")
    lines: list[str] = []
    for entry in entries[-max(1, min(int(limit), 300)):]:
        lines.append(f"**{entry['source']} — {entry['file']}**")
        lines.append("```")
        lines.extend(str(line) for line in entry["lines"][-40:])
        lines.append("```")
    return {"text": "\n".join(lines) if lines else "Логов пока нет."}


def profiles_list() -> list[dict[str, Any]]:
    rdm = _rdm()
    active = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map().get("ACTIVE_PROFILE", "")
    rows = []
    for name in rdm.profiles.available():
        path = rdm.profiles.find(name)
        row: dict[str, Any] = {"name": name, "active": name == active}
        if path is not None:
            try:
                profile = rdm.profiles.load(path)
            except (ValueError, OSError):
                row["mode"] = "?"
                row["project_dir"] = ""
            else:
                row["mode"] = profile.mode
                row["project_dir"] = profile.project_dir
        rows.append(row)
    return rows


def profile_get(name: str) -> dict[str, Any]:
    rdm = _rdm()
    path = rdm.profiles.find(name)
    if path is None:
        raise ValueError(f"profile not found: {name}")
    raw = rdm.ui.server._read_json(path)
    if not isinstance(raw, dict):
        raise ValueError(f"profile unreadable: {name}")
    return {"name": name, "path": str(path), "raw": raw}


def _queue_submit(fn_name: str, *args: Any) -> dict[str, Any]:
    """Enqueue a cockpit CLI callable on the single-flight action queue."""
    import contextlib
    import io

    global _queue
    rdm = _rdm()
    if _queue is None:
        from rdm.ui import control  # noqa: PLC0415

        _queue = control.ActionQueue()

    fn = getattr(rdm.cli, fn_name)

    def _runner() -> int:
        # cli entry points print human output; keep the RPC stdout clean.
        with contextlib.redirect_stdout(io.StringIO()):
            return int(fn(*args))

    action_id = _queue.submit(_runner)
    if action_id is None:
        return {"accepted": False, "reason": "another action is already running"}
    return {"accepted": True, "action_id": action_id}


def action_status() -> dict[str, Any]:
    busy = bool(_queue is not None and _queue.busy())
    actions = [
        event
        for event in events_tail(30)
        if event.get("kind") == "action"
    ]
    return {"busy": busy, "recent": actions[-10:]}


def stack_start(name: str = "", preview: bool = False) -> dict[str, Any]:
    return _queue_submit("_start", name or None, bool(preview))


def stack_down() -> dict[str, Any]:
    return _queue_submit("_down")


def profile_use(name: str) -> dict[str, Any]:
    if not name:
        raise ValueError("name is required")
    return _queue_submit("apply_use", name)


def stop_host() -> dict[str, Any]:
    return _queue_submit("_stop_host")


def ingress_control(action: str = "up") -> dict[str, Any]:
    return _queue_submit("_ingress", str(action))


def issue_tokens() -> dict[str, Any]:
    return _queue_submit("_issue_tokens")


def run_doctor() -> dict[str, Any]:
    return _queue_submit("_doctor")
