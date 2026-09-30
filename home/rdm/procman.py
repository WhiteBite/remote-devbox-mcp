"""Жизненный цикл host-сервисов профиля и ingress-прокси (pid-файлы, рестарты)."""

from __future__ import annotations

import os
import pathlib
import shlex
import sys

from rdm import hostos
from rdm.profiles import Profile

DEFAULT_PROXY_ARGV = [sys.executable, "-m", "rdm.proxy", "--mode", "target"]
INGRESS_ARGV = [sys.executable, "-m", "rdm.proxy", "--mode", "ingress"]
INGRESS_PORT = 8799
_PROXY_MARKER = "rdm.proxy"


def _pids_path(profile_name: str) -> pathlib.Path:
    return hostos.tempdir() / "rdm-host" / f"{profile_name}-pids.txt"


def _ingress_dir(state_dir: pathlib.Path | None) -> pathlib.Path:
    if state_dir is not None:
        return pathlib.Path(state_dir)
    return hostos.tempdir() / "rdm-ingress"


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
    if hostos.cmdline_matches(pid, marker):
        return True
    return recorded is not None and hostos.create_time(pid) == recorded


def stop_host_services(profile_name: str) -> None:
    path = _pids_path(profile_name)
    for pid, recorded, marker in _read_entries(path):
        if _owned(pid, recorded, marker):
            hostos.kill_tree(pid)
    path.unlink(missing_ok=True)


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
        env = {**os.environ, "MCP_HTTP_PORT": str(svc.port + 1)} if bearer else None
        pid = hostos.spawn(
            argv,
            cwd=svc.cwd or None,
            stdout_path=log_dir / f"{profile_name}-svc{svc.port}.out",
            stderr_path=log_dir / f"{profile_name}-svc{svc.port}.err",
            env=env,
        )
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
            proxy_pid = hostos.spawn(
                proxy,
                cwd=host_dir,
                stdout_path=log_dir / f"{profile_name}-pxy{svc.port}.out",
                stderr_path=log_dir / f"{profile_name}-pxy{svc.port}.err",
                env=proxy_env,
            )
            entries.append((proxy_pid, hostos.create_time(proxy_pid), " ".join(proxy)))
            report.append(f"host :{svc.port} pid {pid} proxy pid {proxy_pid}")
        else:
            report.append(f"host :{svc.port} pid {pid}")
    _write_entries(_pids_path(profile_name), entries)
    return report


def start_ingress(
    env_map: dict[str, str],
    host_dir: pathlib.Path,
    state_dir: pathlib.Path | None = None,
) -> int:
    target_dir = _ingress_dir(state_dir)
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
    path = _ingress_dir(None) / "pids.txt"
    for pid, _, _ in _read_entries(path):
        if hostos.cmdline_matches(pid, _PROXY_MARKER):
            hostos.kill_tree(pid)
    path.unlink(missing_ok=True)
