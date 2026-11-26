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


def can_connect(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _relay_url(kwargs: dict[str, str], env_map: dict[str, str]) -> str:
    try:
        logs = docker.compose("logs", "--tail", "200", "relay", **kwargs).stdout
    except OSError:
        logs = ""
    published = tunnels.from_relay_logs(logs)
    if published:
        return published
    try:
        direct = docker.run("logs", "--tail", "200", "rdm-relay").stdout
    except OSError:
        direct = ""
    published = tunnels.from_relay_logs(direct)
    if published:
        return published
    return (env_map.get("RELAY_URL") or "").strip()


_TUNNEL_DEAD_CODES = frozenset({0, 502, 503, 504, 530})


def tunnel_alive(url: str, token: str | None, timeout: float = 8.0) -> bool:
    """Любой HTTP-ответ через туннель = жив; 0/5xx — мёртвый субдомен/edge.

    Пробуем манифест-путь: ingress-proxy отвечает на него сам (даже 404),
    так что ответ доказывает живость туннеля, а не бэкенда.
    """
    code = probe_http(f"{url}/p/9000/manifest.json", token, timeout)
    return code not in _TUNNEL_DEAD_CODES


def issued_url(env_map: dict[str, str], compose_file: str | None = None) -> str:
    """URL, который реально выдан агенту; fallback — резолв по логам.

    Манифест/handoff/UI обязаны показывать этот адрес, а не свежий резолв:
    логи туннеля содержат старые анонсы и могут вернуть мёртвый субдомен,
    уже отличный от того, что ушёл в RELAY_URL.
    """
    mode = (env_map.get("INGRESS_MODE") or "").strip().lower()
    if mode == "manual":
        return (env_map.get("PUBLIC_URL") or "").strip()
    issued = (env_map.get("RELAY_URL") or "").strip()
    if issued:
        return issued
    return ingress_url(env_map, compose_file)


def ingress_url(env_map: dict[str, str], compose_file: str | None = None) -> str:
    kwargs = {"compose_file": compose_file} if compose_file else {}
    mode = (env_map.get("INGRESS_MODE") or "").strip().lower()
    if mode == "relay":
        return _relay_url(kwargs, env_map)
    if mode == "manual":
        return (env_map.get("PUBLIC_URL") or "").strip()
    if mode == "auto":
        relay = _relay_url(kwargs, env_map)
        if relay:
            return relay
        return (env_map.get("PUBLIC_URL") or "").strip()
    public = env_map.get("PUBLIC_URL")
    if public:
        return public
    public = env_map.get("PUBLIC_URL")
    if public:
        return public
    try:
        logs = docker.compose("logs", "--tail", "200", "cloudflared-ingress", **kwargs).stdout
    except OSError:
        return ""
    published = tunnels.from_logs(logs)
    if published:
        return published
    try:
        relay_logs = docker.compose("logs", "--tail", "200", "relay", **kwargs).stdout
    except OSError:
        return ""
    return tunnels.from_relay_logs(relay_logs)
