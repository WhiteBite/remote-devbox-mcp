"""Сетевые пробы (HTTP/loopback) и ingress-URL — общие для cli/doctor/watchdog.

Считать их копиями в модулях выше запрещено: таймауты и поведение при обрыве
уже расходились.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

from rdm import docker, tunnels


def probe_http(url: str, token: str | None, timeout: float = 10.0) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, OSError):
        return 0


def can_connect(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def ingress_url(env_map: dict[str, str], compose_file: str | None = None) -> str:
    public = env_map.get("PUBLIC_URL")
    if public:
        return public
    kwargs = {"compose_file": compose_file} if compose_file else {}
    try:
        logs = docker.compose("logs", "--tail", "200", "cloudflared-ingress", **kwargs).stdout
    except OSError:
        return ""
    return tunnels.from_logs(logs)
