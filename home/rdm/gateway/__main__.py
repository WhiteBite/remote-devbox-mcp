"""MCP gateway entry: streamable-HTTP server on MCP_HTTP_PORT."""

from __future__ import annotations

import os
import signal
import sys
import threading

from rdm import freeze, ports
from rdm.gateway import server


def _manager_factory() -> server.SessionManager:
    from rdm.gateway import sessions

    return sessions.SessionManager()


def main() -> None:
    if freeze.FROZEN:
        sys.exit("gateway not in frozen build (P2)")
    port = int(os.environ.get("MCP_HTTP_PORT", str(ports.DEFAULT_GATEWAY_PORT + 1)))
    httpd = server.build_server("127.0.0.1", port, _manager_factory)

    def request_stop(*_args: object) -> None:
        threading.Thread(target=httpd.stop, daemon=True).start()

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, request_stop)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
