from __future__ import annotations

import json
import re
import socket

from tests.proxy_fakes import FakeUpstream, raw_request, request, responder, start_target


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


def test_target_ok(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_target(tmp_path, target_port=fake.port)
    try:
        out = raw_request(port, request("GET", "/anything", headers=[("Authorization", "Bearer tok")]))
        assert b"200 OK" in out and out.endswith(b"ok")
    finally:
        fake.close()
        server.shutdown()


def test_target_requires_token(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_target(tmp_path, target_port=fake.port)
    try:
        out = raw_request(port, request("GET", "/x"))
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


def test_target_rewrites_host(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_target(tmp_path, target_port=fake.port)
    try:
        raw_request(port, request("GET", "/echo-headers", headers=[("Authorization", "Bearer tok")]))
        assert dict(fake.seen[-1]["headers"])[b"host"] == f"127.0.0.1:{fake.port}".encode()
    finally:
        fake.close()
        server.shutdown()


def test_target_dead_upstream_502(tmp_path):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = int(probe.getsockname()[1])
    probe.close()
    server, port = start_target(tmp_path, target_port=dead_port)
    try:
        out = raw_request(port, request("GET", "/x", headers=[("Authorization", "Bearer tok")]))
        head, body = _head_body(out)
        assert b"502" in head
        data = json.loads(body)
        assert data["error"] == "bad_gateway"
        assert "hint" in data
        assert str(dead_port).encode() not in body
        _assert_generic_error(head, body, tmp_path)
    finally:
        server.shutdown()