"""Concurrent HTTP/1.1 reverse proxy for host loopback services."""

from __future__ import annotations

import http.client
import pathlib
import re
import socketserver
import threading
from dataclasses import dataclass
from http import HTTPStatus

from rdm import ports
from rdm.proxy import access_log, auth, framing, router, upstream

MAX_CONNECTIONS = 256
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 120.0
MAX_HTML_REWRITE = 8 * 1024 * 1024
_TOO_BIG = b"\x00too-big"

_BASE_TAG = re.compile(
    rb"<base\b[^>]*?\shref\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)[^>]*>", re.IGNORECASE
)
_HEAD_TAG = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)

_BODY_UNAUTHORIZED = b'{"error":"unauthorized","hint":"send Authorization: Bearer <token>"}'
_BODY_FORBIDDEN = b'{"error":"forbidden","hint":"port is not in the allowed list for this profile"}'
_BODY_NOT_FOUND = b'{"error":"not_found","hint":"unknown path; endpoints live under /p/<port>/"}'
_BODY_BAD_GATEWAY = b'{"error":"bad_gateway","hint":"upstream unavailable"}'
_BODY_OVERLOADED = b'{"error":"service_unavailable","hint":"too many concurrent connections"}'
_AUTH_CHALLENGE = ((b"www-authenticate", b'Bearer realm="devbox"'),)


def _reason(code: int) -> str:
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Unknown"


def _is_websocket(pairs: list[tuple[bytes, bytes]]) -> bool:
    if b"upgrade" not in framing.connection_tokens(pairs):
        return False
    return any(name == b"upgrade" and b"websocket" in value.lower() for name, value in pairs)


def _rewrite_base_href(body: bytes, prefix: bytes) -> bytes:
    tag = b'<base href="' + prefix + b'">'
    rewritten, count = _BASE_TAG.subn(tag, body, count=1)
    if count:
        return rewritten
    head = _HEAD_TAG.search(body)
    if head is None:
        return body
    return body[: head.end()] + tag + body[head.end() :]


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
    ranges: frozenset[tuple[int, int]] = frozenset()
    denied: frozenset[int] = frozenset()

    @property
    def policy(self) -> ports.PortPolicy:
        return ports.PortPolicy(
            tuple(self.self_authed), tuple(self.allowed), None, tuple(self.ranges), tuple(self.denied)
        )


def _read_body(sock, carry: bytes, length: int) -> tuple[bytes, bytes, bool]:
    body = carry[:length]
    carry = carry[length:]
    while len(body) < length:
        try:
            chunk = sock.recv(min(65536, length - len(body)))
        except OSError:
            return body, b"", False
        if not chunk:
            return body, b"", False
        body += chunk
    return body, carry, True


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
            self._simple(503, body=_BODY_OVERLOADED)
            return
        try:
            self._run(server)
        finally:
            server.sem.release()

    def _simple(
        self,
        code: int,
        keep_alive: bool = False,
        body: bytes = b"",
        headers: tuple[tuple[bytes, bytes], ...] = (),
    ) -> None:
        connection = b"keep-alive" if keep_alive else b"close"
        head = (
            b"HTTP/1.1 "
            + str(code).encode()
            + b" "
            + _reason(code).encode("latin-1")
            + b"\r\n"
        )
        if body:
            head += b"content-type: application/json\r\n"
        for name, value in headers:
            head += name + b": " + value + b"\r\n"
        head += b"content-length: " + str(len(body)).encode() + b"\r\n"
        head += b"connection: " + connection + b"\r\n\r\n"
        try:
            self.request.sendall(head + body)
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
        target_bytes = target.encode("latin-1")

        if server.config.mode == "ingress" and router.is_manifest(target_bytes):
            if not auth.check_token(pairs, server.config.token):
                self._simple(401, body=_BODY_UNAUTHORIZED, headers=_AUTH_CHALLENGE)
                return False
            data = router.load_manifest(server.config.manifest_path)
            if data is None:
                self._simple(404, body=_BODY_NOT_FOUND)
                return False
            consumed = self._consume_body(sock, decision, carry)
            if consumed is None:
                return False
            self._carry = consumed[1]
            return self._send_body(200, [(b"content-type", b"application/json")], data, request_close)

        if server.config.mode == "ingress":
            route = router.parse_route(target_bytes)
            if route is None:
                self._simple(404, body=_BODY_NOT_FOUND)
                return False
            access_log.log(server.config.access_log_path, route.port, method, target, "ingress")
            if route.port not in server.config.self_authed:
                if not auth.check_token(pairs, server.config.token):
                    self._simple(401, body=_BODY_UNAUTHORIZED, headers=_AUTH_CHALLENGE)
                    return False
                if not ports.is_port_allowed(server.config.policy, route.port):
                    self._simple(403, body=_BODY_FORBIDDEN)
                    return False
            host, port, path = "127.0.0.1", route.port, route.rest
        else:
            if not auth.check_token(pairs, server.config.token):
                self._simple(401, body=_BODY_UNAUTHORIZED, headers=_AUTH_CHALLENGE)
                return False
            host, port, path = server.config.target_host, server.config.target_port, target_bytes

        consumed = self._consume_body(sock, decision, carry)
        if consumed is None:
            return False
        body, carry = consumed
        self._carry = carry

        access_log.log(server.config.access_log_path, port, method, target, "forward")
        if _is_websocket(pairs):
            return self._forward_websocket(sock, method, path, pairs, host, port, carry)
        return self._forward(sock, method, path, pairs, body, host, port, request_close)

    def _consume_body(self, sock, decision: framing.FramingOk, carry: bytes) -> tuple[bytes, bytes] | None:
        if decision.framing is not framing.BodyFraming.LENGTH:
            return b"", carry
        if decision.expect_continue:
            try:
                sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
            except OSError:
                return None
        body, carry, complete = _read_body(sock, carry, decision.content_length)
        if not complete:
            self._simple(400)
            return None
        return body, carry

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
        connection: http.client.HTTPConnection | None = None
        try:
            connection = upstream.open_connection(host, port, CONNECT_TIMEOUT)
            upstream.send_head(connection, method, path, forward)
            upstream.send_body(connection, body)
            response = connection.getresponse()
            return self._relay(sock, method, response, request_close, port, path)
        except (OSError, http.client.HTTPException):
            self._simple(502, body=_BODY_BAD_GATEWAY)
            return False
        finally:
            if connection is not None:
                connection.close()

    def _forward_websocket(
        self,
        sock,
        method: str,
        path: bytes,
        pairs: list[tuple[bytes, bytes]],
        host: str,
        port: int,
        carry: bytes,
    ) -> bool:
        host_header = f"{host}:{port}".encode()
        forward = upstream.filter_upgrade_headers(pairs, host_header, self.client_address[0], b"https")
        try:
            connection = upstream.open_raw(host, port, CONNECT_TIMEOUT)
        except OSError:
            self._simple(502, body=_BODY_BAD_GATEWAY)
            return False
        try:
            head = method.encode("latin-1") + b" " + path + b" HTTP/1.1\r\n"
            for name, value in forward:
                head += name + b": " + value + b"\r\n"
            head += b"\r\n"
            connection.sendall(head)
            # клиенты пайплайнят первые WS-фреймы сразу за upgrade-запросом — они уже в carry
            if carry:
                connection.sendall(carry)
            upstream_head = self._read_raw_head(connection)
            if upstream_head is None:
                self._simple(502, body=_BODY_BAD_GATEWAY)
                return False
            response_head, rest = upstream_head
            try:
                status = int(response_head.split(b" ", 2)[1])
            except (IndexError, ValueError):
                self._simple(502, body=_BODY_BAD_GATEWAY)
                return False
            try:
                sock.sendall(response_head)
                if rest:
                    sock.sendall(rest)
            except OSError:
                return False
            if status != 101:
                self._pipe_until_eof(connection, sock)
                return False
            self._tunnel(sock, connection)
            return False
        finally:
            connection.close()

    @staticmethod
    def _read_raw_head(sock) -> tuple[bytes, bytes] | None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
            if len(buf) > framing.MAX_HEADER_BYTES:
                return None
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head + b"\r\n\r\n", rest

    @staticmethod
    def _pipe_until_eof(src, dst) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass

    @staticmethod
    def _tunnel(client, upstream_sock) -> None:
        client.settimeout(None)
        upstream_sock.settimeout(None)
        stop = threading.Event()

        def pump(src, dst) -> None:
            try:
                while not stop.is_set():
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                stop.set()

        first = threading.Thread(target=pump, args=(client, upstream_sock), daemon=True)
        second = threading.Thread(target=pump, args=(upstream_sock, client), daemon=True)
        first.start()
        second.start()
        first.join()
        second.join()

    def _relay(self, sock, method: str, response, request_close: bool, port: int, path: bytes) -> bool:
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
        bodyless = method == "HEAD" or response.status in (204, 304)
        content_type = next((value for name, value in raw if name == b"content-type"), b"")
        content_encoding = next((value for name, value in raw if name == b"content-encoding"), None)
        rewritable = (
            not bodyless
            and self.server.config.mode == "ingress"
            and response.length is not None
            and response.length <= MAX_HTML_REWRITE
            and content_type.strip().lower().startswith(b"text/html")
            and (content_encoding is None or content_encoding.strip().lower() == b"identity")
        )
        rewritten: bytes | None = None
        if rewritable:
            prefix = b"/p/" + str(port).encode("ascii") + path.rpartition(b"/")[0] + b"/"
            try:
                rewritten = _rewrite_base_href(response.read(response.length), prefix)
            except (OSError, http.client.HTTPException):
                return False
        if rewritten is not None:
            out.append((b"content-length", str(len(rewritten)).encode()))
            close = request_close
        elif bodyless:
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
            if rewritten is not None:
                sock.sendall(rewritten)
            elif not bodyless:
                if response.chunked:
                    self._stream_chunked(sock, response)
                elif response.length is not None:
                    # read1() декрементирует response.length, поэтому эталон снимаем до стриминга
                    expected = response.length
                    if self._stream_length(sock, response) != expected:
                        return False
                else:
                    self._stream_eof(sock, response)
        except (OSError, http.client.HTTPException):
            return False
        return not close

    @staticmethod
    def _stream_length(sock, response) -> int:
        remaining = response.length or 0
        sent = 0
        while remaining > 0:
            chunk = response.read1(min(65536, remaining))
            if not chunk:
                break
            sock.sendall(chunk)
            sent += len(chunk)
            remaining -= len(chunk)
        return sent

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
    ranges: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    denied: set[int] | frozenset[int] = frozenset(),
) -> ProxyServer:
    token_bytes = token if isinstance(token, bytes) else token.encode()
    config = Config(
        mode="ingress",
        token=token_bytes,
        access_log_path=access_log_path,
        self_authed=frozenset(self_authed),
        allowed=frozenset(allowed),
        manifest_path=manifest_path,
        ranges=frozenset(ranges),
        denied=frozenset(denied),
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
    ranges: set[tuple[int, int]] | frozenset[tuple[int, int]] = frozenset(),
    denied: set[int] | frozenset[int] = frozenset(),
) -> None:
    server = build_ingress_server(
        host, port, token, self_authed, allowed, manifest_path, access_log.default_path(), ranges, denied
    )
    server.serve_forever()


def serve_target(host: str, port: int, token: str, target_host: str, target_port: int) -> None:
    server = build_target_server(host, port, token, target_host, target_port, access_log.default_path())
    server.serve_forever()