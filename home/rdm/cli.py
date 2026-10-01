"""devbox CLI: apply project profiles and orchestrate the host side."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

from rdm import docker, envfile, hostos, procman, profiles, ps_import, render, tokens, tunnels

HOME_DIR = pathlib.Path(__file__).resolve().parent.parent
PROJECTS_DIR = HOME_DIR.parent / "projects"
ENV_FILE = HOME_DIR / ".env"
OVERRIDE_FILE = HOME_DIR / "docker-compose.override.yml"
COMPOSE_FILE = str(HOME_DIR / "docker-compose.yml")
LOG_ROOT = hostos.tempdir() / "rdm-host"
MANIFEST_PATH = LOG_ROOT / "rdm-manifest.json"
BRIDGE_PORT = 8787
INGRESS_PORT = 8799
DEFAULT_RUNNER_PORT = 8796

_READONLY = '{"write":"deny","edit":"deny","apply_patch":"deny","bash":"deny"}'
_FULL = '{"write":"allow","edit":"allow","apply_patch":"allow","bash":"allow"}'


def _permissions(mode: str) -> str | None:
    if mode == "readonly":
        return _READONLY
    if mode == "full":
        return _FULL
    return None


def _ingress_url(env_map: dict[str, str]) -> str:
    public = env_map.get("PUBLIC_URL")
    if public:
        return public
    try:
        logs = docker.compose("logs", "cloudflared-ingress", compose_file=COMPOSE_FILE).stdout
    except OSError:
        return ""
    return tunnels.from_logs(logs)


def _preview_url(env_map: dict[str, str]) -> str:
    public = env_map.get("PUBLIC_PREVIEW_URL")
    if public:
        return public
    try:
        logs = docker.compose("logs", "cloudflared-preview", compose_file=COMPOSE_FILE).stdout
    except OSError:
        return ""
    return tunnels.from_logs(logs)


def _available_profiles() -> list[str]:
    try:
        return sorted(path.stem for path in PROJECTS_DIR.glob("*.json") if not path.stem.startswith("_"))
    except OSError:
        return []


def _runner_port(env_map: dict[str, str]) -> int | None:
    name = env_map.get("ACTIVE_PROFILE")
    path = PROJECTS_DIR / f"{name}.json" if name else None
    if not path or not path.exists():
        return None
    try:
        profile = profiles.load(path)
    except (ValueError, OSError):
        return None
    if not profile.runner_commands:
        return None
    return profile.runner_port or DEFAULT_RUNNER_PORT


def _runner_json(command: profiles.RunnerCommand) -> dict[str, object]:
    return {
        "name": command.name,
        "cmd": list(command.cmd),
        "description": command.description,
        "args": {name: {"type": spec.type, "position": spec.position} for name, spec in command.args},
        "background": command.background,
        "port": command.port,
    }


def _script_json(script: profiles.Script) -> dict[str, object]:
    return {"name": script.name, "cmd": list(script.cmd), "description": script.description}


def _with_runner(profile: profiles.Profile, name: str) -> profiles.Profile:
    if not profile.runner_commands:
        os.environ.pop("RUNNER_CONFIG", None)
        os.environ.pop("RUNNER_PORT", None)
        return profile
    port = profile.runner_port or DEFAULT_RUNNER_PORT
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
    os.environ["RUNNER_PORT"] = str(port + 1)
    service = profiles.HostService(port=port, auth="bearer", cwd=str(HOME_DIR), cmd="python host\\runner-mcp.py")
    return dataclasses.replace(profile, host_services=(*profile.host_services, service))


def _ingress_env(env_map: dict[str, str]) -> dict[str, str]:
    return {
        "INGRESS_TOKEN": env_map.get("INGRESS_TOKEN", ""),
        "PROXY_PORT": str(INGRESS_PORT),
        "SELF_AUTHED_PORTS": env_map.get("SELF_AUTHED_PORTS", ""),
        "ALLOWED_PORTS": env_map.get("ALLOWED_PORTS", ""),
        "RDM_MANIFEST_PATH": str(MANIFEST_PATH),
    }


def _emit_agent_artifacts(profile: profiles.Profile, name: str, allowed: str) -> None:
    if profile.toolchain:
        docker.run(
            "run", "--rm", "-v", "rdm-tools:/opt/tools", "alpine", "sh", "-c",
            f"mkdir -p /opt/tools/refs && echo '{profile.toolchain}' > /opt/tools/refs/{name}.list",
        )
    agents = render.render_agents_md(
        profile, name, profile.toolchain, profile.mode, allowed, _available_profiles()
    )
    docker.run(
        "run", "--rm", "-i", "-v", "rdm-agent:/agent", "alpine", "sh", "-c", "cat > /agent/AGENTS.md",
        input=agents,
    )


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
    path = PROJECTS_DIR / f"{name}.json"
    if not path.exists():
        print(f"нет профиля {path}", file=sys.stderr)
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
    profile = _with_runner(profile, name)
    env = envfile.EnvFile.load(ENV_FILE)
    active = env.get("ACTIVE_PROFILE")
    if active:
        procman.stop_host_services(active)
    env.set("PROJECT_DIR", profile.project_dir)
    env.set("TOOLCHAIN", profile.toolchain)
    env.set("GIT_NAME", profile.git_name)
    env.set("GIT_EMAIL", profile.git_email)
    if profile.preview_origin:
        env.set("PREVIEW_ORIGIN", profile.preview_origin)
    else:
        env.remove("PREVIEW_ORIGIN")
    if profile.ui_port:
        env.set("UI_PORT", str(profile.ui_port))
    else:
        env.remove("UI_PORT")
    bearer_ports = [service.port for service in profile.host_services if service.auth == "bearer"]
    self_authed = {BRIDGE_PORT, *bearer_ports}
    env.set("SELF_AUTHED_PORTS", ",".join(str(port) for port in sorted(self_authed)))
    allowed = set(profile.allowed_ports)
    allowed |= {service.port for service in profile.host_services if service.auth != "bearer"}
    allowed |= {command.port for command in profile.runner_commands if command.port}
    allowed_sorted = sorted(allowed)
    allowed_text = ",".join(str(port) for port in allowed_sorted)
    env.set("ALLOWED_PORTS", allowed_text)
    permission = _permissions(profile.mode)
    if permission:
        env.set("OPENCODE_MCP_PERMISSIONS", permission)
    else:
        env.remove("OPENCODE_MCP_PERMISSIONS")
    env.set("SETUP_SCRIPT_B64", render.setup_script_b64(profile, name))
    env.set("TUNNEL_TAIL", render.tunnel_tail(env.get("TUNNEL_TOKEN") or ""))
    env.set("ACTIVE_PROFILE", name)
    env.write(ENV_FILE)
    dir_mounts = frozenset(
        mount
        for mount in profile.deny_mounts
        if (pathlib.Path(profile.project_dir) / mount).is_dir()
    )
    OVERRIDE_FILE.write_text(render.render_override(profile, dir_mounts), encoding="utf-8")
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    env_map = env.as_map()
    MANIFEST_PATH.write_text(
        json.dumps(
            render.build_manifest(profile, name, _ingress_url(env_map), profile.mode, allowed_sorted, self_authed),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    procman.restart_host_services(profile, name, HOME_DIR, env_map.get("MCP_PUBLIC_TOKEN", ""))
    procman.stop_ingress()
    procman.start_ingress(_ingress_env(env_map), HOME_DIR, hostos.tempdir() / "rdm-ingress")
    docker.compose("up", "-d", "--force-recreate", "toolbox", compose_file=COMPOSE_FILE)
    _emit_agent_artifacts(profile, name, allowed_text)
    _start_gitleaks(profile, name)
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
    print(f"Профили:    {', '.join(_available_profiles())}")
    if not full:
        print("--- чат-блок (маскированный) ---")
    print(tokens.chat_block(env_map, _ingress_url(env_map), full, _preview_url(env_map), _runner_port(env_map)))
    return 0


def _start(name: str | None, with_preview: bool) -> int:
    if name:
        rc = apply_use(name)
        if rc:
            return rc
    else:
        docker.compose("up", "-d", compose_file=COMPOSE_FILE)
        env_map = envfile.EnvFile.load(ENV_FILE).as_map()
        procman.stop_ingress()
        procman.start_ingress(_ingress_env(env_map), HOME_DIR, hostos.tempdir() / "rdm-ingress")
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    if not env_map.get("PUBLIC_PREVIEW_URL"):
        if with_preview:
            docker.compose("--profile", "preview", "up", "-d", compose_file=COMPOSE_FILE)
        else:
            docker.compose("stop", "cloudflared-preview", compose_file=COMPOSE_FILE)
    print()
    print("=== Скопируй агенту (arena.ai и любой агентский сайт) ===")
    print(tokens.chat_block(env_map, _ingress_url(env_map), True, _preview_url(env_map), _runner_port(env_map)))
    print("=== Затем напиши задачу. ===")
    print(f"(хост) профили: {', '.join(_available_profiles())}")
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
    docker.compose("--profile", "preview", "up", "-d", "--force-recreate", "cloudflared-preview", compose_file=COMPOSE_FILE)
    url = _preview_url(env_map)
    print(f"PREVIEW={url}" if url else "preview поднимается; повтори `devbox.py preview` через пару секунд")
    return 0


def _issue_tokens() -> int:
    env = envfile.EnvFile.load(ENV_FILE)
    tokens.rotate_tokens(env)
    env.write(ENV_FILE)
    docker.compose("up", "-d", "--force-recreate", "toolbox", compose_file=COMPOSE_FILE)
    env_map = env.as_map()
    active = env_map.get("ACTIVE_PROFILE")
    if active:
        profile_path = PROJECTS_DIR / f"{active}.json"
        if profile_path.exists():
            try:
                profile = _with_runner(profiles.load(profile_path), active)
            except (ValueError, OSError) as error:
                print(f"profile error: {error}", file=sys.stderr)
            else:
                procman.restart_host_services(profile, active, HOME_DIR, env_map.get("MCP_PUBLIC_TOKEN", ""))
    procman.stop_ingress()
    procman.start_ingress(_ingress_env(env_map), HOME_DIR, hostos.tempdir() / "rdm-ingress")
    print(tokens.chat_block(env_map, _ingress_url(env_map), True, _preview_url(env_map), _runner_port(env_map)))
    return 0


def _profile(action: str, target: str) -> int:
    if action == "show":
        path = PROJECTS_DIR / f"{target}.json"
        if not path.exists():
            print(f"нет профиля {path}", file=sys.stderr)
            return 1
        sys.stdout.write(path.read_text(encoding="utf-8"))
        return 0
    source = PROJECTS_DIR / f"{target}.ps1"
    if not source.exists():
        print(f"нет файла {source}", file=sys.stderr)
        return 1
    data = ps_import.parse_profile_ps1(source.read_text(encoding="utf-8"))
    destination = PROJECTS_DIR / f"{target}.json"
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{source} -> {destination}")
    return 0


def _ingress(action: str) -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    if action == "start":
        procman.start_ingress(_ingress_env(env_map), HOME_DIR, hostos.tempdir() / "rdm-ingress")
    else:
        procman.stop_ingress()
    return 0


def _block(masked: bool) -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    print(tokens.chat_block(env_map, _ingress_url(env_map), not masked, _preview_url(env_map), _runner_port(env_map)))
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


def _probe_http(url: str, token: str, timeout: float = 8.0) -> int:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, OSError):
        return 0


def _health() -> int:
    env_map = envfile.EnvFile.load(ENV_FILE).as_map()
    if "healthy" not in docker.compose_ps(COMPOSE_FILE):
        return 1
    url = _ingress_url(env_map)
    if not url:
        return 1
    ok = _probe_http(f"{url}/p/{BRIDGE_PORT}/healthz", env_map.get("MCP_BEARER_TOKEN", "")) == 200
    return 0 if ok else 1


def _allow(port: int, ui: bool) -> int:
    env = envfile.EnvFile.load(ENV_FILE)
    active = env.get("ACTIVE_PROFILE")
    if not active:
        print("нет активного профиля", file=sys.stderr)
        return 1
    path = PROJECTS_DIR / f"{active}.json"
    if not path.exists():
        print(f"нет профиля {path}", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = {int(p) for p in (data.get("allowed_ports") or [])}
    allowed.add(port)
    data["allowed_ports"] = sorted(allowed)
    if ui:
        data["ui_port"] = port
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    profile = profiles.load(path)
    bearer_ports = [service.port for service in profile.host_services if service.auth == "bearer"]
    self_authed = {BRIDGE_PORT, *bearer_ports}
    allowed_all = set(profile.allowed_ports)
    allowed_all |= {service.port for service in profile.host_services if service.auth != "bearer"}
    allowed_all |= {command.port for command in profile.runner_commands if command.port}
    env.set("SELF_AUTHED_PORTS", ",".join(str(p) for p in sorted(self_authed)))
    env.set("ALLOWED_PORTS", ",".join(str(p) for p in sorted(allowed_all)))
    if ui:
        env.set("UI_PORT", str(port))
    env.write(ENV_FILE)
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    env_map = env.as_map()
    MANIFEST_PATH.write_text(
        json.dumps(
            render.build_manifest(profile, active, _ingress_url(env_map), profile.mode, sorted(allowed_all), self_authed),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    procman.stop_ingress()
    procman.start_ingress(_ingress_env(env_map), HOME_DIR, hostos.tempdir() / "rdm-ingress")
    print(f"{active}: порт {port} открыт" + (" как UI" if ui else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
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
    profile.add_argument("action", choices=("import", "convert", "show"))
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
        return _profile(args.action, args.target)
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
    return _status()