from __future__ import annotations

import time

from tests.proxy_fakes import FakeUpstream, raw_request, request, responder, start_ingress, start_target


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


def test_connect_timeout_does_not_cut_slow_streams(monkeypatch, tmp_path):
    from rdm.proxy import server as proxy_server

    monkeypatch.setattr(proxy_server, "CONNECT_TIMEOUT", 0.3)

    def slow_chunked(conn, method, path, pairs, body):
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        conn.sendall(_chunk(b"data: one\n\n"))
        time.sleep(0.8)
        conn.sendall(_chunk(b"data: two\n\n"))
        conn.sendall(b"0\r\n\r\n")

    fake = FakeUpstream(slow_chunked)
    server, port = start_target(tmp_path, target_port=fake.port)
    try:
        out = raw_request(
            port,
            request(
                "POST",
                "/mcp",
                headers=[("Authorization", "Bearer tok"), ("Content-Length", "2")],
                body=b"{}",
            ),
        )
        assert b"data: one" in out
        assert b"data: two" in out
        assert out.endswith(b"0\r\n\r\n")
    finally:
        fake.close()
        server.shutdown()


def _url(port: int, rest: bytes = b"/ok") -> str:
    return f"/p/{port}{rest.decode()}"


def test_route_ok(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port), headers=[("Authorization", "Bearer tok")]))
        assert b"200 OK" in out and out.endswith(b"ok")
    finally:
        fake.close()
        server.shutdown()


def test_wrong_token_401(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port), headers=[("Authorization", "Bearer nope")]))
        assert b"401 Unauthorized" in out
        assert fake.seen == []
    finally:
        fake.close()
        server.shutdown()


def test_unauth_expect_continue_401_without_100_continue(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request(
                "POST",
                _url(fake.port),
                headers=[("Expect", "100-continue"), ("Content-Length", "5")],
                body=b"hello",
            ),
        )
        assert b"401 Unauthorized" in out
        assert b"100 Continue" not in out
        assert b"connection: close" in out.lower()
        assert fake.seen == []
    finally:
        fake.close()
        server.shutdown()


def test_not_allowed_port_403(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed=set())
    try:
        out = raw_request(port, request("GET", _url(fake.port), headers=[("Authorization", "Bearer tok")]))
        assert b"403 Forbidden" in out
    finally:
        fake.close()
        server.shutdown()


def test_self_authed_passthrough(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, self_authed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port)))
        assert b"200 OK" in out and out.endswith(b"ok")
    finally:
        fake.close()
        server.shutdown()


def test_unknown_path_404(tmp_path):
    server, port = start_ingress(tmp_path)
    try:
        out = raw_request(port, request("GET", "/nope"))
        assert b"404 Not Found" in out
    finally:
        server.shutdown()


def test_non_ascii_port_404(tmp_path):
    server, port = start_ingress(tmp_path)
    try:
        raw = b"GET /p/\xd9\xa5\xd9\xa5\xd9\xa5/mcp HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"
        out = raw_request(port, raw)
        assert b"404 Not Found" in out
    finally:
        server.shutdown()


def test_manifest_with_token(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b'{"ok":true}')
    server, port = start_ingress(tmp_path, manifest_path=str(manifest))
    try:
        out = raw_request(port, request("GET", "/p/9000/manifest.json", headers=[("Authorization", "Bearer tok")]))
        assert b"200 OK" in out and b'{"ok":true}' in out
    finally:
        server.shutdown()


def test_manifest_without_token_401(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b"{}")
    server, port = start_ingress(tmp_path, manifest_path=str(manifest))
    try:
        out = raw_request(port, request("GET", "/p/9000/manifest.json"))
        assert b"401 Unauthorized" in out
    finally:
        server.shutdown()


def test_host_rewritten(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        raw_request(port, request("GET", _url(fake.port, b"/echo-headers"), headers=[("Authorization", "Bearer tok")]))
        headers = dict(fake.seen[-1]["headers"])
        assert headers[b"host"] == f"127.0.0.1:{fake.port}".encode()
    finally:
        fake.close()
        server.shutdown()


def test_prefix_strip_and_query(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        raw_request(port, request("GET", _url(fake.port, b"/deep/path?x=1&y=2"), headers=[("Authorization", "Bearer tok")]))
        assert fake.seen[-1]["path"] == b"/deep/path?x=1&y=2"
    finally:
        fake.close()
        server.shutdown()


def test_origin_is_forwarded_not_blocked(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request(
                "GET",
                _url(fake.port, b"/echo-headers"),
                headers=[("Authorization", "Bearer tok"), ("Origin", "https://example.test")],
            ),
        )
        assert b"200 OK" in out and b"origin" in out
    finally:
        fake.close()
        server.shutdown()


def test_hop_by_hop_stripped(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request(
                "GET",
                _url(fake.port, b"/echo-headers"),
                headers=[("Authorization", "Bearer tok"), ("Connection", "close, X-Foo"), ("X-Foo", "1")],
            ),
        )
        assert b"x-foo" not in out
    finally:
        fake.close()
        server.shutdown()