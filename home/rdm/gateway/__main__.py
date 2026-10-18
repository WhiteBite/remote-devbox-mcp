"""MCP gateway entry: /healthz stub on MCP_HTTP_PORT."""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rdm import freeze, ports


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        body = b"ok\n" if path == "/healthz" else b""
        self.send_response(200 if path == "/healthz" else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    if freeze.FROZEN:
        sys.exit("gateway not in frozen build (P2)")
    port = int(os.environ.get("MCP_HTTP_PORT", str(ports.DEFAULT_GATEWAY_PORT + 1)))
    ThreadingHTTPServer(("127.0.0.1", port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
