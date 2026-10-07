"""Жизненный цикл host-сервисов профиля и ingress-прокси (pid-файлы, рестарты)."""

from __future__ import annotations

import os
import pathlib
import shlex
import sys

from rdm import hostos
from rdm.freeze import spawn_entry
from rdm.ports import COCKPIT_PORT
from rdm.profiles import Profile
from rdm.ui import auth

DEFAULT_PROXY_ARGV = spawn_entry("proxy", "--mode", "target")
INGRESS_ARGV = spawn_entry("proxy", "--mode", "ingress")
INGRESS_PORT = 8799
UI_ARGV = spawn_entry("ui", "--server")
_PROXY_MARKER = "proxy --mode"
_UI_MARKER = "ui --server"
_RUNNER_MARKER = " runner" if getattr(sys, "frozen", False) else "runner-mcp.py"


def _pids_path(profile_name: str) -> pathlib.Path:
    return hostos.tempdir() / "rdm-host" / f"{profile_name}-pids.txt"


def _argv(cmd: str) -> list[str]:
    # posix=False: бэкслеши Windows-путей выживают, кавычки срезаем после сплита
    return [token.strip('"') for token in shlex.split(cmd, posix=False)]


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
    report: list[str] = []
    for svc in profile.host_services:
        argv = _argv(svc.cmd)
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
        entries.append((pid, hostos.create_time(pid), marker))
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
            entries.append((proxy_pid, hostos.create_time(proxy_pid), " ".join(proxy)))
            report.append(f"host :{svc.port} pid {pid} proxy pid {proxy_pid}")
        else:
            report.append(f"host :{svc.port} pid {pid}")
    _write_entries(_pids_path(profile_name), entries)
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
