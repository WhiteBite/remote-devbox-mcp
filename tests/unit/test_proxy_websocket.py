from __future__ import annotations

import socket
import threading

from tests.proxy_fakes import start_ingress


class FakeWebSocket:
    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = int(self.sock.getsockname()[1])
        self.heads: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn) -> None:
        conn.settimeout(5.0)
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
            head, _, rest = buf.partition(b"\r\n\r\n")
            self.heads.append(head)
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
            )
            if rest:
                conn.sendall(rest)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
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


def _upgrade_request(port: int, *extra: str) -> bytes:
    headers = [
        "Host: t",
        *extra,
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version: 13",
    ]
    return (f"GET /p/{port}/hmr HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("latin-1")


def test_websocket_tunnel_echo(tmp_path):
    fake = FakeWebSocket()
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            sock.sendall(_upgrade_request(fake.port, "Authorization: Bearer tok"))
            head = b""
            while b"\r\n\r\n" not in head:
                head += sock.recv(65536)
            assert b"101 Switching Protocols" in head
            sock.sendall(b"ping")
            echoed = b""
            while b"ping" not in echoed:
                echoed += sock.recv(65536)
            assert echoed == b"ping"
    finally:
        fake.close()
        server.shutdown()


def test_websocket_pipelined_frames_before_101_not_lost(tmp_path):
    fake = FakeWebSocket()
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            sock.sendall(_upgrade_request(fake.port, "Authorization: Bearer tok") + b"ping")
            out = b""
            while b"ping" not in out:
                out += sock.recv(65536)
            assert b"101 Switching Protocols" in out
    finally:
        fake.close()
        server.shutdown()


def test_websocket_requires_token(tmp_path):
    fake = FakeWebSocket()
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
            sock.settimeout(5.0)
            sock.sendall(_upgrade_request(fake.port))
            out = b""
            while b"\r\n\r\n" not in out:
                out += sock.recv(65536)
        assert b"401" in out
        assert fake.heads == []
    finally:
        fake.close()
        server.shutdown()