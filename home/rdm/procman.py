"""Жизненный цикл host-сервисов профиля и ingress-прокси (pid-файлы, рестарты)."""

from __future__ import annotations

import os
import pathlib
import shlex
import shutil
import sys
import time

from rdm import hostos
from rdm.freeze import spawn_entry
from rdm.ports import COCKPIT_PORT, DEFAULT_GATEWAY_PORT
from rdm.profiles import HostService, Profile
from rdm.ui import auth

DEFAULT_PROXY_ARGV = spawn_entry("proxy", "--mode", "target")
INGRESS_ARGV = spawn_entry("proxy", "--mode", "ingress")
INGRESS_PORT = 8799
UI_ARGV = spawn_entry("ui", "--server")
WATCHDOG_ARGV = spawn_entry("watch")
GATEWAY_ARGV = spawn_entry("gateway")
_PROXY_MARKER = "proxy --mode"
_UI_MARKER = "ui --server"
_WATCHDOG_MARKER = " watch"
_GATEWAY_MARKER = " gateway"
_RUNNER_MARKER = " runner" if getattr(sys, "frozen", False) else "runner-mcp.py"
_CHILD_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOME",
    "JAVA_HOME",
    "FLUTTER_ROOT",
)


def _pids_path(profile_name: str) -> pathlib.Path:
    return hostos.tempdir() / "rdm-host" / f"{profile_name}-pids.txt"


def _argv(cmd: str) -> list[str]:
    # posix=False: бэкслеши Windows-путей выживают, кавычки срезаем после сплита
    return [token.strip('"') for token in shlex.split(cmd, posix=False)]


def _service_argv(svc: HostService) -> list[str]:
    """Run a service that declares deps in its own cached env via uv.

    Keeps a tool pinned to a different dependency major (e.g. mcp<2) off the
    shared interpreter, so an app-side bump cannot break it.
    """
    argv = _argv(svc.cmd)
    if not (svc.deps or svc.requirements):
        return argv
    uv = shutil.which("uv")
    if uv is None:
        raise OSError("uv not found on PATH (host_service declares deps)")
    wrapped = [uv, "run", "--no-project"]
    if svc.python:
        wrapped += ["--python", svc.python]
    for dep in svc.deps:
        wrapped += ["--with", dep]
    if svc.requirements:
        wrapped += ["--with-requirements", svc.requirements]
    return [*wrapped, *argv]


_START_GRACE_SECONDS = 1.5
_TAIL_LINES = 6


def _stderr_tail(path: pathlib.Path, lines: int = _TAIL_LINES) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " | ".join(text.splitlines()[-lines:])


def _startup_failures(spawned: list[tuple[int, float | None, str, pathlib.Path]]) -> list[str]:
    """Report services that died right after spawn, with their stderr, instead of skipping silently."""
    if not spawned:
        return []
    time.sleep(_START_GRACE_SECONDS)
    problems: list[str] = []
    for pid, created, marker, err_path in spawned:
        if _owned(pid, created, marker):
            continue
        tail = _stderr_tail(err_path)
        problems.append(f"FAILED (exited): {marker[:80]}" + (f" - {tail[:400]}" if tail else ""))
    return problems


def _write_entries(path: pathlib.Path, entries: list[tuple[int, float | None, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{pid}|{ct if ct is not None else ''}|{marker}" for pid, ct, marker in entries]
    text = "\n".join(lines) + "\n" if lines else ""
    path.write_text(text, encoding="utf-8")


def _read_entries(path: pathlib.Path) -> list[tuple[int, float | None, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    entries: list[tuple[int, float | None, str]] = []
    for line in text.splitlines():
        fields = line.split("|", 2)
        if len(fields) != 3 or not fields[0].isdigit():
            continue
        try:
            ct = float(fields[1]) if fields[1] else None
        except ValueError:
            continue
        entries.append((int(fields[0]), ct, fields[2]))
    return entries


def _owned(pid: int, recorded: float | None, marker: str) -> bool:
    return hostos.owned(pid, recorded, marker)


def _free_port(port: int) -> None:
    """Снять с порта осиротевший наш процесс: иначе новый не забиндится и умрёт молча."""
    pid = hostos.find_pid_by_port(port)
    if pid is None:
        return
    if (
        hostos.cmdline_matches(pid, _RUNNER_MARKER)
        or hostos.cmdline_matches(pid, _PROXY_MARKER)
        or hostos.cmdline_matches(pid, _UI_MARKER)
        or hostos.cmdline_matches(pid, _GATEWAY_MARKER)
    ):
        hostos.kill_tree(pid)


def stop_host_services(profile_name: str) -> None:
    path = _pids_path(profile_name)
    for pid, recorded, marker in _read_entries(path):
        if _owned(pid, recorded, marker):
            hostos.kill_tree(pid)
    path.unlink(missing_ok=True)


def host_services_dead(profile_name: str) -> bool:
    path = _pids_path(profile_name)
    for pid, recorded, marker in _read_entries(path):
        if not _owned(pid, recorded, marker):
            return True
    return False


def host_services_recorded(profile_name: str) -> bool:
    return bool(_read_entries(_pids_path(profile_name)))


def restart_host_services(
    profile: Profile,
    profile_name: str,
    host_dir: pathlib.Path,
    public_token: str,
    proxy_argv: list[str] | None = None,
) -> list[str]:
    stop_host_services(profile_name)
    log_dir = _pids_path(profile_name).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[int, float | None, str]] = []
    spawned: list[tuple[int, float | None, str, pathlib.Path]] = []
    report: list[str] = []
    for svc in profile.host_services:
        try:
            argv = _service_argv(svc)
        except OSError as error:
            report.append(f"host :{svc.port} FAILED: {error}")
            continue
        marker = " ".join(argv)
        bearer = svc.auth == "bearer"
        for busy_port in ([svc.port, svc.port + 1] if bearer else [svc.port]):
            _free_port(busy_port)
        env = {**os.environ, "MCP_HTTP_PORT": str(svc.port + 1)} if bearer else None
        try:
            pid = hostos.spawn(
                argv,
                cwd=svc.cwd or None,
                stdout_path=log_dir / f"{profile_name}-svc{svc.port}.out",
                stderr_path=log_dir / f"{profile_name}-svc{svc.port}.err",
                env=env,
            )
        except OSError as error:
            report.append(f"host :{svc.port} FAILED: {error}")
            continue
        created = hostos.create_time(pid)
        entries.append((pid, created, marker))
        spawned.append((pid, created, marker, log_dir / f"{profile_name}-svc{svc.port}.err"))
        if bearer:
            proxy = proxy_argv if proxy_argv is not None else DEFAULT_PROXY_ARGV
            proxy_env = {
                **os.environ,
                "MCP_PUBLIC_TOKEN": public_token,
                "PROXY_PORT": str(svc.port),
                "TARGET_PORT": str(svc.port + 1),
                "TARGET_HOST": "127.0.0.1",
            }
            try:
                proxy_pid = hostos.spawn(
                    proxy,
                    cwd=host_dir,
                    stdout_path=log_dir / f"{profile_name}-pxy{svc.port}.out",
                    stderr_path=log_dir / f"{profile_name}-pxy{svc.port}.err",
                    env=proxy_env,
                )
            except OSError as error:
                report.append(f"host :{svc.port} pid {pid} proxy FAILED: {error}")
                continue
            proxy_created = hostos.create_time(proxy_pid)
            entries.append((proxy_pid, proxy_created, " ".join(proxy)))
            spawned.append((proxy_pid, proxy_created, " ".join(proxy), log_dir / f"{profile_name}-pxy{svc.port}.err"))
            report.append(f"host :{svc.port} pid {pid} proxy pid {proxy_pid}")
        else:
            report.append(f"host :{svc.port} pid {pid}")
    _write_entries(_pids_path(profile_name), entries)
    report.extend(_startup_failures(spawned))
    return report


def start_ingress(env_map: dict[str, str], host_dir: pathlib.Path) -> int:
    stop_ingress()
    target_dir = hostos.tempdir() / "rdm-ingress"
    target_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **env_map}
    if not env.get("PROXY_PORT"):
        env["PROXY_PORT"] = str(INGRESS_PORT)
    pid = hostos.spawn(
        INGRESS_ARGV,
        cwd=host_dir,
        stdout_path=target_dir / "ingress.out",
        stderr_path=target_dir / "ingress.err",
        env=env,
    )
    _write_entries(target_dir / "pids.txt", [(pid, hostos.create_time(pid), _PROXY_MARKER)])
    return pid


def stop_ingress() -> None:
    path = hostos.tempdir() / "rdm-ingress" / "pids.txt"
    for pid, recorded, _ in _read_entries(path):
        if _owned(pid, recorded, _PROXY_MARKER):
            hostos.kill_tree(pid)
    path.unlink(missing_ok=True)


def start_ui(env_map: dict[str, str], home_dir: pathlib.Path) -> int:
    stop_ui()
    _free_port(COCKPIT_PORT)
    state_dir = auth.ui_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **env_map}
    if not env.get("RDM_UI_PORT"):
        env["RDM_UI_PORT"] = str(COCKPIT_PORT)
    pid = hostos.spawn(
        UI_ARGV,
        cwd=home_dir,
        stdout_path=state_dir / "ui.out",
        stderr_path=state_dir / "ui.err",
        env=env,
    )
    _write_entries(state_dir / "pids.txt", [(pid, hostos.create_time(pid), _UI_MARKER)])
    return pid


def stop_ui() -> None:
    path = auth.ui_state_dir() / "pids.txt"
    for pid, recorded, _ in _read_entries(path):
        if _owned(pid, recorded, _UI_MARKER):
            hostos.kill_tree(pid)
    path.unlink(missing_ok=True)


def action_lock_path() -> pathlib.Path:
    return hostos.tempdir() / "rdm-host" / "action.lock"


def watchdog_pids_path() -> pathlib.Path:
    return hostos.tempdir() / "rdm-watchdog" / "pids.txt"


def start_watchdog(env_map: dict[str, str], home_dir: pathlib.Path) -> int:
    stop_watchdog()
    state_dir = watchdog_pids_path().parent
    state_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **env_map}
    pid = hostos.spawn(
        WATCHDOG_ARGV,
        cwd=home_dir,
        stdout_path=state_dir / "watchdog.out",
        stderr_path=state_dir / "watchdog.err",
        env=env,
    )
    _write_entries(watchdog_pids_path(), [(pid, hostos.create_time(pid), _WATCHDOG_MARKER)])
    return pid


def stop_watchdog() -> None:
    path = watchdog_pids_path()
    for pid, recorded, _ in _read_entries(path):
        if _owned(pid, recorded, _WATCHDOG_MARKER):
            hostos.kill_tree(pid)
    path.unlink(missing_ok=True)


def gateway_pids_path() -> pathlib.Path:
    return hostos.tempdir() / "rdm-gateway" / "pids.txt"


def start_gateway(profile: Profile, gateway_token: str, host_dir: pathlib.Path) -> int:
    stop_gateway()
    gateway_port = profile.gateway_port or DEFAULT_GATEWAY_PORT
    _free_port(gateway_port)
    _free_port(gateway_port + 1)
    state_dir = gateway_pids_path().parent
    state_dir.mkdir(parents=True, exist_ok=True)
    child_env = {key: os.environ[key] for key in _CHILD_ENV_KEYS if os.environ.get(key)}
    child_env["MCP_HTTP_PORT"] = str(gateway_port + 1)
    child_env["GATEWAY_PORT"] = str(gateway_port)
    pid = hostos.spawn(
        GATEWAY_ARGV,
        cwd=host_dir,
        stdout_path=state_dir / "gateway.out",
        stderr_path=state_dir / "gateway.err",
        env=child_env,
    )
    proxy_env = {
        **os.environ,
        "MCP_PUBLIC_TOKEN": gateway_token,
        "PROXY_PORT": str(gateway_port),
        "TARGET_PORT": str(gateway_port + 1),
        "TARGET_HOST": "127.0.0.1",
    }
    proxy_pid = hostos.spawn(
        DEFAULT_PROXY_ARGV,
        cwd=host_dir,
        stdout_path=state_dir / "proxy.out",
        stderr_path=state_dir / "proxy.err",
        env=proxy_env,
    )
    _write_entries(
        gateway_pids_path(),
        [
            (pid, hostos.create_time(pid), _GATEWAY_MARKER),
            (proxy_pid, hostos.create_time(proxy_pid), _GATEWAY_MARKER),
        ],
    )
    return pid


def stop_gateway() -> None:
    path = gateway_pids_path()
    for pid, recorded, _ in _read_entries(path):
        if _owned(pid, recorded, _GATEWAY_MARKER):
            hostos.kill_tree(pid)
    path.unlink(missing_ok=True)
