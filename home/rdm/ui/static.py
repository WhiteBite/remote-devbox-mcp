"""Cockpit static assets: ui-static/ files resolved with a containment check."""

from __future__ import annotations

import urllib.parse

from rdm.ui import auth

_MIME: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def resolve(url_path: str) -> tuple[bytes, str] | None:
    root = auth.static_dir().resolve()
    rel = "index.html" if url_path in ("", "/") else urllib.parse.unquote(url_path).lstrip("/")
    candidate = (root / rel).resolve()
    if root not in candidate.parents:
        return None
    try:
        data = candidate.read_bytes()
    except (OSError, ValueError):
        return None
    return data, _MIME.get(candidate.suffix.lower(), "application/octet-stream")
