"""CLI entry: `python -m rdm.proxy --mode ingress|target`."""

from __future__ import annotations

import argparse
import os
import sys

from rdm.proxy.server import serve_ingress, serve_target


def _ports(value: str) -> set[int]:
    return {int(part) for part in value.split(",") if part.strip()}


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
        manifest = os.environ.get("RDM_MANIFEST_PATH") or None
        serve_ingress("127.0.0.1", port, token, self_authed, allowed, manifest)
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