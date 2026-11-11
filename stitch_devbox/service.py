"""Command layer for the stitch-devbox service plugin.

Thin adapter over the remote-devbox-mcp ``rdm`` core: the plugin imports
``rdm`` in-process (``<repo>/home`` on ``sys.path``) and reuses the cockpit
payload builders and single-flight action queue. Read commands return what the
cockpit UI shows; write commands enqueue the same CLI entry points the tray
and cockpit use, with stdout redirected to stderr so the JSON-RPC channel
stays clean.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HOME_DIR = _REPO_ROOT / "home"
if _HOME_DIR.is_dir() and str(_HOME_DIR) not in sys.path:
    sys.path.insert(0, str(_HOME_DIR))

_rd: Any = None
_queue: Any = None
_host_capabilities: frozenset[str] = frozenset()

_REAL_ENV_KEYS = ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP")
_COCKPIT_WAIT_SECONDS = 5.0
_INGRESS_ACTIONS = {"up": "start", "down": "stop"}
_WATCHDOG_START_GRACE_SECONDS = 1.5


def set_host_capabilities(supported: list[str]) -> None:
    """Store host capabilities advertised in the plugin.init handshake."""
    global _host_capabilities
    _host_capabilities = frozenset(supported)


def _real_user_env() -> dict[str, str]:
    """Real user environment values hidden by the sandbox scope.

    Windows: per-session values from HKCU ``Volatile Environment`` (the
    environment the current logon session was launched with).  POSIX: the
    passwd home directory only.
    """
    if os.name == "nt":
        values: dict[str, str] = {}
        try:
            import winreg  # noqa: PLC0415

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Volatile Environment"
            ) as key:
                for name in _REAL_ENV_KEYS:
                    try:
                        values[name] = str(winreg.QueryValueEx(key, name)[0])
                    except OSError:
                        continue
        except OSError:
            return {}
        return values
    try:
        import pwd  # noqa: PLC0415

        return {"HOME": pwd.getpwuid(os.getuid()).pw_dir}
    except (ImportError, KeyError, OSError):
        return {}


def _adopt_real_home() -> None:
    """Restore the real user home and temp before rdm resolves its paths.

    The Stitch host spawns plugin children with a sandbox-scoped
    ``USERPROFILE``/``HOME``/``TEMP``; rdm resolves profiles
    (``~/.devbox/projects``) relative to ``Path.home()`` and events, pidfiles
    and log sources relative to the scoped temp, so under the sandbox the
    real stack is invisible and stop actions are no-ops.  Recover the real
    values from the OS (registry on Windows, passwd on POSIX).  The host
    scopes HOME/USERPROFILE for every plugin; ``host_driver`` opens only the
    real TEMP/TMP, never the profile, so this recovery always runs.
    """
    real = _real_user_env()
    profile = real.get("USERPROFILE") or real.get("HOME")
    if not profile or Path(profile) == Path.home():
        return
    os.environ["HOME"] = profile
    if os.name == "nt":
        os.environ["USERPROFILE"] = profile
        if real.get("HOMEDRIVE") and real.get("HOMEPATH"):
            os.environ["HOMEDRIVE"] = real["HOMEDRIVE"]
            os.environ["HOMEPATH"] = real["HOMEPATH"]
        else:
            drive, _, tail = profile.partition(":")
            if tail:
                os.environ["HOMEDRIVE"] = f"{drive}:"
                os.environ["HOMEPATH"] = tail
        for name in ("TEMP", "TMP"):
            if real.get(name):
                os.environ[name] = real[name]


def _rdm() -> Any:
    """Lazily import the rdm package; raise a clear error when unavailable."""
    global _rd
    if _rd is None:
        _adopt_real_home()
        try:
            import rdm  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "rdm core not importable from the devbox repo checkout"
            ) from exc
        _rd = True
    import rdm.cli  # noqa: PLC0415
    import rdm.prompt  # noqa: PLC0415
    import rdm.ui.api  # noqa: PLC0415

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
    payload = rdm.ui.api.build_status(0)
    payload.pop("env", None)
    return payload


def _iso(ts: object) -> str:
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return str(ts) if ts else ""


def _port_card(card_id: str, state: object) -> tuple[str, str, str, str, str | None]:
    if state is None:
        return (card_id, card_id, "off", "warn", None)
    up = bool(state)
    return (
        card_id,
        card_id,
        "up" if up else "down",
        "ok" if up else "down",
        None if up else f"stitch-devbox.hint.{card_id}",
    )


def _runner_card(ports: dict[str, Any], runner_port: int | None) -> tuple[str, str, str, str, str | None]:
    if runner_port is None:
        return ("runner", "runner", "off", "ok", None)
    if ports.get("runner"):
        return ("runner", "runner", "up", "ok", None)
    return ("runner", "runner", "down", "down", "stitch-devbox.hint.runner")


def _gateway_block(payload: dict[str, Any]) -> dict[str, Any]:
    rdm = _rdm()
    env_map = payload.get("env", {}) or {}
    ports = payload.get("ports", {}) or {}
    profile = _load_profile(env_map)
    exposed = profile.exposed_mcps if profile is not None else ()
    enabled = bool(exposed)
    gateway_events = [event for event in rdm.ui.api.read_events() if event.get("kind") == "gateway"]
    return {
        "enabled": enabled,
        "up": enabled and bool(ports.get("gateway")),
        "port": rdm.tokens.gateway_port_for(profile) if profile is not None else None,
        "calls": len(gateway_events),
        "errors": sum(1 for event in gateway_events if event.get("status") == "error"),
        "exposed_mcps": [
            {"name": mcp.name, "mode": mcp.mode, "tools": list(mcp.tool_allow)} for mcp in exposed
        ],
    }


def overview() -> dict[str, Any]:
    """Vitals cards plus the gateway block for the frozen overview contract."""
    rdm = _rdm()
    payload = rdm.ui.api.build_status(0)
    env_map = payload.get("env", {}) or {}
    ports = payload.get("ports", {}) or {}
    host_services = payload.get("host_services") or {}
    updated_at = datetime.now(timezone.utc).isoformat()
    profile = env_map.get("ACTIVE_PROFILE", "")
    profile_loaded = _load_profile(env_map)
    alive = int(host_services.get("alive", 0))
    recorded = int(host_services.get("recorded", 0))
    services_down = recorded > 0 and alive == 0
    cards = [
        (
            "profile",
            "profile",
            profile or "—",
            "ok" if profile_loaded is not None else "warn",
            None if profile_loaded is not None else "stitch-devbox.hint.profile",
        ),
        _port_card("ingress", ports.get("ingress")),
        _port_card("bridge", ports.get("bridge")),
        _runner_card(ports, rdm.cli._runner_port(env_map)),
        (
            "watchdog",
            "watchdog",
            "on" if payload.get("watchdog") else "off",
            "ok" if payload.get("watchdog") else "warn",
            None if payload.get("watchdog") else "stitch-devbox.hint.watchdog",
        ),
        (
            "host_services",
            "hostServices",
            f"{alive}/{recorded}",
            "warn" if services_down else "ok",
            "stitch-devbox.hint.hostServices" if services_down else None,
        ),
    ]
    rows: list[dict[str, Any]] = []
    for card_id, title_key, value, tone, hint in cards:
        row: dict[str, Any] = {
            "id": card_id,
            "title": f"stitch-devbox.card.{title_key}",
            "value": value,
            "tone": tone,
            "updatedAt": updated_at,
        }
        if hint is not None:
            row["hint"] = hint
        rows.append(row)
    return {"cards": rows, "gateway": _gateway_block(payload)}


def jobs() -> dict[str, Any]:
    return _rdm().ui.api.jobs_payload()


def permissions() -> dict[str, Any]:
    return _rdm().ui.api.permissions_payload()


def exposure() -> dict[str, Any]:
    payload = _rdm().ui.api.exposure_payload()
    return payload if payload is not None else {"profile": None}


def diff() -> dict[str, Any]:
    return _rdm().ui.api.diff_payload()


def manifest() -> dict[str, Any]:
    payload = _rdm().ui.api.read_json(_rdm().cli.MANIFEST_PATH)
    return payload if isinstance(payload, dict) else {}


def events_tail(limit: int = 50) -> list[dict[str, Any]]:
    events = _rdm().ui.api.read_events()
    return events[-max(1, min(int(limit), 500)):]


def logs(source: str = "", filter: str = "", limit: int = 80) -> dict[str, Any]:  # noqa: A002
    entries = _rdm().ui.api.collect_logs(source, filter.lower())
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        grouped.setdefault(str(entry["source"]), []).extend(str(line) for line in entry["lines"])
    bounded = max(1, min(int(limit), 500))
    return {
        "sources": [
            {"name": name, "lines": lines[-bounded:]} for name, lines in grouped.items()
        ]
    }


def logs_text(limit: int = 80) -> dict[str, Any]:
    """Markdown block: tail of every log source, for the page."""
    entries = _rdm().ui.api.collect_logs("", "")
    lines: list[str] = []
    for entry in entries[-max(1, min(int(limit), 300)):]:
        lines.append(f"**{entry['source']} — {entry['file']}**")
        lines.append("```")
        lines.extend(str(line) for line in entry["lines"][-40:])
        lines.append("```")
    return {"text": "\n".join(lines) if lines else "stitch-devbox.logsEmpty"}


def profiles_list() -> list[dict[str, Any]]:
    rdm = _rdm()
    active = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map().get("ACTIVE_PROFILE", "")
    rows = []
    for name in rdm.profiles.available():
        row: dict[str, Any] = {"name": name, "mode": "?", "project_dir": "", "active": name == active}
        path = rdm.profiles.find(name)
        if path is not None:
            try:
                profile = rdm.profiles.load(path)
            except (ValueError, OSError):
                pass
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
    try:
        json_text = path.read_text(encoding="utf-8")
    except OSError:
        raise ValueError(f"profile unreadable: {name}") from None
    try:
        data = json.loads(json_text)
    except ValueError as exc:
        return {"json": json_text, "valid": False, "errors": [f"json: {exc}"]}
    if not isinstance(data, dict):
        return {"json": json_text, "valid": False, "errors": ["json: expected an object"]}
    try:
        problems = [
            problem
            for problem in rdm.profiles.validate(rdm.profiles.load(path))
            if not problem.startswith("WARN")
        ]
    except ValueError as exc:
        return {"json": json_text, "valid": False, "errors": [str(exc)]}
    return {"json": json_text, "valid": not problems, "errors": problems}


def _git_config_value(key: str, project_dir: str = "", scope: str = "") -> str:
    args = ["git"]
    target = Path(project_dir).expanduser() if project_dir else None
    if target is not None and target.is_dir():
        args += ["-C", str(target)]
    args.append("config")
    if scope:
        args.append(f"--{scope}")
    args += ["--get", key]
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_identity(project_dir: str = "") -> tuple[str, str, str]:
    for scope in ("local", "global"):
        name = _git_config_value("user.name", project_dir, scope)
        email = _git_config_value("user.email", project_dir, scope)
        if name and email:
            return name, email, scope
    return "", "", "none"


def folder_check(path: str) -> dict[str, Any]:
    target = Path(path).expanduser()
    return {"exists": target.is_dir(), "is_git": (target / ".git").exists()}


def profile_defaults(project_dir: str = "") -> dict[str, Any]:
    name, email, source = _git_identity(project_dir)
    return {"git_name": name, "git_email": email, "git_source": source}


_DETECT_CHILD_LIMIT = 20
_DETECT_STACK_LIMIT = 8
_MAX_JSON_BYTES = 2 * 1024 * 1024
_SKIP_DIRS = ("node_modules", "dist", "build", "target", "vendor", ".git")
_WORKSPACE_DIRS = ("packages", "apps", "services", "modules", "crates")


def _normalize_dir(path: str) -> str:
    target = Path(path).expanduser()
    try:
        resolved = target.resolve(strict=False)
    except OSError:
        resolved = target
    return str(resolved).replace("\\", "/")


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_git_repo(target: Path) -> bool:
    if (target / ".git").exists():
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _node_stack(root: Path) -> dict[str, Any] | None:
    manifest = root / "package.json"
    if not manifest.exists():
        return None
    package = _read_json_file(manifest)
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies"):
        block = package.get(key)
        if isinstance(block, dict):
            deps.update(block)
    markers = ["package.json"]
    if "vite" in deps or (root / "vite.config.ts").exists() or (root / "vite.config.js").exists():
        return {"id": "vite", "markers": markers, "port": 5173, "toolchain": ""}
    if "next" in deps:
        return {"id": "next", "markers": markers, "port": 3000, "toolchain": ""}
    if "@angular/core" in deps:
        return {"id": "angular", "markers": markers, "port": 4200, "toolchain": ""}
    if "react-scripts" in deps:
        return {"id": "cra", "markers": markers, "port": 3000, "toolchain": ""}
    return {"id": "node", "markers": markers, "port": 3000, "toolchain": ""}


def _stack_candidates(root: Path) -> list[dict[str, Any]]:
    stacks: list[dict[str, Any]] = []
    node = _node_stack(root)
    if node is not None:
        stacks.append(node)
    java_markers = [
        name for name in ("pom.xml", "build.gradle", "build.gradle.kts", "gradlew") if (root / name).exists()
    ]
    if java_markers:
        stacks.append({"id": "java", "markers": java_markers[:1], "port": 8080, "toolchain": "java21"})
    if (root / "pubspec.yaml").exists():
        stacks.append({"id": "flutter", "markers": ["pubspec.yaml"], "port": 8080, "toolchain": "flutter"})
    python_markers = [name for name in ("pyproject.toml", "requirements.txt") if (root / name).exists()]
    if python_markers:
        stacks.append({"id": "python", "markers": python_markers[:1], "port": 8000, "toolchain": ""})
    if (root / "go.mod").exists():
        stacks.append({"id": "go", "markers": ["go.mod"], "port": 8080, "toolchain": ""})
    if (root / "Cargo.toml").exists():
        stacks.append({"id": "rust", "markers": ["Cargo.toml"], "port": 0, "toolchain": ""})
    if (root / "composer.json").exists():
        stacks.append({"id": "php", "markers": ["composer.json"], "port": 8000, "toolchain": ""})
    if any(root.glob("*.csproj")) or any(root.glob("*.sln")):
        stacks.append({"id": "dotnet", "markers": ["*.csproj"], "port": 5000, "toolchain": ""})
    return stacks


def _child_dirs(root: Path) -> list[Path]:
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    dirs: list[Path] = []
    for entry in entries:
        if len(dirs) >= _DETECT_CHILD_LIMIT:
            break
        if entry.name.startswith(".") or entry.name in _SKIP_DIRS:
            continue
        try:
            if entry.is_dir():
                dirs.append(entry)
        except OSError:
            continue
    return dirs


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def profile_detect(path: str = "") -> dict[str, Any]:
    raw = str(path or "").strip()
    requested = Path(raw).expanduser() if raw else None
    relative = requested is not None and not requested.is_absolute()
    normalized = _normalize_dir(raw) if raw and not relative else ""
    target = Path(normalized) if normalized else None
    root_like = bool(
        target is not None and (target == Path(target.anchor) or len(target.parts) < 2)
    )
    exists = bool(target is not None and not root_like and target.is_dir())
    is_git = bool(exists and target is not None and _is_git_repo(target))
    result: dict[str, Any] = {
        "path": normalized,
        "exists": exists,
        "is_git": is_git,
        "name": target.name if target is not None else "",
        "stacks": [],
        "suggested": None,
        "provenance": {},
        "warnings": [],
        "port_conflicts": [],
        "requires_choice": False,
    }
    if target is None or not exists:
        result["warnings"].append({"code": "not_absolute" if relative else "missing"})
        return result
    stacks = list(_stack_candidates(target))
    for child in _child_dirs(target):
        for stack in _stack_candidates(child):
            stacks.append({**stack, "subdir": child.name})
        if child.name in _WORKSPACE_DIRS:
            for nested in _child_dirs(child):
                for stack in _stack_candidates(nested):
                    stacks.append({**stack, "subdir": f"{child.name}/{nested.name}"})
    result["stacks"] = stacks[:_DETECT_STACK_LIMIT]
    result["requires_choice"] = len(stacks) > 1
    primary = next((stack for stack in stacks if not stack.get("subdir")), stacks[0] if stacks else None)
    git_name, git_email, git_source = _git_identity(normalized)
    suggested: dict[str, Any] = {"project_dir": normalized, "mode": "standard"}
    if git_name:
        suggested["git_name"] = git_name
    if git_email:
        suggested["git_email"] = git_email
    if primary is not None:
        if primary["toolchain"]:
            suggested["toolchain"] = primary["toolchain"]
        if primary["port"]:
            suggested["ui_port"] = primary["port"]
            suggested["allowed_ports"] = [primary["port"]]
    result["suggested"] = suggested
    result["provenance"] = {
        "git": git_source,
        "toolchain": "detected" if primary is not None and primary["toolchain"] else "none",
        "ui_port": "detected" if primary is not None and primary["port"] else "none",
    }
    if len(stacks) > 1:
        result["warnings"].append(
            {
                "code": "monorepo",
                "stacks": [
                    f"{stack['subdir']}/{stack['id']}" if stack.get("subdir") else stack["id"]
                    for stack in stacks
                ],
            }
        )
    if not stacks:
        result["warnings"].append({"code": "no_stack"})
    if not is_git:
        result["warnings"].append({"code": "not_git"})
    if primary is not None and primary["port"] and _port_in_use(primary["port"]):
        result["port_conflicts"].append({"port": primary["port"]})
    return result


def _previous_exposed(rdm: Any, path: Path) -> tuple[Any, ...] | None:
    try:
        return rdm.profiles.load(path).exposed_mcps
    except (OSError, ValueError):
        return None


def _refresh_manifest_if_active(rdm: Any, name: str) -> None:
    env_map = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map()
    if env_map.get("ACTIVE_PROFILE") == name:
        rdm.cli._refresh_manifest_from_env()


def profile_put(name: str, json_text: str) -> dict[str, Any]:
    rdm = _rdm()
    if not name or not rdm.cli._NAME_RE.fullmatch(name):
        raise ValueError(f"invalid profile name: {name!r}")
    path = rdm.profiles.find(name)
    if path is None:
        path = rdm.profiles.new_path(name)
    try:
        data = json.loads(json_text)
    except ValueError as exc:
        return {"valid": False, "errors": [f"json: {exc}"]}
    if not isinstance(data, dict):
        return {"valid": False, "errors": ["json: expected an object"]}
    if not data.get("git_name") or not data.get("git_email"):
        fallback_name, fallback_email, _ = _git_identity(str(data.get("project_dir") or ""))
        if not data.get("git_name"):
            data["git_name"] = fallback_name or name
        if not data.get("git_email"):
            data["git_email"] = fallback_email or f"{name}@devbox.local"
    try:
        new_profile = rdm.profiles._build(rdm.profiles._fields(data, rdm.profiles._TOP_KEYS, "profile"))
    except ValueError as exc:
        return {"valid": False, "errors": str(exc).split("; ")}
    problems = [problem for problem in rdm.profiles.validate(new_profile) if not problem.startswith("WARN")]
    if problems:
        return {"valid": False, "errors": problems}
    exposed_changed = new_profile.exposed_mcps != _previous_exposed(rdm, path)
    if exposed_changed and new_profile.exposed_mcps:
        from rdm.gateway import snapshot  # noqa: PLC0415

        try:
            snapshot.save_snapshot(path, new_profile)
        except snapshot.GatewaySnapshotError as exc:
            return {"valid": False, "errors": [str(exc)]}
    try:
        rdm.profiles.write_raw(path, data)
    except ValueError as exc:
        return {"valid": False, "errors": str(exc).split("; ")}
    if exposed_changed:
        _refresh_manifest_if_active(rdm, name)
    return {"valid": True, "errors": []}


def profile_delete(name: str) -> dict[str, Any]:
    rdm = _rdm()
    if not name or not rdm.cli._NAME_RE.fullmatch(name):
        raise ValueError(f"invalid profile name: {name!r}")
    path = rdm.profiles.find(name)
    if path is None:
        raise ValueError(f"profile not found: {name}")
    path.unlink()
    return {"deleted": True}


def _queue_submit(fn_name: str, *args: Any, payload_key: str = "") -> dict[str, Any]:
    """Enqueue a cockpit CLI callable on the single-flight action queue.

    With ``payload_key`` the runner captures the cli callable's stdout and
    returns it as ``{"exit": rc, payload_key: text}`` — failure output is
    kept too, so the Actions table can show why a command died; the
    ActionQueue stores that dict on the finished action event (redacted,
    size-capped).
    """
    import contextlib
    import io

    global _queue
    rdm = _rdm()
    if _queue is None:
        from rdm.ui import control  # noqa: PLC0415

        _queue = control.ActionQueue()

    fn = getattr(rdm.cli, fn_name)

    def _runner() -> int | dict[str, Any]:
        # cli entry points print human output; keep the RPC stdout clean.
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                rc = int(fn(*args))
        finally:
            # actions mutate host state; the TTL caches must not keep serving
            # pre-action vitals to the poll loop after the action is done.
            rdm.ui.api.invalidate_status_cache()
        if not payload_key:
            return rc
        return {"exit": rc, payload_key: buffer.getvalue()}

    _runner.__name__ = fn_name

    action_id = _queue.submit(_runner)
    if action_id is None:
        return {"accepted": False, "reason": "another action is already running"}
    return {"accepted": True, "actionId": action_id}


def _recent_actions() -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events_tail(500):
        if event.get("kind") != "action":
            continue
        action_id = str(event.get("job_id") or "")
        if not action_id:
            continue
        row = rows.get(action_id)
        if row is None:
            row = {"id": action_id, "cmd": str(event.get("tool") or ""), "status": "started", "startedAt": ""}
            rows[action_id] = row
            order.append(action_id)
        status = str(event.get("status") or "")
        if status == "started":
            row["startedAt"] = _iso(event.get("ts"))
        elif status in ("finished", "interrupted"):
            row["status"] = status
            row["finishedAt"] = _iso(event.get("ts"))
            if event.get("exit") is not None:
                row["exit"] = event.get("exit")
            if event.get("payload") is not None:
                row["payload"] = event.get("payload")
    return [rows[action_id] for action_id in order][-10:]


def action_status() -> dict[str, Any]:
    busy = bool(_queue is not None and _queue.busy())
    payload: dict[str, Any] = {"busy": busy, "recent": _recent_actions()}
    if _queue is not None:
        current = _queue.current()
        if current is not None:
            payload["current"] = current
    return payload


def reconcile_actions() -> dict[str, Any]:
    try:
        _rdm()
        from rdm.ui import control  # noqa: PLC0415

        interrupted = control.reconcile_open_actions()
    except Exception:  # noqa: BLE001 - reconcile must not break plugin init
        interrupted = 0
    return {"interrupted": interrupted}


def stack_start(name: str = "", preview: bool = False) -> dict[str, Any]:
    return _queue_submit("_start", name or None, bool(preview), payload_key="output")


def stack_down() -> dict[str, Any]:
    return _queue_submit("_down")


def profile_use(name: str) -> dict[str, Any]:
    if not name:
        raise ValueError("name is required")
    return _queue_submit("apply_use", name)


def stop_host() -> dict[str, Any]:
    return _queue_submit("_stop_host")


def cockpit_open() -> dict[str, Any]:
    rdm = _rdm()
    from rdm.ui import auth  # noqa: PLC0415

    env_map = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map()
    rdm.procman.start_ui(env_map, rdm.cli.HOME_DIR)
    deadline = time.monotonic() + _COCKPIT_WAIT_SECONDS
    while not rdm.netprobe.can_connect(rdm.ports.COCKPIT_PORT):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"cockpit did not start on 127.0.0.1:{rdm.ports.COCKPIT_PORT}"
            )
        time.sleep(0.2)
    token = auth.write_bootstrap()
    return {"url": f"http://127.0.0.1:{rdm.ports.COCKPIT_PORT}/?t={token}"}


def ingress_control(action: str = "up") -> dict[str, Any]:
    normalized = _INGRESS_ACTIONS.get(str(action))
    if normalized is None:
        raise ValueError(f"unknown ingress action: {action!r}")
    return _queue_submit("_ingress", normalized)


def watchdog_control(action: str = "start") -> dict[str, Any]:
    if str(action) not in ("start", "stop"):
        raise ValueError(f"unknown watchdog action: {action!r}")
    if str(action) == "stop":
        return _queue_submit("_watchdog_control", "stop")
    # start must stay synchronous: the UI toasts accepted=False from the RPC result itself
    rdm = _rdm()
    ports = rdm.ui.api.build_status(0).get("ports") or {}
    if not any(ports.get(key) for key in ("ingress", "bridge", "runner")):
        return {
            "accepted": False,
            "reason": "watchdog cannot run while the devbox stack is down; raise the project stack first",
        }
    env_map = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map()
    rdm.procman.start_watchdog(env_map, rdm.cli.HOME_DIR)
    time.sleep(_WATCHDOG_START_GRACE_SECONDS)
    if not rdm.ui.api.build_status(0).get("watchdog"):
        rdm.procman.stop_watchdog()
        return {
            "accepted": False,
            "reason": "watchdog did not stay running; check rdm-watchdog logs and retry",
        }
    return {"accepted": True}


def stack_full_down() -> dict[str, Any]:
    return _queue_submit("_full_down")


def issue_tokens() -> dict[str, Any]:
    return _queue_submit("_issue_tokens", payload_key="chatBlock")


def run_doctor() -> dict[str, Any]:
    return _queue_submit("_doctor", payload_key="report")


def handoff_block(reveal: bool = False) -> dict[str, Any]:
    rdm = _rdm()
    full = bool(reveal)
    env_map = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map()
    block = rdm.tokens.chat_block(
        env_map,
        rdm.netprobe.ingress_url(env_map, rdm.cli.COMPOSE_FILE),
        full,
        preview_url=env_map.get("PUBLIC_PREVIEW_URL", ""),
        runner_port=rdm.cli._runner_port(env_map),
        self_authed_ports=rdm.cli._self_authed_ports(env_map),
        gateway_port=rdm.cli._gateway_port(env_map),
    )
    return {"block": block, "masked": not full}


def _load_profile(env_map: dict[str, Any]) -> Any:
    rdm = _rdm()
    name = env_map.get("ACTIVE_PROFILE", "")
    path = rdm.profiles.find(name) if name else None
    if path is None:
        return None
    try:
        return rdm.profiles.load(path)
    except (ValueError, OSError):
        return None


def _active_profile_context(env_map: dict[str, Any]) -> dict[str, str] | None:
    profile = _load_profile(env_map)
    if profile is None:
        return None
    return {"project_dir": profile.project_dir, "mode": profile.mode, "toolchain": profile.toolchain}


def _gateway_mcps(
    env_map: dict[str, Any], gateway_port: int | None
) -> tuple[tuple[str, str, tuple[str, ...]], ...] | None:
    if gateway_port is None:
        return None
    profile = _load_profile(env_map)
    if profile is None:
        return None
    return tuple((mcp.name, mcp.mode, mcp.tool_allow) for mcp in profile.exposed_mcps)


def agent_prompt(
    task: str = "", acceptance: str = "", reveal: bool = False, client: str = "universal"
) -> dict[str, Any]:
    rdm = _rdm()
    full = bool(reveal)
    env_map = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map()
    ingress_url = rdm.netprobe.ingress_url(env_map, rdm.cli.COMPOSE_FILE)
    gateway_port = rdm.cli._gateway_port(env_map)
    prompt_text = rdm.prompt.compose_prompt(
        env_map,
        ingress_url,
        env_map.get("PUBLIC_PREVIEW_URL", ""),
        rdm.cli._runner_port(env_map),
        rdm.cli._self_authed_ports(env_map),
        _active_profile_context(env_map),
        task,
        acceptance,
        full,
        client,
        gateway_port=gateway_port,
        gateway_mcps=_gateway_mcps(env_map, gateway_port),
    )
    ports = rdm.ui.api.build_status(0).get("ports", {}) or {}
    runner_port = rdm.cli._runner_port(env_map)
    ready = {
        "profile": _load_profile(env_map) is not None,
        "bridge": bool(ports.get("bridge")),
        # the ingress port probe only sees the local proxy; the agent needs the public tunnel URL
        "ingress": bool(ports.get("ingress")) and bool(ingress_url),
        "runner": "off" if runner_port is None else ("up" if ports.get("runner") else "down"),
        "gateway": gateway_port is not None and bool(ports.get("gateway")),
    }
    connection = rdm.prompt.connection_payload(
        env_map,
        ingress_url,
        full,
        runner_port=rdm.cli._runner_port(env_map),
        gateway_port=gateway_port,
    )
    return {"prompt": prompt_text, "masked": not full, "ready": ready, "connection": connection}


def connection_check() -> dict[str, Any]:
    """Probe the public bridge endpoint end-to-end: initialize + tools/list."""
    rdm = _rdm()
    env_map = rdm.envfile.EnvFile.load(rdm.cli.ENV_FILE).as_map()
    ingress_url = rdm.netprobe.ingress_url(env_map, rdm.cli.COMPOSE_FILE)
    if not ingress_url:
        return {"ok": False, "error": "ingress URL unknown"}
    url = f"{ingress_url}/p/{rdm.profiles.BRIDGE_PORT}/mcp"
    token = env_map.get("MCP_BEARER_TOKEN", "")
    if not token:
        return {"ok": False, "url": url, "error": "MCP_BEARER_TOKEN empty"}
    return _probe_bridge(url, token)


def _probe_bridge(url: str, token: str) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    started = time.monotonic()

    def post(payload: dict[str, Any], session: str | None) -> dict[str, Any]:
        data = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if session:
            headers["Mcp-Session-Id"] = session
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            new_session = response.headers.get("Mcp-Session-Id")
            if new_session:
                headers["Mcp-Session-Id"] = new_session
            body = response.read().decode("utf-8", "replace")
        message = json.loads(body) if body.strip() else {}
        if not isinstance(message, dict) or "error" in message:
            return {"ok": False, "url": url, "error": f"bad response: {str(message)[:120]}"}
        return {"ok": True, "session": new_session, "result": message.get("result", {})}

    try:
        init = post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "stitch-connection-check", "version": "1.0"},
                },
            },
            None,
        )
        if not init.get("ok"):
            return init
        session = init.get("session")
        tools = post(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session,
        )
        if not tools.get("ok"):
            return tools
        tool_count = len(tools.get("result", {}).get("tools", []))
        server_info = init.get("result", {}).get("serverInfo", {})
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": True,
            "url": url,
            "tools": tool_count,
            "server": str(server_info.get("name", "")),
            "latency_ms": latency_ms,
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "url": url, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "url": url, "error": str(exc)[:120]}


def poll() -> dict[str, Any]:
    """Batch the five page-refresh reads into one RPC round-trip.

    Payload keys mirror the individual commands verbatim; overview's cached
    build_status makes the docker compose ps cost paid once per batch.
    """
    from . import jobcontrol  # noqa: PLC0415 - jobcontrol imports service

    return {
        "overview": overview(),
        "action_status": action_status(),
        "jobs": jobcontrol.jobs_live(),
        "permissions": jobcontrol.permissions_pending(),
        "events": events_tail(100),
    }
