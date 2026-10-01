from __future__ import annotations

import socket

from tests.proxy_fakes import FakeUpstream, raw_request, request, responder, start_target


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
        assert b"401 Unauthorized" in out
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
        assert b"502" in out
    finally:
        server.shutdown()