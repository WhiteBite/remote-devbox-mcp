from __future__ import annotations

import socket
import time

from tests.proxy_fakes import FakeUpstream, raw_request, request, responder, start_ingress


def _url(port: int, rest: bytes = b"/ok") -> str:
    return f"/p/{port}{rest.decode()}"


def _auth() -> list[tuple[str, str]]:
    return [("Authorization", "Bearer tok")]


def test_sse_incremental(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
            sock.sendall(request("GET", _url(fake.port, b"/sse"), headers=_auth()))
            started = time.monotonic()
            head = b""
            while b"\r\n\r\n" not in head:
                head += sock.recv(65536)
            first = b""
            while b"event-1" not in first:
                first += sock.recv(65536)
            first_at = time.monotonic()
            rest = b""
            while b"event-2" not in rest:
                rest += sock.recv(65536)
        assert b"event-1" in first
        assert first_at - started < 0.35
        assert b"event-2" in rest
    finally:
        fake.close()
        server.shutdown()


def test_chunked_response_relayed(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port, b"/chunked"), headers=_auth()))
        assert b"transfer-encoding: chunked" in out.lower()
        assert b"hello" in out and b"-world" in out
    finally:
        fake.close()
        server.shutdown()


def test_eof_delimited_response(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port, b"/eof"), headers=_auth()))
        assert b"200 OK" in out and b"eof-body" in out
    finally:
        fake.close()
        server.shutdown()


def test_head_has_no_body(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("HEAD", _url(fake.port), headers=_auth()))
        head, _, body = out.partition(b"\r\n\r\n")
        assert b"200 OK" in head and b"content-length: 5" in head.lower()
        assert body == b""
    finally:
        fake.close()
        server.shutdown()


def test_204_no_body(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port, b"/204"), headers=_auth()))
        assert b"204 No Content" in out
        assert out.partition(b"\r\n\r\n")[2] == b""
    finally:
        fake.close()
        server.shutdown()


def test_304_no_body(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port, b"/304"), headers=_auth()))
        assert b"304 Not Modified" in out
        assert out.partition(b"\r\n\r\n")[2] == b""
    finally:
        fake.close()
        server.shutdown()


def test_websocket_upgrade_501(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        raw = (
            f"GET {_url(fake.port)} HTTP/1.1\r\nHost: t\r\nAuthorization: Bearer tok\r\n"
            "Upgrade: websocket\r\nConnection: upgrade\r\n\r\n"
        ).encode("latin-1")
        out = raw_request(port, raw)
        assert b"501 Not Implemented" in out
    finally:
        fake.close()
        server.shutdown()


def test_chunked_request_501(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request("POST", _url(fake.port), headers=_auth() + [("Transfer-Encoding", "chunked")], body=b"0\r\n\r\n"),
        )
        assert b"501 Not Implemented" in out
        assert fake.seen == []
    finally:
        fake.close()
        server.shutdown()


def test_oversized_content_length_413_no_upstream(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request("POST", _url(fake.port), headers=_auth() + [("Content-Length", str(50 * 1024 * 1024 + 1))]),
        )
        assert b"413 " in out
        assert fake.seen == []
    finally:
        fake.close()
        server.shutdown()


def test_conflicting_content_length_400(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        raw = (
            f"POST {_url(fake.port)} HTTP/1.1\r\nHost: t\r\nAuthorization: Bearer tok\r\n"
            "Content-Length: 5\r\nContent-Length: 6\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        out = raw_request(port, raw)
        assert b"400 Bad Request" in out
        assert fake.seen == []
    finally:
        fake.close()
        server.shutdown()


def test_duplicate_singleton_400(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        raw = (
            f"GET {_url(fake.port)} HTTP/1.1\r\nHost: a\r\nHost: b\r\n"
            "Authorization: Bearer tok\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        out = raw_request(port, raw)
        assert b"400 Bad Request" in out
    finally:
        fake.close()
        server.shutdown()


def test_post_body_forwarded(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request("POST", _url(fake.port, b"/echo-body"), headers=_auth() + [("Content-Length", "5")], body=b"hello"),
        )
        assert fake.seen[-1]["body"] == b"hello"
        assert out.endswith(b"hello")
    finally:
        fake.close()
        server.shutdown()


def test_large_body_within_cap_forwarded(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    payload = b"x" * 100000
    try:
        raw_request(
            port,
            request(
                "POST",
                _url(fake.port, b"/echo-body"),
                headers=_auth() + [("Content-Length", str(len(payload)))],
                body=payload,
            ),
        )
        assert fake.seen[-1]["body"] == payload
    finally:
        fake.close()
        server.shutdown()


def test_connection_close_honored(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port), headers=_auth(), connection="close"))
        assert b"connection: close" in out.lower()
    finally:
        fake.close()
        server.shutdown()


def test_bad_request_line_400(tmp_path):
    server, port = start_ingress(tmp_path)
    try:
        out = raw_request(port, b"GARBAGE\r\n\r\n")
        assert b"400 Bad Request" in out
    finally:
        server.shutdown()