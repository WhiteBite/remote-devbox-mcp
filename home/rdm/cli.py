"""devbox CLI: apply project profiles and orchestrate the host side."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
import sys
import time
import webbrowser

from rdm import docker, envfile, freeze, hostos, netprobe, ports, procman, profiles, render, tokens, tunnels
from rdm.ui import auth

HOME_DIR = freeze.app_dir()
ENV_FILE = HOME_DIR / ".env"
OVERRIDE_FILE = HOME_DIR / "docker-compose.override.yml"
COMPOSE_FILE = str(HOME_DIR / "docker-compose.yml")
LOG_ROOT = hostos.tempdir() / "rdm-host"
MANIFEST_PATH = LOG_ROOT / "rdm-manifest.json"
BRIDGE_PORT = ports.BRIDGE_PORT
INGRESS_PORT = 8799
_NAME_RE = re.compile(r"[A-Za-z0-9._\-]+")
_COCKPIT_WAIT_SECONDS = 5.0


def _ingress_url(env_map: dict[str, str]) -> str:
    return netprobe.ingress_url(env_map, COMPOSE_FILE)


def _preview_url(env_map: dict[str, str]) -> str:
    public = env_map.get("PUBLIC_PREVIEW_URL")
    if public:
        return public
    try:
        logs = docker.compose("logs", "--tail", "200", "cloudflared-preview", compose_file=COMPOSE_FILE).stdout
    except OSError:
        return ""
    return tunnels.from_logs(logs)


def _runner_port(env_map: dict[str, str]) -> int | None:
    name = env_map.get("ACTIVE_PROFILE")
    path = profiles.find(name) if name else None
    if path is None:
        return None
    try:
        profile = profiles.load(path)
    except (ValueError, OSError):
        return None
    return ports.compute_port_policy(profile).runner_port


def _self_authed_ports(env_map: dict[str, str]) -> tuple[int, ...]:
    name = env_map.get("ACTIVE_PROFILE")
    path = profiles.find(name) if name else None
    if path is None:
        return ()
    try:
        profile = profiles.load(path)
    except (ValueError, OSError):
        return ()
    return tuple(port for port in ports.compute_port_policy(profile).self_authed if port != BRIDGE_PORT)


def _runner_json(command: profiles.RunnerCommand) -> dict[str, object]:
    return {
        "name": command.name,
        "cmd": list(command.cmd),
        "description": command.description,
        "args": {name: {"type": spec.type, "position": spec.position} for name, spec in command.args},
        "background": command.background,
        "port": command.port,
        "timeout": command.timeout,
    }


def _script_json(script: profiles.Script) -> dict[str, object]:
    return {"name": script.name, "cmd": list(script.cmd), "description": script.description}


def _with_runner(profile: profiles.Profile, name: str) -> profiles.Profile:
    if not profile.runner_commands:
        os.environ.pop("RUNNER_CONFIG", None)
        return profile
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    config = LOG_ROOT / f"runner-{name}.json"
    config.write_text(
        json.dumps(
            {
                "profile": name,
                "cwd": profile.project_dir,
                "commands": [_runner_json(command) for command in profile.runner_commands],
                "scripts": [_script_json(script) for script in profile.scripts],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.environ["RUNNER_CONFIG"] = str(config)
    return ports.with_runner_service(profile)


def _ingress_env(env_map: dict[str, str]) -> dict[str, str]:
    return {
        "INGRESS_TOKEN": env_map.get("INGRESS_TOKEN", ""),
        "PROXY_PORT": str(INGRESS_PORT),
        "SELF_AUTHED_PORTS": env_map.get("SELF_AUTHED_PORTS", ""),
        "ALLOWED_PORTS": env_map.get("ALLOWED_PORTS", ""),
        "ALLOWED_PORT_RANGES": env_map.get("ALLOWED_PORT_RANGES", ""),
        "DENIED_PORTS": env_map.get("DENIED_PORTS", ""),
        "RDM_MANIFEST_PATH": str(MANIFEST_PATH),
    }


def _write_manifest(profile: profiles.Profile, name: str, env_map: dict[str, str]) -> None:
    manifest_profile = ports.with_runner_service(profile)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(
            render.build_manifest(manifest_profile, name, _ingress_url(env_map), manifest_profile.mode),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _refresh_manifest_from_env() -> None:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    name = env_map.get("ACTIVE_PROFILE")
    path = profiles.find(name) if name else None
    if path is None:
        return
    try:
        profile = profiles.load(path)
    except (ValueError, OSError):
        return
    _write_manifest(profile, name, env_map)


def _emit_agent_artifacts(profile: profiles.Profile, name: str, allowed: str) -> bool:
    if profile.toolchain:
        result = docker.run(
            "run", "--rm", "-i", "-v", "rdm-tools:/opt/tools", "alpine", "sh", "-c",
            f"mkdir -p /opt/tools/refs && cat > /opt/tools/refs/{name}.list",
            input=profile.toolchain + "\n",
        )
        if result.returncode != 0:
            print(f"refs тулчейнов не записаны (rc={result.returncode}): {result.stderr.strip()[:200]}", file=sys.stderr)
            return False
    agents = render.render_agents_md(
        profile, name, profile.toolchain, profile.mode, allowed, profiles.available()
    )
    result = docker.run(
        "run", "--rm", "-i", "-v", "rdm-agent:/agent", "alpine", "sh", "-c", "cat > /agent/AGENTS.md",
        input=agents,
    )
    if result.returncode != 0:
        print(f"/agent/AGENTS.md не записан (rc={result.returncode}): {result.stderr.strip()[:200]}", file=sys.stderr)
        return False
    workspace = profile.project_dir.replace("\\", "/")
    result = docker.run(
        "run", "--rm", "-i", "-v", f"{workspace}:/workspace", "alpine", "sh", "-c",
        "mkdir -p /workspace/.devbox && cat > /workspace/.devbox/AGENTS.md",
        input=agents,
    )
    if result.returncode != 0:
        print(f"/workspace/.devbox/AGENTS.md не записан (rc={result.returncode}): {result.stderr.strip()[:200]}", file=sys.stderr)
    return True


def _start_gitleaks(profile: profiles.Profile, name: str) -> None:
    if os.environ.get("SKIP_GITLEAKS"):
        return
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    source = profile.project_dir.replace("\\", "/")
    config = str(HOME_DIR / "docker" / "gitleaks.toml").replace("\\", "/")
    out = str(LOG_ROOT).replace("\\", "/")
    argv = [
        "docker", "run", "--rm",
        "-v", f"{source}:/src:ro",
        "-v", f"{config}:/cfg.toml:ro",
        "-v", f"{out}:/out",
        "zricethezav/gitleaks", "detect", "--source", "/src", "--no-git",
        "--config", "/cfg.toml", "--report-format", "json",
        "--report-path", f"/out/gitleaks-{name}.json", "--exit-code", "0",
    ]
    try:
        hostos.spawn(
            argv,
            stdout_path=LOG_ROOT / f"gitleaks-{name}.out",
            stderr_path=LOG_ROOT / f"gitleaks-{name}.err",
        )
    except OSError:
        pass


def apply_use(name: str) -> int:
    path = profiles.find(name)
    if path is None:
        print(f"нет профиля {name}", file=sys.stderr)
        return 1
    if not _NAME_RE.fullmatch(name):
        print(f"имя профиля {name!r}: допустимы только [A-Za-z0-9._-]", file=sys.stderr)
        return 1
    try:
        profile = profiles.load(path)
    except (ValueError, OSError) as error:
        print(f"profile error: {error}", file=sys.stderr)
        return 1
    problems = profiles.validate(profile)
    for problem in problems:
        if problem.startswith("WARN"):
            print(f"profile warn: {problem[5:]}")
    errors = [problem for problem in problems if not problem.startswith("WARN")]
    if errors:
        for error in errors:
            print(f"profile error: {error}", file=sys.stderr)
        return 1
    base_profile = profile
    profile = _with_runner(profile, name)
    env = envfile.EnvFile.load(ENV_FILE)
    active = env.get("ACTIVE_PROFILE")
    if active:
        procman.stop_host_services(active)
    for key, value in render.env_fields(profile, name, env.get("TUNNEL_TOKEN") or "").items():
        if value is None:
            env.remove(key)
        else:
            env.set(key, value)
    policy = ports.compute_port_policy(profile)
    allowed_text = ",".join(str(port) for port in policy.allowed)
    env.write(ENV_FILE)
    dir_mounts = frozenset(
        mount
        for mount in profile.deny_mounts
        if (pathlib.Path(profile.project_dir) / mount).is_dir()
    )
    OVERRIDE_FILE.write_text(render.render_override(profile, dir_mounts), encoding="utf-8")
    env_map = env.as_map()
    _write_manifest(base_profile, name, env_map)
    procman.restart_host_services(profile, name, HOME_DIR, env_map.get("MCP_PUBLIC_TOKEN", ""))
    procman.stop_ingress()
    procman.start_ingress(_ingress_env(env_map), HOME_DIR)
    up = docker.compose("up", "-d", "--force-recreate", "toolbox", compose_file=COMPOSE_FILE)
    if up.returncode != 0:
        print(f"docker compose up не удался (rc={up.returncode}): {up.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    if not _emit_agent_artifacts(profile, name, allowed_text):
        return 1
    _start_gitleaks(profile, name)
    print(f"профиль {name} ← {path.parent}")
    print(f"профиль {name} применён; тулчейны ставятся при старте toolbox")
    return 0


def _status() -> int:
    result = docker.compose("ps", "--format", "{{.Name}} {{.Status}}", compose_file=COMPOSE_FILE)
    sys.stdout.write(result.stdout)
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    active = env_map.get("ACTIVE_PROFILE", "")
    print(f"active profile: {active}")
    pids = hostos.tempdir() / "rdm-host" / f"{active}-pids.txt"
    if pids.exists():
        for line in pids.read_text(encoding="utf-8").splitlines():
            print(f"host pid: {line}")
    return 0


def _stop_host() -> int:
    active = envfile.EnvFile.load(ENV_FILE).get("ACTIVE_PROFILE")
    if active:
        procman.stop_host_services(active)
    else:
        print("ACTIVE_PROFILE не задан")
    return 0


def _doctor() -> int:
    from rdm import doctor

    return doctor.run(envfile.EnvFile.load(ENV_FILE).as_map(), compose_file=COMPOSE_FILE)


def _watch() -> int:
    from rdm import watchdog

    watchdog.run(envfile.EnvFile.load(ENV_FILE).as_map(), compose_file=COMPOSE_FILE)
    return 0


def _chat(full: bool) -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    print(f"Профиль:    {env_map.get('ACTIVE_PROFILE', '')}")
    print(f"Проект:     {env_map.get('PROJECT_DIR', '')}")
    print(f"Тулчейны:   {env_map.get('TOOLCHAIN', '')}")
    print(f"Git:        {env_map.get('GIT_NAME', '')} <{env_map.get('GIT_EMAIL', '')}>")
    print(f"INGRESS:    {_ingress_url(env_map)}")
    print(f"Порты:      self-authed {env_map.get('SELF_AUTHED_PORTS', '')}; allowed {env_map.get('ALLOWED_PORTS', '')}")
    print(f"Preview:    {env_map.get('PREVIEW_ORIGIN', '')}")
    print(f"Профили:    {', '.join(profiles.available())}")
    if not full:
        print("--- чат-блок (маскированный) ---")
    _refresh_manifest_from_env()
    print(
        tokens.chat_block(
            env_map, _ingress_url(env_map), full, _preview_url(env_map),
            _runner_port(env_map), _self_authed_ports(env_map),
        )
    )
    return 0


def _start(name: str | None, with_preview: bool) -> int:
    if name:
        rc = apply_use(name)
        if rc:
            return rc
    else:
        up = docker.compose("up", "-d", compose_file=COMPOSE_FILE)
        if up.returncode != 0:
            print(f"docker compose up не удался (rc={up.returncode}): {up.stderr.strip()[:200]}", file=sys.stderr)
            return 1
        env_map = envfile.EnvFile.load(ENV_FILE).as_map()
        procman.stop_ingress()
        procman.start_ingress(_ingress_env(env_map), HOME_DIR)
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    if not env_map.get("PUBLIC_PREVIEW_URL") and with_preview:
        docker.compose("--profile", "preview", "up", "-d", compose_file=COMPOSE_FILE)
    _refresh_manifest_from_env()
    print()
    print("=== Скопируй агенту (arena.ai и любой агентский сайт) ===")
    print(
        tokens.chat_block(
            env_map, _ingress_url(env_map), True, _preview_url(env_map),
            _runner_port(env_map), _self_authed_ports(env_map),
        )
    )
    print("=== Затем напиши задачу. ===")
    print(f"(хост) профили: {', '.join(profiles.available())}")
    return 0


def _preview(origin: str | None) -> int:
    env = envfile.EnvFile.load(ENV_FILE)
    if origin:
        env.set("PREVIEW_ORIGIN", origin)
        env.write(ENV_FILE)
    env_map = env.as_map()
    if not env_map.get("PREVIEW_ORIGIN"):
        print("укажи origin: devbox.py preview http://host.docker.internal:<port>", file=sys.stderr)
        return 1
    up = docker.compose("--profile", "preview", "up", "-d", "--force-recreate", "cloudflared-preview", compose_file=COMPOSE_FILE)
    if up.returncode != 0:
        print(f"preview-туннель не поднялся (rc={up.returncode}): {up.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    url = _preview_url(env_map)
    print(f"PREVIEW={url}" if url else "preview поднимается; повтори `devbox.py preview` через пару секунд")
    return 0


def _issue_tokens() -> int:
    env = envfile.EnvFile.load(ENV_FILE)
    tokens.rotate_tokens(env)
    env.write(ENV_FILE)
    up = docker.compose("up", "-d", "--force-recreate", "toolbox", compose_file=COMPOSE_FILE)
    if up.returncode != 0:
        print(f"docker compose up не удался (rc={up.returncode}): {up.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    env_map = env.as_map()
    active = env_map.get("ACTIVE_PROFILE")
    if active:
        profile_path = profiles.find(active)
        if profile_path is not None:
            try:
                profile = _with_runner(profiles.load(profile_path), active)
            except (ValueError, OSError) as error:
                print(f"profile error: {error}", file=sys.stderr)
            else:
                procman.restart_host_services(profile, active, HOME_DIR, env_map.get("MCP_PUBLIC_TOKEN", ""))
    procman.stop_ingress()
    procman.start_ingress(_ingress_env(env_map), HOME_DIR)
    _refresh_manifest_from_env()
    print(
        tokens.chat_block(
            env_map, _ingress_url(env_map), True, _preview_url(env_map),
            _runner_port(env_map), _self_authed_ports(env_map),
        )
    )
    return 0


def _profile(target: str) -> int:
    path = profiles.find(target)
    if path is None:
        print(f"нет профиля {target}", file=sys.stderr)
        return 1
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


def _ingress(action: str) -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    if action == "start":
        procman.start_ingress(_ingress_env(env_map), HOME_DIR)
    else:
        procman.stop_ingress()
    return 0


def _cockpit() -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    procman.start_ui(env_map, HOME_DIR)
    deadline = time.monotonic() + _COCKPIT_WAIT_SECONDS
    while not netprobe.can_connect(ports.COCKPIT_PORT):
        if time.monotonic() >= deadline:
            print(f"cockpit не поднялся на 127.0.0.1:{ports.COCKPIT_PORT}", file=sys.stderr)
            return 1
        time.sleep(0.2)
    token = auth.write_bootstrap()
    url = f"http://127.0.0.1:{ports.COCKPIT_PORT}/?t={token}"
    print(url)
    webbrowser.open(url)
    return 0


def _block(masked: bool) -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    print(
        tokens.chat_block(
            env_map, _ingress_url(env_map), not masked, _preview_url(env_map),
            _runner_port(env_map), _self_authed_ports(env_map),
        )
    )
    return 0


def _url(preview: bool) -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    print(_preview_url(env_map) if preview else _ingress_url(env_map))
    return 0


def _down() -> int:
    active = envfile.EnvFile.load(ENV_FILE).get("ACTIVE_PROFILE")
    if active:
        procman.stop_host_services(active)
    procman.stop_ingress()
    docker.compose("stop", "cloudflared-ingress", "cloudflared-preview", compose_file=COMPOSE_FILE)
    print("host-сервисы и ingress остановлены; туннели остановлены (стек оставлен)")
    return 0


def _health() -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    if "healthy" not in docker.compose_ps(COMPOSE_FILE):
        return 1
    url = _ingress_url(env_map)
    if not url:
        return 1
    ok = netprobe.probe_http(f"{url}/p/{BRIDGE_PORT}/healthz", env_map.get("MCP_BEARER_TOKEN", ""), timeout=8.0) == 200
    return 0 if ok else 1


def _allow(port: int, ui: bool) -> int:
    if port == ports.COCKPIT_PORT:
        print(f"порт {port} — cockpit; открывать его наружу нельзя", file=sys.stderr)
        return 1
    env = envfile.EnvFile.load(ENV_FILE)
    active = env.get("ACTIVE_PROFILE")
    if not active:
        print("нет активного профиля", file=sys.stderr)
        return 1
    path = profiles.find(active)
    if path is None:
        print(f"нет профиля {active}", file=sys.stderr)
        return 1
    if port < 1 or port > 65535:
        print(f"порт {port} вне 1-65535", file=sys.stderr)
        return 1
    try:
        profile = profiles.load(path)
    except (ValueError, OSError) as error:
        print(f"profile error: {error}", file=sys.stderr)
        return 1
    policy = ports.compute_port_policy(profile)
    if port == BRIDGE_PORT:
        print(f"порт {port} — порт моста; открывать его наружу нельзя", file=sys.stderr)
        return 1
    if policy.runner_http_port == port:
        print(f"порт {port} — HTTP раннера без авторизации; открывать его наружу нельзя", file=sys.stderr)
        return 1
    if port in policy.self_authed:
        print(f"порт {port} уже в SELF_AUTHED_PORTS (сервис со своей авторизацией)", file=sys.stderr)
        return 1
    if any(service.port == port for service in profile.host_services if service.auth != "bearer"):
        print(f"порт {port} — host-сервис без своей авторизации; открывать его наружу нельзя", file=sys.stderr)
        return 1
    if port in policy.denied:
        print(f"порт {port} в port_deny; сначала убери его оттуда", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = set(profile.allowed_ports)
    allowed.add(port)
    data["allowed_ports"] = sorted(allowed)
    if ui:
        data["ui_port"] = port
    mutated = dataclasses.replace(
        profile,
        allowed_ports=tuple(sorted(allowed)),
        ui_port=port if ui else profile.ui_port,
    )
    problems = [problem for problem in profiles.validate(mutated) if not problem.startswith("WARN")]
    if problems:
        for problem in problems:
            print(f"profile error: {problem}", file=sys.stderr)
        return 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    policy = ports.compute_port_policy(mutated)
    env.set("SELF_AUTHED_PORTS", ",".join(str(p) for p in policy.self_authed))
    env.set("ALLOWED_PORTS", ",".join(str(p) for p in policy.allowed))
    env.set("ALLOWED_PORT_RANGES", ",".join(f"{lo}-{hi}" for lo, hi in policy.ranges))
    env.set("DENIED_PORTS", ",".join(str(p) for p in policy.denied))
    if ui:
        env.set("UI_PORT", str(port))
    env.write(ENV_FILE)
    env_map = env.as_map()
    _write_manifest(mutated, active, env_map)
    procman.stop_ingress()
    procman.start_ingress(_ingress_env(env_map), HOME_DIR)
    print(f"{active}: порт {port} открыт" + (" как UI" if ui else ""))
    return 0


def _embedded(argv: list[str]) -> int:
    if argv[0] == "proxy":
        sys.argv = ["rdm.proxy", *argv[1:]]
        from rdm.proxy.__main__ import main as proxy_main

        proxy_main()
        return 0
    if argv[0] == "ui":
        sys.argv = ["rdm.ui", *argv[1:]]
        from rdm.ui.__main__ import main as ui_main

        ui_main()
        return 0
    from rdm.runner.__main__ import main as runner_main

    runner_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows-консоль на CI — cp1252; кириллица в профилях/блоке валит print
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = sys.argv[1:] if argv is None else list(argv)
    # скрытые подкоманды: spawn_entry запускает proxy/runner/ui через этот же CLI
    if argv and argv[0] in ("proxy", "runner", "ui"):
        return _embedded(argv)
    parser = argparse.ArgumentParser(prog="devbox")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status")
    start = sub.add_parser("start")
    start.add_argument("name", nargs="?", default=None)
    start.add_argument("--preview", action="store_true")
    preview = sub.add_parser("preview")
    preview.add_argument("origin", nargs="?", default=None)
    use = sub.add_parser("use")
    use.add_argument("name")
    sub.add_parser("stop-host")
    sub.add_parser("doctor")
    sub.add_parser("watch")
    sub.add_parser("info")
    sub.add_parser("share")
    sub.add_parser("issue-tokens")
    profile = sub.add_parser("profile")
    profile.add_argument("action", choices=("show",))
    profile.add_argument("target")
    ingress = sub.add_parser("ingress")
    ingress.add_argument("action", choices=("start", "stop"))
    block = sub.add_parser("block")
    block.add_argument("--masked", action="store_true")
    url = sub.add_parser("url")
    url.add_argument("--preview", action="store_true")
    sub.add_parser("down")
    sub.add_parser("health")
    allow = sub.add_parser("allow")
    allow.add_argument("port", type=int)
    allow.add_argument("--ui", action="store_true")
    sub.add_parser("cockpit")
    args = parser.parse_args(argv)

    if args.command == "use":
        return apply_use(args.name)
    if args.command == "start":
        return _start(args.name, args.preview)
    if args.command == "preview":
        return _preview(args.origin)
    if args.command == "stop-host":
        return _stop_host()
    if args.command == "doctor":
        return _doctor()
    if args.command == "watch":
        return _watch()
    if args.command == "info":
        return _chat(False)
    if args.command == "share":
        return _chat(True)
    if args.command == "issue-tokens":
        return _issue_tokens()
    if args.command == "profile":
        return _profile(args.target)
    if args.command == "ingress":
        return _ingress(args.action)
    if args.command == "block":
        return _block(args.masked)
    if args.command == "url":
        return _url(args.preview)
    if args.command == "down":
        return _down()
    if args.command == "health":
        return _health()
    if args.command == "allow":
        return _allow(args.port, args.ui)
    if args.command == "cockpit":
        return _cockpit()
    return _status()