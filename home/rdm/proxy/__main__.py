"""CLI entry: `python -m rdm.proxy --mode ingress|target`."""

from __future__ import annotations

import argparse
import os
import sys

from rdm.proxy.server import serve_ingress, serve_target


def _ports(value: str) -> set[int]:
    ports: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            ports.add(int(token))
        except ValueError:
            sys.exit(f"некорректный порт: {token!r}")
    return ports


def _ranges(value: str) -> set[tuple[int, int]]:
    ranges: set[tuple[int, int]] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        lo, sep, hi = token.partition("-")
        if not sep:
            sys.exit(f"некорректный диапазон портов: {token!r}")
        try:
            lo_port, hi_port = int(lo), int(hi)
        except ValueError:
            sys.exit(f"некорректный диапазон портов: {token!r}")
        if not 1 <= lo_port <= hi_port <= 65535:
            sys.exit(f"некорректный диапазон портов: {token!r}")
        ranges.add((lo_port, hi_port))
    return ranges


def main() -> None:
    parser = argparse.ArgumentParser(prog="rdm.proxy")
    parser.add_argument("--mode", choices=("ingress", "target"), required=True)
    args = parser.parse_args()

    if args.mode == "ingress":
        token = os.environ.get("INGRESS_TOKEN")
        if not token:
            sys.exit("INGRESS_TOKEN не задан")
        port = int(os.environ.get("PROXY_PORT", "8799"))
        self_authed = _ports(os.environ.get("SELF_AUTHED_PORTS", ""))
        allowed = _ports(os.environ.get("ALLOWED_PORTS", ""))
        ranges = _ranges(os.environ.get("ALLOWED_PORT_RANGES", ""))
        denied = _ports(os.environ.get("DENIED_PORTS", ""))
        manifest = os.environ.get("RDM_MANIFEST_PATH") or None
        serve_ingress("127.0.0.1", port, token, self_authed, allowed, manifest, ranges, denied)
        return

    token = os.environ.get("MCP_PUBLIC_TOKEN")
    if not token:
        sys.exit("MCP_PUBLIC_TOKEN не задан")
    target_port = os.environ.get("TARGET_PORT")
    if not target_port:
        sys.exit("TARGET_PORT не задан")
    port = int(os.environ.get("PROXY_PORT", "8792"))
    target_host = os.environ.get("TARGET_HOST", "127.0.0.1")
    serve_target("127.0.0.1", port, token, target_host, int(target_port))


if __name__ == "__main__":
    main()