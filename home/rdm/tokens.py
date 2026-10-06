from __future__ import annotations

import secrets

from .envfile import EnvFile

_TOKEN_KEYS = ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN")
_TOKEN_SURFACES = (
    "Токены и поверхности:",
    "BRIDGE_TOKEN  -> /p/8787/mcp (bridge: read/edit/write/bash)",
    "HOST_TOKEN    -> runner + host-сервисы со своей авторизацией (/p/<порт>/mcp)",
    "INGRESS_TOKEN -> все HTTP-эндпоинты /p/<порт>, включая /p/9000/manifest.json",
)
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
    runner_port: int | None = None,
    self_authed_ports: tuple[int, ...] = (),
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
        *_TOKEN_SURFACES,
    ]
    if preview_url:
        lines.append(f"PREVIEW={preview_url}  # UI для playwright (root URL, без токена)")
    ui_port = env_map.get("UI_PORT")
    if ui_port and ingress_url:
        lines.append(f"UI={ingress_url}/p/{ui_port}/  # открой своим Playwright (Bearer INGRESS_TOKEN)")
    if ingress_url and (runner_port or self_authed_ports):
        if runner_port:
            lines.append(f"RUNNER_URL={ingress_url}/p/{runner_port}/mcp")
        host = show(env_map.get("MCP_PUBLIC_TOKEN", ""))
        lines.append(
            f"HOST_TOKEN={host}  # MCP_TOKEN: runner (run_<имя>) + host-сервисы со своей авторизацией /p/<порт>/mcp"
        )
    if ingress_url:
        lines.append(f"Реестр эндпоинтов: GET {ingress_url}/p/9000/manifest.json (Bearer INGRESS_TOKEN)")
    lines.append(
        "Песочница сбрасывается — конфиги ~/.remote-devbox-mcp.conf и ~/.mcp-runner.conf"
        " держи в Workspace и восстанавливай на старте сессии"
    )
    return "\n".join(lines)
