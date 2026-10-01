"""Test doubles and helpers for the proxy server tests."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

from rdm.proxy.server import build_ingress_server, build_target_server


def raw_request(port: int, raw: bytes, timeout: float = 10.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw)
        out = b""
        while True:
            try:
                data = sock.recv(65536)
            except (TimeoutError, OSError):
                break
            if not data:
                break
            out += data
        return out


def request(
    method: str,
    target: str,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
    connection: str = "close",
) -> bytes:
    lines = [f"{method} {target} HTTP/1.1", "Host: test.local"]
    for name, value in headers or []:
        lines.append(f"{name}: {value}")
    lines.append(f"Connection: {connection}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def start_ingress(
    tmp_path: Path,
    token: str = "tok",
    self_authed: set[int] | tuple[int, ...] = (),
    allowed: set[int] | tuple[int, ...] = (),
    manifest_path: str | None = None,
) -> tuple[object, int]:
    server = build_ingress_server(
        "127.0.0.1", 0, token, set(self_authed), set(allowed), manifest_path, tmp_path / "access.log"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def start_target(
    tmp_path: Path,
    token: str = "tok",
    target_host: str = "127.0.0.1",
    target_port: int = 0,
) -> tuple[object, int]:
    server = build_target_server(
        "127.0.0.1", 0, token, target_host, target_port, tmp_path / "access.log"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


class FakeUpstream:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.seen: list[dict] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = int(self.sock.getsockname()[1])
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn) -> None:
        conn.settimeout(10.0)
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
            head, _, body = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            method, path, _ = lines[0].split(b" ", 2)
            pairs: list[tuple[bytes, bytes]] = []
            for line in lines[1:]:
                name, sep, value = line.partition(b":")
                if sep:
                    pairs.append((name.strip().lower(), value.strip()))
            length = 0
            for name, value in pairs:
                if name == b"content-length":
                    length = int(value)
            while len(body) < length:
                data = conn.recv(65536)
                if not data:
                    break
                body += data
            self.seen.append({"method": method, "path": path, "headers": pairs, "body": body})
            self.responder(conn, method, path, pairs, body)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def responder(conn, method: bytes, path: bytes, pairs: list[tuple[bytes, bytes]], body: bytes) -> None:
    if path.endswith(b"/sse"):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
        conn.sendall(b"data: event-1\n\n")
        time.sleep(0.4)
        conn.sendall(b"data: event-2\n\n")
    elif path.endswith(b"/chunked"):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
        conn.sendall(b"5\r\nhello\r\n6\r\n-world\r\n0\r\n\r\n")
    elif path.endswith(b"/eof"):
        conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
        conn.sendall(b"eof-body")
    elif method == b"HEAD":
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n")
    elif path.endswith(b"/204"):
        conn.sendall(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
    elif path.endswith(b"/304"):
        conn.sendall(b"HTTP/1.1 304 Not Modified\r\nConnection: close\r\n\r\n")
    elif path.endswith(b"/echo-headers"):
        payload = b"\n".join(name for name, _ in pairs)
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )
    elif path.endswith(b"/echo-body"):
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
    elif path.endswith(b"/truncated"):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nabc")
    else:
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")