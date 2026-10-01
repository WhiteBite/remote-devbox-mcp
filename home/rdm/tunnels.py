"""Извлечение публичного URL быстрого туннеля из логов cloudflared."""

from __future__ import annotations

import re

_RESERVED = r"(?!api\.|www\.|update\.|developers\.|blog\.|cloudflared\.)"
_URL = re.compile(rf"https://{_RESERVED}[a-z0-9][a-z0-9-]*\.trycloudflare\.com")


def from_logs(logs: str | None) -> str:
    text = logs or ""
    for line in text.splitlines():
        if "Visit it at" in line or "quick Tunnel" in line:
            match = _URL.search(line)
            if match:
                return match.group(0)
    matches = _URL.findall(text)
    return matches[-1] if matches else ""