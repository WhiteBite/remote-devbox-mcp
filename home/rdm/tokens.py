from __future__ import annotations

import secrets

from .envfile import EnvFile

_TOKEN_KEYS = ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN")
_REPO_URL = "https://github.com/WhiteBite/remote-devbox-mcp"
_INSTRUCTION_URL = f"{_REPO_URL}/blob/main/ARENA.md"
_SKILL_URL = f"{_REPO_URL}/blob/main/skills/remote-devbox/SKILL.md"


def generate_token() -> str:
    return secrets.token_hex(32)


def rotate_tokens(env: EnvFile) -> dict[str, str]:
    new = {key: generate_token() for key in _TOKEN_KEYS}
    for key, value in new.items():
        env.set(key, value)
    return new


def mask(token: str) -> str:
    if len(token) < 8:
        return "..."
    return f"{token[:4]}...{token[-4:]}"


def chat_block(
    env_map: dict[str, str],
    ingress_url: str,
    full: bool,
    preview_url: str = "",
    runner: bool = False,
) -> str:
    def show(token: str) -> str:
        return token if full else mask(token)

    bridge = show(env_map.get("MCP_BEARER_TOKEN", ""))
    ingress_token = show(env_map.get("INGRESS_TOKEN", ""))
    lines = [
        f"Репозиторий: {_REPO_URL}",
        f"Скилл + инструкция: {_SKILL_URL} , {_INSTRUCTION_URL}",
        f"INGRESS={ingress_url}",
        f"BRIDGE_TOKEN={bridge}  # /p/8787/mcp — код: read/edit/write/bash",
        f"INGRESS_TOKEN={ingress_token}  # UI и прочие порты (Authorization: Bearer)",
    ]
    if preview_url:
        lines.append(f"PREVIEW={preview_url}  # UI для playwright (root URL, без токена)")
    ui_port = env_map.get("UI_PORT")
    if ui_port and ingress_url:
        lines.append(f"UI={ingress_url}/p/{ui_port}/  # открой своим Playwright (Bearer INGRESS_TOKEN)")
    if runner:
        host = show(env_map.get("MCP_PUBLIC_TOKEN", ""))
        lines.append(f"HOST_TOKEN={host}  # runner: MCP_CONF=~/.mcp-runner.conf ./mcp call run_<имя>")
    if ingress_url:
        lines.append("Реестр эндпоинтов: GET <INGRESS>/p/9000/manifest.json (Bearer INGRESS_TOKEN)")
    return "\n".join(lines)
