"""Cockpit UI HTTP server: loopback-only endpoints over host state."""

from __future__ import annotations

import json
import pathlib
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rdm import cli, docker, envfile, hostos, netprobe, procman, profiles, redact, tokens
from rdm.events import jobs, sink
from rdm.ui import auth, control, static

LOOPBACK_HOST = "127.0.0.1"
STATUS_TTL_SECONDS = 3.0
_TAIL_LINES = 200
_EVENTS_TAIL_LINES = 2000
_SSE_MAX_CONNECTIONS = 8
_SSE_POLL_SECONDS = 0.25
_SSE_HEARTBEAT_SECONDS = 15.0
_DIFF_TIMEOUT_SECONDS = 5.0
_LOG_SOURCES: dict[str, tuple[str, ...]] = {
    "host": ("rdm-host/*.out", "rdm-host/*.err"),
    "ingress": ("rdm-ingress/ingress.out", "rdm-ingress/ingress.err"),
    "watchdog": ("rdm-watchdog/watchdog.log",),
    "runner": ("rdm-runner/audit.log",),
}
_SENSITIVE_MARKERS = ("TOKEN", "SECRET", "PASSWORD")
_SENSITIVE_KEYS = ("VLESS_SUB_URL", "TUNNEL_TAIL")
_PROFILE_PREFIX = "/api/profile/"
_ACTION_PREFIX = "/api/action/"


def _load_profile(active: str) -> profiles.Profile | None:
    path = profiles.find(active) if active else None
    if path is None:
        return None
    try:
        return profiles.load(path)
    except (ValueError, OSError):
        return None


def _read_json(path: pathlib.Path) -> object | None:
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
    if hostos.psutil is None:
        return False
    for proc in hostos.psutil.process_iter():
        try:
            cmdline = proc.cmdline()
        except hostos.psutil.Error:
            continue
        if "watch" in cmdline and any("devbox" in part for part in cmdline):
            return True
    return False


def _build_status(port: int) -> dict[str, object]:
    env_map = envfile.EnvFile.load(cli.ENV_FILE).as_map()
    active = env_map.get("ACTIVE_PROFILE", "")
    profile_path = profiles.find(active) if active else None
    runner_port = cli._runner_port(env_map)
    return {
        "compose_ps": docker.compose_ps(cli.COMPOSE_FILE),
        "ports": {
            "cockpit": netprobe.can_connect(port),
            "ingress": netprobe.can_connect(procman.INGRESS_PORT),
            "bridge": netprobe.can_connect(profiles.BRIDGE_PORT),
            "runner": netprobe.can_connect(runner_port) if runner_port else None,
        },
        "host_services": _pidfile_alive(hostos.tempdir() / "rdm-host" / f"{active}-pids.txt"),
        "ingress_pids": _pidfile_alive(hostos.tempdir() / "rdm-ingress" / "pids.txt"),
        "watchdog": _watchdog_alive(),
        "env": _masked_env(env_map),
        "manifest": _read_json(cli.MANIFEST_PATH),
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


def _collect_logs(source: str, needle: str) -> list[dict[str, object]]:
    temp = hostos.tempdir()
    logs: list[dict[str, object]] = []
    for name in (source,) if source else tuple(_LOG_SOURCES):
        files = sorted(path for pattern in _LOG_SOURCES[name] for path in temp.glob(pattern))
        for path in files:
            lines = _tail(path, needle)
            if lines:
                logs.append({"source": name, "file": path.name, "lines": lines})
    return logs


def _handoff_payload(reveal: bool) -> dict[str, str]:
    env_map = envfile.EnvFile.load(cli.ENV_FILE).as_map()
    block = tokens.chat_block(
        env_map,
        netprobe.ingress_url(env_map, cli.COMPOSE_FILE),
        reveal,
        preview_url=env_map.get("PUBLIC_PREVIEW_URL", ""),
        runner_port=cli._runner_port(env_map),
        self_authed_ports=cli._self_authed_ports(env_map),
    )
    return {"handoff": block}


def _exposure_payload() -> dict[str, object] | None:
    env_map = envfile.EnvFile.load(cli.ENV_FILE).as_map()
    active = env_map.get("ACTIVE_PROFILE", "")
    profile = _load_profile(active)
    if profile is None:
        return None
    manifest = _read_json(cli.MANIFEST_PATH)
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


def _diff_payload() -> dict[str, object]:
    env_map = envfile.EnvFile.load(cli.ENV_FILE).as_map()
    profile = _load_profile(env_map.get("ACTIVE_PROFILE", ""))
    if profile is None:
        return {"git": False}
    porcelain = _git_porcelain(profile.project_dir)
    if porcelain is None:
        return {"git": False}
    return {"git": True, "porcelain": porcelain}


def _read_events() -> list[dict[str, object]]:
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


def _jobs_payload() -> dict[str, object]:
    events = _read_events()
    return {
        "jobs": jobs.correlate(events)["jobs"],
        "stalled": jobs.stall_alarm(events, time.time()),
    }


def _permissions_payload() -> dict[str, object]:
    events = _read_events()
    correlated = jobs.correlate(events)
    permissions = [
        job
        for job in correlated["jobs"]
        if job.get("permission") is not None or job.get("status") == "awaiting_permission"
    ]
    return {"permissions": permissions, "stalled": jobs.stall_alarm(events, time.time())}


def _plan_use(body: dict[str, object]) -> tuple[Callable[..., int], tuple[object, ...]]:
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string")
    return control.apply_use, (name,)


def _plan_allow(body: dict[str, object]) -> tuple[Callable[..., int], tuple[object, ...]]:
    port = body.get("port")
    ui = body.get("ui", False)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError("port must be an integer")
    if not isinstance(ui, bool):
        raise ValueError("ui must be a boolean")
    return control.allow_port, (port, ui)


def _plan_start(body: dict[str, object]) -> tuple[Callable[..., int], tuple[object, ...]]:
    name = body.get("name")
    preview = body.get("preview", False)
    if name is not None and not isinstance(name, str):
        raise ValueError("name must be a string")
    if not isinstance(preview, bool):
        raise ValueError("preview must be a boolean")
    return control.start, (name, preview)


_ACTION_PLANS: dict[str, Callable[[dict[str, object]], tuple[Callable[..., int], tuple[object, ...]]]] = {
    "use": _plan_use,
    "allow": _plan_allow,
    "start": _plan_start,
    "issue-tokens": lambda body: (control.issue_tokens, ()),
    "doctor": lambda body: (control.run_doctor, ()),
    "down": lambda body: (control.down, ()),
    "stop-host": lambda body: (control.stop_host, ()),
    "ingress-start": lambda body: (control.ingress, ("start",)),
    "ingress-stop": lambda body: (control.ingress, ("stop",)),
}


class CockpitServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, sessions: auth.SessionStore) -> None:
        self.sessions = sessions
        self.actions = control.ActionQueue()
        self._status_lock = threading.Lock()
        self._status_cache: dict[str, object] | None = None
        self._status_at = 0.0
        self._sse_lock = threading.Lock()
        self._sse_connections = 0
        super().__init__((LOOPBACK_HOST, port), _Handler)

    def status(self) -> dict[str, object]:
        with self._status_lock:
            if self._status_cache is not None and time.monotonic() - self._status_at < STATUS_TTL_SECONDS:
                return self._status_cache
            payload = _build_status(self.server_address[1])
            self._status_cache = payload
            self._status_at = time.monotonic()
            return payload

    def sse_acquire(self) -> bool:
        with self._sse_lock:
            if self._sse_connections >= _SSE_MAX_CONNECTIONS:
                return False
            self._sse_connections += 1
            return True

    def sse_release(self) -> None:
        with self._sse_lock:
            self._sse_connections -= 1


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        split = urllib.parse.urlsplit(self.path)
        if not auth.host_origin_ok(self._header_map(), self.server.server_address[1]):
            self._json(403, {"error": "forbidden", "hint": "Host/Origin must be loopback"})
            return
        if split.path in ("", "/"):
            self._serve_root(split.query)
            return
        session = self._session_token()
        if session is None:
            self._json(401, {"error": "unauthorized"})
            return
        if split.path.startswith("/api/"):
            self._serve_api(split.path, split.query, session)
        else:
            self._serve_static(split.path)

    def _serve_root(self, query: str) -> None:
        bootstrap = urllib.parse.parse_qs(query).get("t", [""])[0]
        if bootstrap:
            if auth.consume_bootstrap(bootstrap):
                session = self.server.sessions.mint_session()
                name, _, value = auth.cookie_header(session).partition(": ")
                self._respond(302, b"", "text/plain; charset=utf-8", ((name, value), ("Location", "/")))
                return
            if self._session_token() is None:
                self._json(403, {"error": "invalid or expired bootstrap"})
                return
            self._serve_index()
            return
        if self._session_token() is None:
            self._json(401, {"error": "unauthorized"})
            return
        self._serve_index()

    def _serve_api(self, path: str, query: str, session: str) -> None:
        if path == "/api/session":
            self._json(200, {"header_token": session})
        elif path == "/api/status":
            self._json(200, self.server.status())
        elif path == "/api/logs":
            self._api_logs(query)
        elif path == "/api/handoff":
            self._api_handoff(query)
        elif path == "/api/manifest":
            manifest = _read_json(cli.MANIFEST_PATH)
            if manifest is None:
                self._json(404, {"error": "manifest not found"})
            else:
                self._json(200, manifest)
        elif path == "/api/exposure":
            payload = _exposure_payload()
            if payload is None:
                self._json(404, {"error": "no active profile"})
            else:
                self._json(200, payload)
        elif path == "/api/diff":
            self._json(200, _diff_payload())
        elif path == "/api/events":
            self._api_events()
        elif path == "/api/jobs":
            self._json(200, _jobs_payload())
        elif path == "/api/permissions":
            self._json(200, _permissions_payload())
        elif path.startswith(_PROFILE_PREFIX):
            self._api_profile_get(path)
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        self.close_connection = True
        split = urllib.parse.urlsplit(self.path)
        if not auth.host_origin_ok(self._header_map(), self.server.server_address[1]):
            self._json(403, {"error": "forbidden", "hint": "Host/Origin must be loopback"})
            return
        session = self._session_token()
        if session is None:
            self._json(401, {"error": "unauthorized"})
            return
        if not self._header_token_ok():
            self._json(403, {"error": "action requires a valid header token"})
            return
        if split.path.startswith(_PROFILE_PREFIX):
            self._api_profile_put(split.path)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        self.close_connection = True
        split = urllib.parse.urlsplit(self.path)
        if not auth.host_origin_ok(self._header_map(), self.server.server_address[1]):
            self._json(403, {"error": "forbidden", "hint": "Host/Origin must be loopback"})
            return
        session = self._session_token()
        if session is None:
            self._json(401, {"error": "unauthorized"})
            return
        if not self._header_token_ok():
            self._json(403, {"error": "action requires a valid header token"})
            return
        if split.path.startswith(_ACTION_PREFIX):
            self._api_action(split.path)
        else:
            self._json(404, {"error": "not found"})

    def _profile_target(self, path: str) -> tuple[str, pathlib.Path] | None:
        name = path[len(_PROFILE_PREFIX):]
        if not cli._NAME_RE.fullmatch(name):
            self._json(404, {"error": "invalid profile name"})
            return None
        resolved = profiles.find(name)
        if resolved is None:
            self._json(404, {"error": "profile not found"})
            return None
        return name, resolved

    def _api_profile_get(self, path: str) -> None:
        target = self._profile_target(path)
        if target is None:
            return
        name, resolved = target
        raw = _read_json(resolved)
        if not isinstance(raw, dict):
            self._json(500, {"error": "profile unreadable"})
            return
        self._json(200, {"name": name, "path": str(resolved), "source_dir": str(resolved.parent), "raw": raw})

    def _api_profile_put(self, path: str) -> None:
        target = self._profile_target(path)
        if target is None:
            return
        name, resolved = target
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length)) if length > 0 else None
        except ValueError:
            self._json(400, {"error": "body must be valid JSON"})
            return
        try:
            profiles.write_raw(resolved, data)
        except ValueError as error:
            self._json(400, {"error": str(error)})
            return
        self._json(200, {"name": name, "path": str(resolved)})

    def _api_action(self, path: str) -> None:
        plan = _ACTION_PLANS.get(path[len(_ACTION_PREFIX):])
        if plan is None:
            self._json(404, {"error": "unknown action"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length)) if length > 0 else None
        except ValueError:
            self._json(400, {"error": "body must be valid JSON"})
            return
        body = {} if data is None else data
        if not isinstance(body, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return
        try:
            fn, args = plan(body)
        except ValueError as error:
            self._json(400, {"error": str(error)})
            return
        action_id = self.server.actions.submit(fn, *args)
        if action_id is None:
            self._json(409, {"error": "another action is already running"})
            return
        self._json(202, {"action_id": action_id})

    def _api_logs(self, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        source = params.get("source", [""])[0]
        needle = params.get("filter", [""])[0].lower()
        if source and source not in _LOG_SOURCES:
            self._json(404, {"error": "unknown source"})
            return
        self._json(200, {"logs": _collect_logs(source, needle)})

    def _api_handoff(self, query: str) -> None:
        reveal = urllib.parse.parse_qs(query).get("reveal", [""])[0] == "1"
        if reveal and not self._header_token_ok():
            self._json(403, {"error": "reveal requires a valid header token"})
            return
        self._json(200, _handoff_payload(reveal))

    def _api_events(self) -> None:
        if not self.server.sse_acquire():
            self._json(503, {"error": "sse connection limit reached"})
            return
        try:
            self._sse_stream()
        finally:
            self.server.sse_release()

    def _sse_stream(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        path = sink.default_events_path()
        offset = 0
        last_heartbeat = time.monotonic()
        while True:
            try:
                offset = self._sse_drain(path, offset)
                now = time.monotonic()
                if now - last_heartbeat >= _SSE_HEARTBEAT_SECONDS:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = now
            except OSError:
                return
            time.sleep(_SSE_POLL_SECONDS)

    def _sse_drain(self, path: pathlib.Path, offset: int) -> int:
        try:
            size = path.stat().st_size
        except OSError:
            return offset
        if size < offset:
            offset = 0
        if size == offset:
            return offset
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            return offset
        end = chunk.rfind(b"\n")
        if end < 0:
            return offset
        payload = bytearray()
        for line in chunk[: end + 1].splitlines():
            if line.strip():
                payload += b"data: " + line + b"\n\n"
        if payload:
            self.wfile.write(bytes(payload))
            self.wfile.flush()
        return offset + end + 1

    def _serve_index(self, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
        resolved = static.resolve("/")
        if resolved is None:
            self._json(404, {"error": "index.html not found"}, extra_headers)
            return
        data, mime = resolved
        self._respond(200, data, mime, extra_headers)

    def _serve_static(self, path: str) -> None:
        resolved = static.resolve(path)
        if resolved is None:
            self._json(404, {"error": "not found"})
            return
        data, mime = resolved
        self._respond(200, data, mime)

    def _header_map(self) -> dict[str, str]:
        return {name.capitalize(): value for name, value in self.headers.items()}

    def _session_token(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "rdm_ui" and self.server.sessions.verify_session(value):
                return value
        return None

    def _header_token_ok(self) -> bool:
        token = self.headers.get("X-RDM-Token", "")
        return bool(token) and self.server.sessions.verify_session(token)

    def _respond(
        self, code: int, body: bytes, content_type: str, extra_headers: tuple[tuple[str, str], ...] = ()
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: object, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(code, body, "application/json; charset=utf-8", extra_headers)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # query может нести одноразовый bootstrap-токен — в лог уходит только путь
        path = urllib.parse.urlsplit(self.path).path
        self.log_message('"%s %s" %s %s', self.command, path, code, size)


def build_server(host: str, port: int, sessions: auth.SessionStore | None = None) -> CockpitServer:
    if host != LOOPBACK_HOST:
        raise ValueError(f"cockpit binds {LOOPBACK_HOST!r} only, refusing {host!r}")
    return CockpitServer(port, sessions if sessions is not None else auth.SessionStore())
