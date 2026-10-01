"""Tunnel and host-service watchdog loop."""

from __future__ import annotations

import datetime
import pathlib
import time

from rdm import docker, hostos, procman, profiles
from rdm.doctor import _HOME, _ingress_url, _probe

_DEFAULT_COMPOSE = str(_HOME / "docker-compose.yml")
_BRIDGE_PORT = 8787
_PROJECTS = _HOME.parent / "projects"


def _host_services_dead(active: str) -> bool:
    path = hostos.tempdir() / "rdm-host" / f"{active}-pids.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        fields = line.split("|", 2)
        if not fields or not fields[0].isdigit():
            continue
        marker = fields[2] if len(fields) > 2 else ""
        if not hostos.cmdline_matches(int(fields[0]), marker):
            return True
    return False


def _log(path: pathlib.Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{datetime.datetime.now().isoformat()} {message}\n")


def run(
    env_map: dict[str, str],
    compose_file: str | None = None,
    interval: float = 15.0,
    iterations: int | None = None,
    prober=None,
) -> None:
    compose_file = compose_file or _DEFAULT_COMPOSE
    probe = prober or _probe
    log_path = hostos.tempdir() / "rdm-watchdog" / "watchdog.log"
    active = env_map.get("ACTIVE_PROFILE", "")
    fails = 0
    count = 0
    while iterations is None or count < iterations:
        count += 1
        try:
            url = _ingress_url(env_map)
            code = probe(f"{url}/p/{_BRIDGE_PORT}/healthz", env_map.get("MCP_BEARER_TOKEN")) if url else 0
            fails = fails + 1 if code != 200 else 0
            if fails >= 3:
                _log(log_path, "tunnel flap: recreate ingress")
                docker.compose("up", "-d", "--force-recreate", "cloudflared-ingress", compose_file=compose_file)
                fails = 0
            if active and _host_services_dead(active):
                _log(log_path, "host services dead: restart")
                profile_path = _PROJECTS / f"{active}.json"
                if profile_path.exists():
                    from rdm import cli as _cli

                    profile = _cli._with_runner(profiles.load(profile_path), active)
                    procman.restart_host_services(profile, active, _HOME, env_map.get("MCP_PUBLIC_TOKEN", ""))
            if "healthy" not in docker.compose_ps(compose_file):
                _log(log_path, "toolbox unhealthy: up -d")
                docker.compose("up", "-d", "toolbox", compose_file=compose_file)
        except (OSError, ValueError) as error:
            _log(log_path, f"watchdog error: {error}")
        if iterations is None:
            time.sleep(interval)