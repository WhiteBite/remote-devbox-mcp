"""Детерминированные генераторы артефактов профиля: env-поля, override-compose, setup-скрипт, manifest, AGENTS.md, TUNNEL_TAIL."""

from __future__ import annotations

import base64
import hashlib

from rdm import ports
from rdm.profiles import BRIDGE_PORT, Profile, runner_tool_name

_PERMISSIONS_JSON = {
    "readonly": '{"write":"deny","edit":"deny","apply_patch":"deny","bash":"deny","webfetch":"deny"}',
    "full": '{"write":"allow","edit":"allow","apply_patch":"allow","bash":"allow","webfetch":"allow"}',
}

_BRIDGE_TOOLS: list[dict[str, object]] = [
    {"name": "read", "mutating": False},
    {"name": "write", "mutating": True},
    {"name": "edit", "mutating": True},
    {"name": "apply_patch", "mutating": True},
    {"name": "glob", "mutating": False},
    {"name": "grep", "mutating": False},
    {"name": "bash", "mutating": True},
    {"name": "lsp", "mutating": False},
    {"name": "todowrite", "mutating": False},
    {"name": "opencode_permission_reply", "mutating": False},
    {"name": "opencode_job_result", "mutating": False},
]


def env_fields(profile: Profile, name: str, tunnel_token: str) -> dict[str, str | None]:
    """Env-поля профиля одним вызовом; None означает «удалить ключ» из .env."""
    policy = ports.compute_port_policy(profile)
    return {
        "PROJECT_DIR": profile.project_dir,
        "TOOLCHAIN": profile.toolchain,
        "GIT_NAME": profile.git_name,
        "GIT_EMAIL": profile.git_email,
        "PREVIEW_ORIGIN": profile.preview_origin or None,
        "UI_PORT": str(profile.ui_port) if profile.ui_port else None,
        "SELF_AUTHED_PORTS": ",".join(str(port) for port in policy.self_authed),
        "ALLOWED_PORTS": ",".join(str(port) for port in policy.allowed),
        "OPENCODE_MCP_PERMISSIONS": _PERMISSIONS_JSON.get(profile.mode),
        "SETUP_SCRIPT_B64": setup_script_b64(profile, name),
        "TUNNEL_TAIL": tunnel_tail(tunnel_token),
        "ACTIVE_PROFILE": name,
    }


def render_override(profile: Profile, dir_mounts: frozenset[str] | set[str] = frozenset()) -> str:
    lines = [
        "services:",
        "  toolbox:",
        "    volumes:",
        "      - ./docker/workspace-empty:/workspace/.opencode:ro",
    ]
    lines.extend(
        f"      - /dev/null:/workspace/{mount}:ro"
        for mount in profile.deny_mounts
        if mount not in dir_mounts
    )
    if dir_mounts:
        lines.append("    tmpfs:")
        lines.extend(f"      - /workspace/{mount}" for mount in sorted(dir_mounts))
    return "\n".join(lines) + "\n"


def build_setup_script(profile: Profile, profile_name: str) -> str:
    parts = [f"# generated: devbox.py use {profile_name}\n"]
    for index, setup in enumerate(profile.setup_cmds, 1):
        digest = hashlib.md5(f"{setup.cmd}{setup.marker}{profile_name}".encode()).hexdigest()
        marker = f"/opt/tools/.setup-{index}-{digest}"
        on_fail = "exit 1" if setup.required else f"echo '[setup] WARN: cmd {index} failed, continue'"
        parts.append(f"if [ ! -f {marker} ]; then\n  {setup.cmd} || {on_fail}\n  touch {marker}\nfi\n")
    return "".join(parts)


def setup_script_b64(profile: Profile, profile_name: str) -> str:
    return base64.b64encode(build_setup_script(profile, profile_name).encode("utf-8")).decode("ascii")


def build_manifest(
    profile: Profile,
    profile_name: str,
    ingress_url: str,
    mode: str,
    allowed_ports: list[int] | None = None,
    self_authed: frozenset[int] | set[int] = frozenset(),
) -> dict[str, object]:
    ports = list(profile.allowed_ports) if allowed_ports is None else list(allowed_ports)
    endpoints: list[dict[str, object]] = [
        {
            "name": "manifest",
            "port": 9000,
            "auth": "ingress",
            "token": "INGRESS_TOKEN",
            "header": "Authorization: Bearer",
            "path": "/p/9000/manifest.json",
        },
        {
            "name": "bridge",
            "port": BRIDGE_PORT,
            "auth": "bearer",
            "token": "BRIDGE_TOKEN",
            "header": "Authorization: Bearer",
            "path": f"/p/{BRIDGE_PORT}/mcp",
        },
    ]
    for service in profile.host_services:
        if service.port in self_authed:
            auth = "self"
        elif service.auth == "bearer":
            auth = "bearer"
        else:
            auth = "ingress"
        endpoints.append(
            {
                "name": f"host-{service.port}",
                "port": service.port,
                "auth": auth,
                "token": "HOST_TOKEN" if auth in ("self", "bearer") else "INGRESS_TOKEN",
                "header": "Authorization: Bearer",
                "path": f"/p/{service.port}/mcp",
            }
        )
    for port in ports:
        endpoints.append(
            {
                "name": f"allowed-{port}",
                "port": port,
                "auth": "ingress",
                "token": "INGRESS_TOKEN",
                "header": "Authorization: Bearer",
                "path": f"/p/{port}",
            }
        )
    return {
        "profile": profile_name,
        "project": profile.project_dir,
        "ingress_url": ingress_url,
        "endpoints": endpoints,
        "allowed_ports": ports,
        "runner_commands": [
            {
                "name": command.name,
                "tool": runner_tool_name(command.name),
                "description": command.description,
                "kill_tool": f"{runner_tool_name(command.name)}_kill" if command.background else None,
            }
            for command in profile.runner_commands
        ],
        "scripts": [
            {
                "name": script.name,
                "tool": runner_tool_name(f"script:{script.name}"),
                "description": script.description,
                "kill_tool": None,
            }
            for script in profile.scripts
        ],
        "bridge_tools": _BRIDGE_TOOLS,
        "preview_origin": profile.preview_origin,
        "mode": mode,
        "host_requirements": {
            "memory_mb": 4096,
            "storage_mb": 20480,
            "note": "toolchains in /opt/tools volume",
        },
    }


def render_agents_md(
    profile: Profile,
    profile_name: str,
    toolchain: str,
    mode: str,
    allowed_ports: str,
    available_profiles: list[str] | None = None,
) -> str:
    runner_lines = [
        f"- {runner_tool_name(command.name)}: {command.description}"
        for command in profile.runner_commands
    ]
    script_lines = [
        f"- {runner_tool_name(f'script:{script.name}')}: {script.description}"
        for script in profile.scripts
    ]
    parts = [
        f"<!-- auto-generated: devbox.py use {profile_name} -->",
        "## Environment",
        f"- profile: {profile_name}; project: {profile.project_dir}",
        f"- toolchain: {toolchain}",
        f"- mode: {mode}; allowed ports: {allowed_ports}",
        "## Runner commands",
        "\n".join(runner_lines) or "- (нет)",
        "## Scripts",
        "\n".join(script_lines) or "- (нет)",
        "## Back-pointers",
        "- repo: https://github.com/WhiteBite/remote-devbox-mcp",
        "- skill: skills/remote-devbox/SKILL.md",
        "- manifest: /p/9000/manifest.json",
        "- если читаешь копию из /agent и read её не берёт: `bash cat /agent/AGENTS.md`",
    ]
    if available_profiles is not None:
        parts += ["## Projects", ", ".join(available_profiles) or "- (нет)"]
    parts.append("")
    return "\n".join(parts)


def tunnel_tail(tunnel_token: str) -> str:
    if tunnel_token:
        return f"run --token {tunnel_token}"
    return "--protocol http2 --url http://host.docker.internal:8799"
