from __future__ import annotations

import secrets

from .envfile import EnvFile

_TOKEN_KEYS = ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN")
_INSTRUCTION_URL = (
    "https://github.com/WhiteBite/remote-devbox-mcp/blob/main/ARENA.md"
)


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


def chat_block(env_map: dict[str, str], ingress_url: str, full: bool) -> str:
    def show(token: str) -> str:
        return token if full else mask(token)

    lines = [
        f"Работай по инструкции: {_INSTRUCTION_URL}",
        f"INGRESS={ingress_url}",
        f"BRIDGE_TOKEN={show(env_map.get('MCP_BEARER_TOKEN', ''))}",
        f"HOST_TOKEN={show(env_map.get('MCP_PUBLIC_TOKEN', ''))}",
        f"INGRESS_TOKEN={show(env_map.get('INGRESS_TOKEN', ''))}",
        "Эндпоинты и порты: GET <INGRESS>/p/9000/manifest.json (Bearer INGRESS_TOKEN)",
        "ТЗ: /workspace/ARENA_TASK.md",
    ]
    return "\n".join(lines)
