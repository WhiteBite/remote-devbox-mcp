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
UNHEALTHY_RUNS_BEFORE_RECREATE = 3
TOOLBOX_RECREATE_COOLDOWN_SECONDS = 120.0
TUNNEL_RECREATE_COOLDOWN_SECONDS = 300.0


def _probe_url(env_map: dict[str, str], compose_file: str) -> str:
    """URL, чьё здоровье гарантирует watchdog.

    В relay/auto агенту выдали адрес на старте (RELAY_URL): если relay сел на
    случайный поддомен, фактический URL из логов жив, а выданный — 503. Пробовать
    надо выданный, иначе потерю субдомена не видно. В manual туннель поднят
    пользователем — пробуем PUBLIC_URL.
    """
    mode = (env_map.get("INGRESS_MODE") or "").strip().lower()
    if mode == "manual":
        return (env_map.get("PUBLIC_URL") or "").strip()
    if mode in ("relay", "auto"):
        published = (env_map.get("RELAY_URL") or "").strip()
        if published:
            return published
        sub = (env_map.get("RELAY_SUBDOMAIN") or "").strip()
        if sub:
            return f"https://{sub}.loca.lt"
    return netprobe.ingress_url(env_map, compose_file)


def _tunnel_service(env_map: dict[str, str], url: str) -> str:
    """Контейнер туннеля, который имеет смысл пересоздать; "" — туннель не наш."""
    mode = (env_map.get("INGRESS_MODE") or "").strip().lower()
    if mode == "manual":
        return ""
    if mode in ("relay", "auto"):
        return "relay" if url.endswith(".loca.lt") else ""
    if mode == "cloudflare":
        return "cloudflared-ingress"
    if url.endswith(".loca.lt"):
        return "relay"
    return "cloudflared-ingress"


def _host_services_stale(name: str) -> bool:
    if procman.host_services_dead(name):
        return True
    if procman.host_services_recorded(name):
        return False
    path = profiles.find(name)
    if path is None:
        return False
    from rdm import cli as _cli

    return bool(_cli._with_runner(profiles.load(path), name).host_services)


def _cli_action_in_flight() -> bool:
    try:
        with hostos.action_lock(procman.action_lock_path(), timeout=0.0):
            return False
    except hostos.ActionLockError:
        return True


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
        toolbox_state = ""
        unhealthy_runs = 0
        last_toolbox_recreate = 0.0
        last_tunnel_recreate = -TUNNEL_RECREATE_COOLDOWN_SECONDS
        if iterations is None:
            time.sleep(START_DELAY)
        while iterations is None or count < iterations:
            count += 1
            try:
                if not netprobe.can_connect(procman.INGRESS_PORT):
                    if _cli_action_in_flight():
                        _log(log_path, "ingress proxy dead: cli action in flight, skip restart")
                    else:
                        _log(log_path, "ingress proxy dead: restart")
                        procman.stop_ingress()
                        from rdm import cli as _cli

                        # .env перечитываем: issue-tokens ротирует INGRESS_TOKEN уже после старта watch
                        fresh = _cli.envfile.EnvFile.load(_cli.ENV_FILE).as_map()
                        procman.start_ingress(_cli._ingress_env(fresh), _HOME)
                        _cli._refresh_manifest_from_env()
                        fails = 0
                url = _probe_url(env_map, compose_file)
                code = probe(f"{url}/p/{profiles.BRIDGE_PORT}/healthz", env_map.get("MCP_BEARER_TOKEN")) if url else 0
                fails = fails + 1 if code != 200 else 0
                if fails >= 3 and time.monotonic() - last_tunnel_recreate > TUNNEL_RECREATE_COOLDOWN_SECONDS:
                    service = _tunnel_service(env_map, url)
                    last_tunnel_recreate = time.monotonic()
                    fails = 0
                    if service:
                        _log(log_path, f"tunnel flap: recreate {service}")
                        profile_flag = "relay" if service == "relay" else "cloudflare"
                        docker.compose(
                            "--profile", profile_flag, "up", "-d", "--force-recreate", service,
                            compose_file=compose_file,
                        )
                        from rdm import cli as _cli

                        # _await ждёт свежий URL в логах и перезаписывает RELAY_URL
                        _cli._await_ingress_url(env_map)
                        _cli._refresh_manifest_from_env()
                        # env_map — снимок на старт: перечитываем, чтобы следующая
                        # проба шла по уже опубликованному (возможно новому) адресу
                        env_map.update(_cli.envfile.EnvFile.load(_cli.ENV_FILE).as_map())
                        if iterations is None:
                            time.sleep(RECREATE_DELAY)
                    else:
                        _log(log_path, f"tunnel probe fails: {url or 'no url'} — туннель внешний, пересоздавать нечего")
                if active and _host_services_stale(active):
                    if _cli_action_in_flight():
                        _log(log_path, "host services dead: cli action in flight, skip restart")
                    else:
                        _log(log_path, "host services dead: restart")
                        profile_path = profiles.find(active)
                        if profile_path is not None:
                            from rdm import cli as _cli

                            profile = _cli._with_runner(profiles.load(profile_path), active)
                            procman.restart_host_services(profile, active, _HOME, env_map.get("MCP_PUBLIC_TOKEN", ""))
                            fresh = _cli.envfile.EnvFile.load(_cli.ENV_FILE).as_map()
                            if profile.exposed_mcps and fresh.get("GATEWAY_TOKEN"):
                                procman.start_gateway(profile, fresh["GATEWAY_TOKEN"], _HOME)
                            else:
                                procman.stop_gateway()
                ps = docker.compose_ps(compose_file)
                toolbox_state_now = (
                    "unhealthy" if "unhealthy" in ps else ("missing" if "toolbox" not in ps else "ok")
                )
                if toolbox_state_now != toolbox_state:
                    _log(log_path, f"toolbox state: {toolbox_state_now}")
                    toolbox_state = toolbox_state_now
                if toolbox_state_now == "unhealthy":
                    unhealthy_runs += 1
                    cooled = time.monotonic() - last_toolbox_recreate > TOOLBOX_RECREATE_COOLDOWN_SECONDS
                    if unhealthy_runs >= UNHEALTHY_RUNS_BEFORE_RECREATE and cooled:
                        _log(log_path, "toolbox unhealthy: recreate")
                        docker.compose("up", "-d", "--force-recreate", "toolbox", compose_file=compose_file)
                        unhealthy_runs = 0
                        last_toolbox_recreate = time.monotonic()
                elif toolbox_state_now == "missing":
                    docker.compose("up", "-d", "toolbox", compose_file=compose_file)
                else:
                    unhealthy_runs = 0
            except (OSError, ValueError) as error:
                _log(log_path, f"watchdog error: {error}")
            if iterations is None:
                time.sleep(interval)
    finally:
        _release_single_instance()