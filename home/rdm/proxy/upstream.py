"""Single-use HTTP/1.1 upstream connections (never reused, so no response desync)."""

from __future__ import annotations

import http.client
import socket

from rdm.proxy import framing

_DROP = frozenset((b"host", b"x-forwarded-host", b"expect"))


def filter_forward_headers(
    pairs: list[tuple[bytes, bytes]],
    host_header: bytes,
    client_ip: str | None,
    proto: bytes,
) -> list[tuple[bytes, bytes]]:
    named = {name for name, _ in framing.hop_by_hop(pairs)} | _DROP
    out = [(name, value) for name, value in pairs if name not in named]
    out.append((b"host", host_header))
    out.append((b"connection", b"close"))
    if client_ip:
        out.append((b"x-forwarded-for", client_ip.encode("latin-1")))
    out.append((b"x-forwarded-proto", proto))
    return out


def open_connection(host: str, port: int, timeout: float) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(host, port, timeout=timeout)


def open_raw(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def filter_upgrade_headers(
    pairs: list[tuple[bytes, bytes]],
    host_header: bytes,
    client_ip: str | None,
    proto: bytes,
) -> list[tuple[bytes, bytes]]:
    drop = {b"host", b"x-forwarded-host"}
    out = [(name, value) for name, value in pairs if name not in drop]
    out.append((b"host", host_header))
    if client_ip:
        out.append((b"x-forwarded-for", client_ip.encode("latin-1")))
    out.append((b"x-forwarded-proto", proto))
    return out


def send_head(
    connection: http.client.HTTPConnection,
    method: str,
    path: bytes,
    headers: list[tuple[bytes, bytes]],
) -> None:
    connection.putrequest(method, path.decode("latin-1"), skip_host=True, skip_accept_encoding=True)
    for name, value in headers:
        connection.putheader(name.decode("latin-1"), value)
    connection.endheaders()


def send_body(connection: http.client.HTTPConnection, body: bytes) -> None:
    if body:
        connection.send(body)