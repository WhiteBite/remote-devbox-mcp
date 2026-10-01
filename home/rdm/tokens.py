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
    profiles: list[str] | None = None,
) -> str:
    def show(token: str) -> str:
        return token if full else mask(token)

    lines = [
        f"Репозиторий: {_REPO_URL}",
        f"Загрузи скилл: {_SKILL_URL}",
        f"Работай по инструкции: {_INSTRUCTION_URL}",
        f"INGRESS={ingress_url}",
        f"BRIDGE_TOKEN={show(env_map.get('MCP_BEARER_TOKEN', ''))}",
        f"HOST_TOKEN={show(env_map.get('MCP_PUBLIC_TOKEN', ''))}",
        f"INGRESS_TOKEN={show(env_map.get('INGRESS_TOKEN', ''))}",
    ]
    if preview_url:
        lines.append(f"PREVIEW={preview_url}   # UI для playwright (root URL, без токена)")
    lines += [
        "Эндпоинты и порты: GET <INGRESS>/p/9000/manifest.json (Bearer INGRESS_TOKEN)",
        "ТЗ: /workspace/ARENA_TASK.md",
    ]
    if profiles:
        lines.append(f"Профили: {', '.join(profiles)}   # смена проекта: devbox.py start <имя>")
    return "\n".join(lines)
