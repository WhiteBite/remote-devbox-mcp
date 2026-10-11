"""Tunnel and host-service watchdog loop."""

from __future__ import annotations

import datetime
import os
import pathlib
import time

from rdm import docker, hostos, netprobe, procman, profiles
from rdm.freeze import app_dir

_HOME = app_dir()
_DEFAULT_COMPOSE = str(_HOME / "docker-compose.yml")
START_DELAY = 15.0
RECREATE_DELAY = 30.0


def _log(path: pathlib.Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{datetime.datetime.now().isoformat()} {message}\n")


def _acquire_single_instance(log_path: pathlib.Path) -> bool:
    path = procman.watchdog_pids_path()
    for pid, recorded, marker in procman._read_entries(path):
        # venv-спавн: pidfile держит pid лончера, реальный процесс — его ребёнок
        if pid not in (os.getpid(), os.getppid()) and hostos.owned(pid, recorded, marker):
            _log(log_path, f"watchdog already running (pid {pid})")
            print(f"watchdog уже запущен (pid {pid}); выходим")
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    procman._write_entries(
        path, [(os.getpid(), hostos.create_time(os.getpid()), procman._WATCHDOG_MARKER)]
    )
    return True


def _release_single_instance() -> None:
    path = procman.watchdog_pids_path()
    entries = procman._read_entries(path)
    if entries and all(pid == os.getpid() for pid, _, _ in entries):
        path.unlink(missing_ok=True)


def run(
    env_map: dict[str, str],
    compose_file: str | None = None,
    interval: float = 15.0,
    iterations: int | None = None,
    prober=None,
) -> None:
    compose_file = compose_file or _DEFAULT_COMPOSE
    probe = prober or netprobe.probe_http
    log_path = hostos.tempdir() / "rdm-watchdog" / "watchdog.log"
    if not _acquire_single_instance(log_path):
        return
    try:
        active = env_map.get("ACTIVE_PROFILE", "")
        fails = 0
        count = 0
        if iterations is None:
            time.sleep(START_DELAY)
        while iterations is None or count < iterations:
            count += 1
            try:
                if not netprobe.can_connect(procman.INGRESS_PORT):
                    _log(log_path, "ingress proxy dead: restart")
                    procman.stop_ingress()
                    from rdm import cli as _cli

                    # .env перечитываем: issue-tokens ротирует INGRESS_TOKEN уже после старта watch
                    fresh = _cli.envfile.EnvFile.load(_cli.ENV_FILE).as_map()
                    procman.start_ingress(_cli._ingress_env(fresh), _HOME)
                    _cli._refresh_manifest_from_env()
                    fails = 0
                url = netprobe.ingress_url(env_map, compose_file)
                code = probe(f"{url}/p/{profiles.BRIDGE_PORT}/healthz", env_map.get("MCP_BEARER_TOKEN")) if url else 0
                fails = fails + 1 if code != 200 else 0
                if fails >= 3:
                    _log(log_path, "tunnel flap: recreate ingress")
                    docker.compose("up", "-d", "--force-recreate", "cloudflared-ingress", compose_file=compose_file)
                    from rdm import cli as _cli

                    _cli._refresh_manifest_from_env()
                    fails = 0
                    if iterations is None:
                        time.sleep(RECREATE_DELAY)
                if active and procman.host_services_dead(active):
                    _log(log_path, "host services dead: restart")
                    profile_path = profiles.find(active)
                    if profile_path is not None:
                        from rdm import cli as _cli

                        profile = _cli._with_runner(profiles.load(profile_path), active)
                        procman.restart_host_services(profile, active, _HOME, env_map.get("MCP_PUBLIC_TOKEN", ""))
                ps = docker.compose_ps(compose_file)
                if "unhealthy" in ps:
                    _log(log_path, "toolbox unhealthy: recreate")
                    docker.compose("up", "-d", "--force-recreate", "toolbox", compose_file=compose_file)
                elif "toolbox" not in ps:
                    _log(log_path, "toolbox отсутствует: up -d")
                    docker.compose("up", "-d", "toolbox", compose_file=compose_file)
            except (OSError, ValueError) as error:
                _log(log_path, f"watchdog error: {error}")
            if iterations is None:
                time.sleep(interval)
    finally:
        _release_single_instance()