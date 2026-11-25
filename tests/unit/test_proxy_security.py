from __future__ import annotations

import socket
import threading

from rdm.proxy import server as proxy_server
from rdm.proxy import upstream

from tests.proxy_fakes import FakeUpstream, raw_request, request, responder, start_ingress


def _seen_headers(fake: FakeUpstream) -> dict[bytes, bytes]:
    return dict(fake.seen[-1]["headers"])


def test_authorization_stripped_for_plain_ports(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(port, request("GET", f"/p/{fake.port}/ok", headers=[("Authorization", "Bearer tok")]))
        assert b"200 OK" in out
        assert b"authorization" not in _seen_headers(fake)
    finally:
        fake.close()
        server.shutdown()


def test_authorization_kept_for_self_authed_ports(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, self_authed={fake.port})
    try:
        raw_request(port, request("GET", f"/p/{fake.port}/ok", headers=[("Authorization", "Bearer svc")]))
        assert _seen_headers(fake)[b"authorization"] == b"Bearer svc"
    finally:
        fake.close()
        server.shutdown()


def test_client_x_forwarded_for_replaced():
    pairs = [(b"x-forwarded-for", b"6.6.6.6"), (b"accept", b"*/*")]
    for fn in (upstream.filter_forward_headers, upstream.filter_upgrade_headers):
        out = fn(pairs, b"h:1", "10.0.0.1", b"https")
        assert [v for n, v in out if n == b"x-forwarded-for"] == [b"10.0.0.1"]


def test_unauthenticated_oversized_body_rejected_before_read(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_server, "MAX_UNAUTHED_BODY", 10)
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, self_authed={fake.port})
    try:
        out = raw_request(
            port, request("POST", f"/p/{fake.port}/echo-body", headers=[("Content-Length", "1000")]), timeout=5
        )
        assert b"413" in out.split(b"\r\n", 1)[0]
        assert fake.seen == []
    finally:
        fake.close()
        server.shutdown()


def test_unauthorized_does_not_wait_for_body(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port, request("POST", f"/p/{fake.port}/ok", headers=[("Content-Length", "100000")]), timeout=5
        )
        assert b"401" in out.split(b"\r\n", 1)[0]
    finally:
        fake.close()
        server.shutdown()


def test_slow_header_dribble_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr(proxy_server, "HEADER_TIMEOUT", 0.3)
    server, port = start_ingress(tmp_path)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            closed = False
            for _ in range(40):
                try:
                    sock.sendall(b"X")
                    sock.settimeout(0.05)
                    if sock.recv(1) == b"":
                        closed = True
                        break
                except TimeoutError:
                    continue
                except OSError:
                    closed = True
                    break
            assert closed
    finally:
        server.shutdown()


def test_tunnel_shuts_down_both_sides():
    client_a, client_b = socket.socketpair()
    up_a, up_b = socket.socketpair()
    done = threading.Event()

    def run():
        proxy_server._Handler._tunnel(client_a, up_a)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    client_b.sendall(b"ping")
    assert up_b.recv(4) == b"ping"
    up_b.close()
    assert done.wait(10), "tunnel must exit after one side closes"
    client_b.settimeout(2)
    try:
        assert client_b.recv(1) == b""
    except ConnectionError:
        pass
    client_b.close()
