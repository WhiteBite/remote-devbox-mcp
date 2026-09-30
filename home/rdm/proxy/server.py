"""Concurrent HTTP/1.1 reverse proxy for host loopback services."""

from __future__ import annotations

import http.client
import pathlib
import socketserver
import threading
from dataclasses import dataclass
from http import HTTPStatus

from rdm.proxy import access_log, auth, framing, router, upstream

MAX_CONNECTIONS = 256
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 120.0
_TOO_BIG = b"\x00too-big"


def _reason(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Unknown"


@dataclass(frozen=True)
class Config:
    mode: str
    token: bytes
    access_log_path: pathlib.Path
    self_authed: frozenset[int] = frozenset()
    allowed: frozenset[int] = frozenset()
    manifest_path: str | None = None
    target_host: str = "127.0.0.1"
    target_port: int = 0


def _read_body(sock, carry: bytes, length: int) -> tuple[bytes, bytes]:
    body = carry[:length]
    carry = carry[length:]
    while len(body) < length:
        try:
            chunk = sock.recv(min(65536, length - len(body)))
        except OSError:
            break
        if not chunk:
            break
        body += chunk
    return body, carry


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: Config) -> None:
        self.config = config
        self.sem = threading.BoundedSemaphore(MAX_CONNECTIONS)
        super().__init__(address, _Handler)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not server.sem.acquire(blocking=False):
            self._simple(503)
            return
        try:
            self._run(server)
        finally:
            server.sem.release()

    def _simple(self, code: int, keep_alive: bool = False) -> None:
        connection = b"keep-alive" if keep_alive else b"close"
        head = (
            b"HTTP/1.1 "
            + str(code).encode()
            + b" "
            + _reason(code).encode("latin-1")
            + b"\r\ncontent-length: 0\r\nconnection: "
            + connection
            + b"\r\n\r\n"
        )
        try:
            self.request.sendall(head)
        except OSError:
            pass

    def _read_head(self, sock, carry: bytes) -> tuple[bytes | None, bytes]:
        while b"\r\n\r\n" not in carry:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return None, b""
            if not chunk:
                return None, b""
            carry += chunk
            if len(carry) > framing.MAX_HEADER_BYTES + 4:
                return _TOO_BIG, b""
        head, _, rest = carry.partition(b"\r\n\r\n")
        return head, rest

    def _run(self, server: ProxyServer) -> None:
        sock = self.request
        sock.settimeout(IDLE_TIMEOUT)
        carry = b""
        while True:
            head, carry = self._read_head(sock, carry)
            if head is None:
                return
            if head == _TOO_BIG:
                self._simple(431)
                return
            decision = framing.validate_request(head)
            if isinstance(decision, framing.FramingError):
                self._simple(decision.status)
                return
            line, _, block = head.partition(b"\r\n")
            try:
                method, target, version = framing.parse_request_line(line)
                pairs = framing.parse_head(block)
            except framing.FramingRejected as rejected:
                self._simple(rejected.decision.status)
                return
            if any(name == b"upgrade" for name, _ in pairs):
                self._simple(501)
                return
            tokens = framing.connection_tokens(pairs)
            request_close = b"close" in tokens or (
                version == "HTTP/1.0" and b"keep-alive" not in tokens
            )
            if not self._dispatch(server, sock, method, target, pairs, decision, request_close, carry):
                return
            carry = self._carry

    _carry = b""

    def _dispatch(
        self,
        server: ProxyServer,
        sock,
        method: str,
        target: str,
        pairs: list[tuple[bytes, bytes]],
        decision: framing.FramingOk,
        request_close: bool,
        carry: bytes,
    ) -> bool:
        if decision.framing is framing.BodyFraming.LENGTH:
            if decision.expect_continue:
                try:
                    sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                except OSError:
                    return False
            body, carry = _read_body(sock, carry, decision.content_length)
        else:
            body = b""
        self._carry = carry
        target_bytes = target.encode("latin-1")

        if server.config.mode == "ingress" and router.is_manifest(target_bytes):
            if not auth.check_token(pairs, server.config.token):
                self._simple(401)
                return False
            data = router.load_manifest(server.config.manifest_path)
            if data is None:
                self._simple(404)
                return False
            return self._send_body(200, [(b"content-type", b"application/json")], data, request_close)

        if server.config.mode == "ingress":
            route = router.parse_route(target_bytes)
            if route is None:
                self._simple(404)
                return False
            access_log.log(server.config.access_log_path, route.port, method, target, "ingress")
            if route.port not in server.config.self_authed:
                if not auth.check_token(pairs, server.config.token):
                    self._simple(401)
                    return False
                if route.port not in server.config.allowed:
                    self._simple(403)
                    return False
            host, port, path = "127.0.0.1", route.port, route.rest
        else:
            if not auth.check_token(pairs, server.config.token):
                self._simple(401)
                return False
            host, port, path = server.config.target_host, server.config.target_port, target_bytes

        access_log.log(server.config.access_log_path, port, method, target, "forward")
        return self._forward(sock, method, path, pairs, body, host, port, request_close)

    def _forward(
        self,
        sock,
        method: str,
        path: bytes,
        pairs: list[tuple[bytes, bytes]],
        body: bytes,
        host: str,
        port: int,
        request_close: bool,
    ) -> bool:
        host_header = f"{host}:{port}".encode()
        forward = upstream.filter_forward_headers(pairs, host_header, self.client_address[0], b"https")
        connection = upstream.open_connection(host, port, CONNECT_TIMEOUT)
        try:
            upstream.send_head(connection, method, path, forward)
            upstream.send_body(connection, body)
            response = connection.getresponse()
            return self._relay(sock, method, response, request_close)
        except (OSError, http.client.HTTPException):
            self._simple(502)
            return False
        finally:
            connection.close()

    def _relay(self, sock, method: str, response, request_close: bool) -> bool:
        raw = [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in response.getheaders()
        ]
        hop = {name for name, _ in framing.hop_by_hop(raw)}
        out = [
            (name, value)
            for name, value in raw
            if name not in hop and name not in (b"content-length", b"transfer-encoding")
        ]
        bodyless = method == "HEAD" or response.status in (204, 304) or 100 <= response.status < 200
        if bodyless:
            original = next((value for name, value in raw if name == b"content-length"), None)
            if original is not None:
                out.append((b"content-length", original))
            elif response.length is not None:
                out.append((b"content-length", str(response.length).encode()))
            close = request_close
        elif response.chunked:
            out.append((b"transfer-encoding", b"chunked"))
            close = request_close
        elif response.length is not None:
            out.append((b"content-length", str(response.length).encode()))
            close = request_close
        else:
            close = True
        out.append((b"connection", b"close" if close else b"keep-alive"))
        head = (
            b"HTTP/1.1 "
            + str(response.status).encode()
            + b" "
            + _reason(response.status).encode("latin-1")
            + b"\r\n"
        )
        for name, value in out:
            head += name + b": " + value + b"\r\n"
        head += b"\r\n"
        try:
            sock.sendall(head)
            if not bodyless:
                if response.chunked:
                    self._stream_chunked(sock, response)
                elif response.length is not None:
                    self._stream_length(sock, response)
                else:
                    self._stream_eof(sock, response)
        except OSError:
            return False
        return not close

    @staticmethod
    def _stream_length(sock, response) -> None:
        remaining = response.length or 0
        while remaining > 0:
            chunk = response.read1(min(65536, remaining))
            if not chunk:
                break
            sock.sendall(chunk)
            remaining -= len(chunk)

    @staticmethod
    def _stream_chunked(sock, response) -> None:
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            sock.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        sock.sendall(b"0\r\n\r\n")

    @staticmethod
    def _stream_eof(sock, response) -> None:
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            sock.sendall(chunk)

    def _send_body(
        self,
        code: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
        request_close: bool,
    ) -> bool:
        close = request_close
        head = b"HTTP/1.1 " + str(code).encode() + b" " + _reason(code).encode("latin-1") + b"\r\n"
        for name, value in headers:
            head += name + b": " + value + b"\r\n"
        head += b"content-length: " + str(len(body)).encode() + b"\r\n"
        head += b"connection: " + (b"close" if close else b"keep-alive") + b"\r\n\r\n"
        try:
            self.request.sendall(head + body)
        except OSError:
            return False
        return not close


def build_ingress_server(
    host: str,
    port: int,
    token: str | bytes,
    self_authed: set[int] | frozenset[int],
    allowed: set[int] | frozenset[int],
    manifest_path: str | None,
    access_log_path: pathlib.Path,
) -> ProxyServer:
    token_bytes = token if isinstance(token, bytes) else token.encode()
    config = Config(
        mode="ingress",
        token=token_bytes,
        access_log_path=access_log_path,
        self_authed=frozenset(self_authed),
        allowed=frozenset(allowed),
        manifest_path=manifest_path,
    )
    return ProxyServer((host, port), config)


def build_target_server(
    host: str,
    port: int,
    token: str | bytes,
    target_host: str,
    target_port: int,
    access_log_path: pathlib.Path,
) -> ProxyServer:
    token_bytes = token if isinstance(token, bytes) else token.encode()
    config = Config(
        mode="target",
        token=token_bytes,
        access_log_path=access_log_path,
        target_host=target_host,
        target_port=target_port,
    )
    return ProxyServer((host, port), config)


def serve_ingress(
    host: str,
    port: int,
    token: str,
    self_authed: set[int],
    allowed: set[int],
    manifest_path: str | None,
) -> None:
    server = build_ingress_server(
        host, port, token, self_authed, allowed, manifest_path, access_log.default_path()
    )
    server.serve_forever()


def serve_target(host: str, port: int, token: str, target_host: str, target_port: int) -> None:
    server = build_target_server(host, port, token, target_host, target_port, access_log.default_path())
    server.serve_forever()