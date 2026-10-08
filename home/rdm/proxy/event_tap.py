"""Fail-open MCP observability taps wired into the ingress proxy.

The taps observe bridge JSON-RPC traffic and emit metadata-only events
through rdm.events.sink. They must never break the agent's call: every tap
error is swallowed and reported as a ``tap_error`` event instead, and the
response path parses only after the relay has already delivered the upstream
bytes to the client.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rdm import ports
from rdm.events import sink
from rdm.proxy import mcp_tap, router

CAP = 32 * 1024 * 1024


class TeeSocket:
    """Write-only socket wrapper: forwards every sendall byte, keeps a bounded capture."""

    def __init__(self, sock, cap: int) -> None:
        self.sock = sock
        self.cap = cap
        self.buf = bytearray()
        self.total = 0
        self.truncated = False

    def sendall(self, data: bytes) -> None:
        self.sock.sendall(data)
        try:
            self.total += len(data)
            room = self.cap - len(self.buf)
            if room > 0:
                kept = data[:room]
                self.buf += kept
                if len(kept) < len(data):
                    self.truncated = True
            elif data:
                self.truncated = True
        except Exception:
            pass


def on_request(config, method: str, target: str, pairs, body: bytes) -> dict[str, Any] | None:
    """Tap a bridge POST body; returns the parsed metadata for response-path gating."""
    if config.mode != "ingress" or method.upper() != "POST":
        return None
    route = router.parse_route(target.encode("latin-1"))
    if route is None or route.port != ports.BRIDGE_PORT:
        return None
    try:
        meta = mcp_tap.parse_request(body, CAP)
    except Exception:
        _emit({"kind": "tap_error", "port": ports.BRIDGE_PORT, "method": method})
        return None
    if meta is not None:
        _emit({"kind": "mcp_request", "port": ports.BRIDGE_PORT, "method": method, **meta})
    return meta


def on_response(
    config,
    port: int,
    path: bytes,
    method: str,
    status: int,
    response,
    sock,
    done: Callable[[TeeSocket], bool],
) -> bool:
    """Relay a bridge tools/call response through a bounded tee, then emit its metadata.

    ``done`` receives the wrapped socket and performs the relay; parsing and
    emission happen only after it returns, so the client already holds the
    upstream response whatever the tap does next.
    """
    if config.mode != "ingress" or port != ports.BRIDGE_PORT:
        return done(sock)
    tee = TeeSocket(sock, CAP)
    relayed = done(tee)
    try:
        meta = _response_meta(tee)
    except Exception:
        _emit({"kind": "tap_error", "port": port, "method": method})
        return relayed
    if meta is not None:
        event: dict[str, Any] = {
            "kind": "mcp_response",
            "port": port,
            "method": method,
            "status": status,
        }
        event.update(meta)
        _emit(event)
    return relayed


def _response_meta(tee: TeeSocket) -> dict[str, Any] | None:
    if tee.truncated:
        return {"bytes": tee.total, "truncated": True}
    return mcp_tap.parse_response(bytes(tee.buf), CAP)


def _emit(event: dict[str, Any]) -> None:
    try:
        sink.emit(event)
    except Exception:
        pass
