"""Детерминированные генераторы артефактов профиля: override-compose, setup-скрипт, manifest, AGENTS.md, TUNNEL_TAIL."""

from __future__ import annotations

import base64
import hashlib

from rdm.profiles import Profile

_BRIDGE_PORT = 8787


def render_override(profile: Profile) -> str:
    lines = [
        "services:",
        "  toolbox:",
        "    volumes:",
        "      - ./docker/workspace-empty:/workspace/.opencode:ro",
    ]
    lines.extend(f"      - /dev/null:/workspace/{mount}:ro" for mount in profile.deny_mounts)
    return "\n".join(lines) + "\n"


def build_setup_script(profile: Profile, profile_name: str) -> str:
    parts = [f"# generated: devbox.py use {profile_name}\n"]
    for index, setup in enumerate(profile.setup_cmds, 1):
        digest = hashlib.md5(f"{setup.cmd}{setup.marker}{profile_name}".encode("utf-8")).hexdigest()
        marker = f"/opt/tools/.setup-{index}-{digest}"
        on_fail = "exit 1" if setup.required else f"echo '[setup] WARN: cmd {index} failed, continue'"
        parts.append(f"if [ ! -f {marker} ]; then\n  {setup.cmd} || {on_fail}\n  touch {marker}\nfi\n")
    return "".join(parts)


def setup_script_b64(profile: Profile, profile_name: str) -> str:
    return base64.b64encode(build_setup_script(profile, profile_name).encode("utf-8")).decode("ascii")


def build_manifest(
    profile: Profile, profile_name: str, ingress_url: str, mode: str
) -> dict[str, object]:
    endpoints: list[dict[str, object]] = [
        {"name": "bridge", "port": _BRIDGE_PORT, "auth": "bearer", "path": f"/p/{_BRIDGE_PORT}/mcp"}
    ]
    for service in profile.host_services:
        endpoints.append(
            {"name": f"host-{service.port}", "port": service.port, "auth": "bearer", "path": f"/p/{service.port}/mcp"}
        )
    for port in profile.allowed_ports:
        endpoints.append({"name": f"allowed-{port}", "port": port, "auth": "ingress", "path": f"/p/{port}"})
    return {
        "profile": profile_name,
        "project": profile.project_dir,
        "ingress_url": ingress_url,
        "endpoints": endpoints,
        "allowed_ports": list(profile.allowed_ports),
        "runner_commands": [command.name for command in profile.runner_commands],
        "scripts": [script.name for script in profile.scripts],
        "preview_origin": profile.preview_origin,
        "mode": mode,
        "host_requirements": {
            "memory_mb": 4096,
            "storage_mb": 20480,
            "note": "toolchains in /opt/tools volume",
        },
    }


def render_agents_md(
    profile: Profile, profile_name: str, toolchain: str, mode: str, allowed_ports: str
) -> str:
    runner_lines = [
        f"- run_{command.name.replace(':', '_').replace('-', '_')}: {command.description}"
        for command in profile.runner_commands
    ]
    script_lines = [f"- run_script_{script.name}: {script.description}" for script in profile.scripts]
    return "\n".join(
        [
            f"<!-- auto-generated: devbox.py use {profile_name} -->",
            "## Environment",
            f"- profile: {profile_name}; project: {profile.project_dir}",
            f"- toolchain: {toolchain}",
            f"- mode: {mode}; allowed ports: {allowed_ports}",
            "## Runner commands",
            "\n".join(runner_lines) or "- (нет)",
            "## Scripts",
            "\n".join(script_lines) or "- (нет)",
            "",
        ]
    )


def tunnel_tail(tunnel_token: str) -> str:
    if tunnel_token:
        return f"run --token {tunnel_token}"
    return "--protocol http2 --url http://host.docker.internal:8799"


def toolchain_ref(profile: Profile) -> str:
    return profile.toolchain
