"""Public payload builders shared by the cockpit server and the stitch-devbox plugin.

Both surfaces (cockpit HTTP server, plugin RPC commands) are thin clients
over these builders; payload shapes here are the frozen RPC contract.
"""

from __future__ import annotations

import concurrent.futures
import json
import pathlib
import subprocess
import threading
import time

from rdm import cli, docker, envfile, hostos, netprobe, procman, profiles, redact, tokens
from rdm.events import jobs, sink

COMPOSE_PS_TTL_SECONDS = 10.0
LOG_SOURCES: dict[str, tuple[str, ...]] = {
    "host": ("rdm-host/*.out", "rdm-host/*.err"),
    "ingress": ("rdm-ingress/ingress.out", "rdm-ingress/ingress.err"),
    "watchdog": ("rdm-watchdog/watchdog.log",),
    "runner": ("rdm-runner/audit.log",),
}
_TAIL_LINES = 200
_EVENTS_TAIL_LINES = 2000
_DIFF_TIMEOUT_SECONDS = 5.0
_SENSITIVE_MARKERS = ("TOKEN", "SECRET", "PASSWORD")
_SENSITIVE_KEYS = ("VLESS_SUB_URL", "TUNNEL_TAIL")


def _load_profile(active: str) -> profiles.Profile | None:
    path = profiles.find(active) if active else None
    if path is None:
        return None
    try:
        return profiles.load(path)
    except (ValueError, OSError):
        return None


def read_json(path: pathlib.Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_sensitive(key: str) -> bool:
    return key in _SENSITIVE_KEYS or any(marker in key for marker in _SENSITIVE_MARKERS)


def _masked_env(env_map: dict[str, str]) -> dict[str, str]:
    return {key: tokens.mask(value) if _is_sensitive(key) else value for key, value in sorted(env_map.items())}


def _pidfile_alive(path: pathlib.Path) -> dict[str, int]:
    entries = procman._read_entries(path)
    return {"recorded": len(entries), "alive": sum(1 for entry in entries if hostos.owned(*entry))}


def _watchdog_alive() -> bool:
    return any(hostos.owned(*entry) for entry in procman._read_entries(procman.watchdog_pids_path()))


_compose_ps_lock = threading.Lock()
_compose_ps_cache: tuple[str, str, float] | None = None
_clock = time.monotonic


def invalidate_status_cache() -> None:
    global _compose_ps_cache
    with _compose_ps_lock:
        _compose_ps_cache = None


def _compose_ps(compose_file: str) -> str:
    global _compose_ps_cache
    with _compose_ps_lock:
        now = _clock()
        if _compose_ps_cache is not None:
            cached_file, value, at = _compose_ps_cache
            if cached_file == compose_file and now - at < COMPOSE_PS_TTL_SECONDS:
                return value
        value = docker.compose_ps(compose_file)
        _compose_ps_cache = (compose_file, value, now)
        return value


def _probe_ports(port: int, runner_port: int | None) -> dict[str, bool | None]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        pending: dict[str, concurrent.futures.Future[bool]] = {
            "cockpit": pool.submit(netprobe.can_connect, port),
            "ingress": pool.submit(netprobe.can_connect, procman.INGRESS_PORT),
            "bridge": pool.submit(netprobe.can_connect, profiles.BRIDGE_PORT),
        }
        if runner_port:
            pending["runner"] = pool.submit(netprobe.can_connect, runner_port)
        return {name: future.result() for name, future in pending.items()}


def build_status(port: int) -> dict[str, object]:
    env_map = envfile.EnvFile.load(cli.ENV_FILE).as_map()
    active = env_map.get("ACTIVE_PROFILE", "")
    profile_path = profiles.find(active) if active else None
    runner_port = cli._runner_port(env_map)
    return {
        "compose_ps": _compose_ps(cli.COMPOSE_FILE),
        "ports": _probe_ports(port, runner_port),
        "host_services": _pidfile_alive(hostos.tempdir() / "rdm-host" / f"{active}-pids.txt"),
        "ingress_pids": _pidfile_alive(hostos.tempdir() / "rdm-ingress" / "pids.txt"),
        "watchdog": _watchdog_alive(),
        "env": _masked_env(env_map),
        "manifest": read_json(cli.MANIFEST_PATH),
        "profile": {"active": active, "source_dir": str(profile_path.parent) if profile_path else None},
    }


def _tail(path: pathlib.Path, needle: str) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()[-_TAIL_LINES:]
    if needle:
        lines = [line for line in lines if needle in line.lower()]
    return [redact.redact_text(line) for line in lines]


def collect_logs(source: str, needle: str) -> list[dict[str, object]]:
    temp = hostos.tempdir()
    logs: list[dict[str, object]] = []
    for name in (source,) if source else tuple(LOG_SOURCES):
        files = sorted(path for pattern in LOG_SOURCES[name] for path in temp.glob(pattern))
        for path in files:
            lines = _tail(path, needle)
            if lines:
                logs.append({"source": name, "file": path.name, "lines": lines})
    return logs


def exposure_payload() -> dict[str, object] | None:
    env_map = envfile.EnvFile.load(cli.ENV_FILE).as_map()
    active = env_map.get("ACTIVE_PROFILE", "")
    profile = _load_profile(active)
    if profile is None:
        return None
    manifest = read_json(cli.MANIFEST_PATH)
    manifest = manifest if isinstance(manifest, dict) else {}
    return {
        "profile": {"name": active, "project_dir": profile.project_dir},
        "allowed_ports": manifest.get("allowed_ports", []),
        "endpoints": manifest.get("endpoints", []),
    }


def _git_porcelain(project_dir: str) -> list[str] | None:
    if not project_dir:
        return None
    try:
        inside = subprocess.run(
            ["git", "-C", project_dir, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=_DIFF_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return None
    try:
        status = subprocess.run(
            ["git", "-C", project_dir, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=_DIFF_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if status.returncode != 0:
        return None
    return [line for line in status.stdout.splitlines() if line.strip()]


def diff_payload() -> dict[str, object]:
    env_map = envfile.EnvFile.load(cli.ENV_FILE).as_map()
    profile = _load_profile(env_map.get("ACTIVE_PROFILE", ""))
    if profile is None:
        return {"git": False}
    porcelain = _git_porcelain(profile.project_dir)
    if porcelain is None:
        return {"git": False}
    return {"git": True, "porcelain": porcelain}


def read_events() -> list[dict[str, object]]:
    try:
        text = sink.default_events_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    events: list[dict[str, object]] = []
    for line in text.splitlines()[-_EVENTS_TAIL_LINES:]:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def jobs_payload() -> dict[str, object]:
    events = read_events()
    return {
        "jobs": jobs.correlate(events)["jobs"],
        "stalled": jobs.stall_alarm(events, time.time()),
    }


def permissions_payload() -> dict[str, object]:
    events = read_events()
    correlated = jobs.correlate(events)
    permissions = [
        job
        for job in correlated["jobs"]
        if job.get("permission") is not None or job.get("status") == "awaiting_permission"
    ]
    return {"permissions": permissions, "stalled": jobs.stall_alarm(events, time.time())}
