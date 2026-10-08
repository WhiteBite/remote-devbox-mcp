from __future__ import annotations

import json
import re
import time

from tests.proxy_fakes import FakeUpstream, raw_request, request, responder, start_ingress, start_target


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


def _head_body(out: bytes) -> tuple[bytes, bytes]:
    head, _, body = out.partition(b"\r\n\r\n")
    return head, body


def _content_length(head: bytes) -> int:
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value.strip())
    raise AssertionError("content-length missing")


def _assert_generic_error(head: bytes, body: bytes, tmp_path) -> None:
    assert _content_length(head) == len(body)
    assert re.search(rb"\d{2,}", body) is None
    assert b"muffin" not in body
    assert str(tmp_path).encode() not in body


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
        head, body = _head_body(out)
        assert b"401 Unauthorized" in head
        assert b'www-authenticate: bearer realm="devbox"' in head.lower()
        data = json.loads(body)
        assert data["error"] == "unauthorized"
        assert "hint" in data
        assert str(fake.port).encode() not in body
        _assert_generic_error(head, body, tmp_path)
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
        head, body = _head_body(out)
        assert b"403 Forbidden" in head
        data = json.loads(body)
        assert data["error"] == "forbidden"
        assert "hint" in data
        assert str(fake.port).encode() not in body
        _assert_generic_error(head, body, tmp_path)
    finally:
        fake.close()
        server.shutdown()


def test_port_inside_range_allowed(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed=set(), ranges={(fake.port, fake.port)})
    try:
        out = raw_request(port, request("GET", _url(fake.port), headers=[("Authorization", "Bearer tok")]))
        assert b"200 OK" in out and out.endswith(b"ok")
    finally:
        fake.close()
        server.shutdown()


def test_port_outside_range_403(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed=set(), ranges={(1, fake.port - 1)})
    try:
        out = raw_request(port, request("GET", _url(fake.port), headers=[("Authorization", "Bearer tok")]))
        head, body = _head_body(out)
        assert b"403 Forbidden" in head
        assert json.loads(body)["error"] == "forbidden"
        assert fake.seen == []
    finally:
        fake.close()
        server.shutdown()


def test_denied_port_beats_allowed_and_range_403(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(
        tmp_path, allowed={fake.port}, ranges={(fake.port, fake.port)}, denied={fake.port}
    )
    try:
        out = raw_request(port, request("GET", _url(fake.port), headers=[("Authorization", "Bearer tok")]))
        assert b"403 Forbidden" in out
        assert fake.seen == []
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


def test_cockpit_port_denied_even_when_self_authed(tmp_path):
    from rdm import ports

    server, port = start_ingress(tmp_path, self_authed={ports.COCKPIT_PORT})
    try:
        out = raw_request(port, request("GET", f"/p/{ports.COCKPIT_PORT}/"))
        head, body = _head_body(out)
        assert b"403 Forbidden" in head
        assert json.loads(body)["error"] == "forbidden"
    finally:
        server.shutdown()


def test_unknown_path_404(tmp_path):
    server, port = start_ingress(tmp_path)
    try:
        out = raw_request(port, request("GET", "/nope"))
        head, body = _head_body(out)
        assert b"404 Not Found" in head
        data = json.loads(body)
        assert data["error"] == "not_found"
        assert "hint" in data
        _assert_generic_error(head, body, tmp_path)
    finally:
        server.shutdown()


def test_manifest_missing_404(tmp_path):
    server, port = start_ingress(tmp_path, manifest_path=str(tmp_path / "absent.json"))
    try:
        out = raw_request(port, request("GET", "/p/9000/manifest.json", headers=[("Authorization", "Bearer tok")]))
        head, body = _head_body(out)
        assert b"404 Not Found" in head
        data = json.loads(body)
        assert data["error"] == "not_found"
        assert "hint" in data
        _assert_generic_error(head, body, tmp_path)
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
        head, body = _head_body(out)
        assert b"401 Unauthorized" in head
        assert b'www-authenticate: bearer realm="devbox"' in head.lower()
        data = json.loads(body)
        assert data["error"] == "unauthorized"
        assert "hint" in data
        _assert_generic_error(head, body, tmp_path)
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


def _static_response(
    payload: bytes, content_type: bytes, extra_headers: tuple[tuple[bytes, bytes], ...] = ()
):
    def respond(conn, method, path, pairs, body):
        head = (
            b"HTTP/1.1 200 OK\r\nContent-Type: "
            + content_type
            + b"\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\n"
        )
        for name, value in extra_headers:
            head += name + b": " + value + b"\r\n"
        head += b"Connection: close\r\n\r\n"
        conn.sendall(head + payload)

    return respond


def test_ingress_rewrites_html_base_href(tmp_path):
    html = b'<html><head><base href="/"><title>t</title></head><body>ok</body></html>'
    fake = FakeUpstream(_static_response(html, b"text/html"))
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port, request("GET", _url(fake.port, b"/x"), headers=[("Authorization", "Bearer tok")])
        )
        head, body = _head_body(out)
        assert f'<base href="/p/{fake.port}/">'.encode() in body
        assert b'<base href="/">' not in body
        assert _content_length(head) == len(body)
        assert b"transfer-encoding" not in head.lower()
    finally:
        fake.close()
        server.shutdown()


def test_ingress_base_href_anchored_to_document_dir(tmp_path):
    html = b'<html><head><base href="/"></head><body>ok</body></html>'
    fake = FakeUpstream(_static_response(html, b"text/html"))
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request(
                "GET",
                _url(fake.port, b"/docs/review/index.html"),
                headers=[("Authorization", "Bearer tok")],
            ),
        )
        head, body = _head_body(out)
        assert f'<base href="/p/{fake.port}/docs/review/">'.encode() in body
        assert _content_length(head) == len(body)
    finally:
        fake.close()
        server.shutdown()


def test_ingress_injects_base_after_head(tmp_path):
    html = b"<html><head><title>t</title></head><body>ok</body></html>"
    fake = FakeUpstream(_static_response(html, b"text/html"))
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port, request("GET", _url(fake.port, b"/x"), headers=[("Authorization", "Bearer tok")])
        )
        head, body = _head_body(out)
        expected = (
            f'<html><head><base href="/p/{fake.port}/"><title>t</title></head><body>ok</body></html>'
        ).encode()
        assert body == expected
        assert _content_length(head) == len(body)
    finally:
        fake.close()
        server.shutdown()


def test_ingress_non_html_unchanged(tmp_path):
    css = b'<base href="/"> body { color: red; }'
    data = b'{"ok":true}'

    def serve(conn, method, path, pairs, body):
        if path.endswith(b".css"):
            payload, ctype = css, b"text/css"
        else:
            payload, ctype = data, b"application/json"
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: "
            + ctype
            + b"\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )

    fake = FakeUpstream(serve)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port, request("GET", _url(fake.port, b"/s.css"), headers=[("Authorization", "Bearer tok")])
        )
        head, body = _head_body(out)
        assert body == css
        assert _content_length(head) == len(css)
        out = raw_request(
            port, request("GET", _url(fake.port, b"/d.json"), headers=[("Authorization", "Bearer tok")])
        )
        head, body = _head_body(out)
        assert body == data
        assert _content_length(head) == len(data)
    finally:
        fake.close()
        server.shutdown()


def test_routed_request_logs_single_access_line(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", _url(fake.port), headers=[("Authorization", "Bearer tok")]))
        assert b"200 OK" in out
        lines = (tmp_path / "access.log").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert lines[0].endswith(f" {fake.port} GET /p/{fake.port}/ok ingress")
    finally:
        fake.close()
        server.shutdown()


def test_ingress_gzip_html_unchanged(tmp_path):
    html = b'<html><head><base href="/"></head><body>ok</body></html>'
    fake = FakeUpstream(_static_response(html, b"text/html", ((b"Content-Encoding", b"gzip"),)))
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port, request("GET", _url(fake.port, b"/x"), headers=[("Authorization", "Bearer tok")])
        )
        head, body = _head_body(out)
        assert body == html
        assert _content_length(head) == len(html)
    finally:
        fake.close()
        server.shutdown()