from __future__ import annotations

import os
import socket

import pytest

from rdm import docker, hostos, procman, tokens
from rdm.proxy.__main__ import _ports
from tests.proxy_fakes import FakeUpstream, raw_request, request, responder, start_ingress


def _auth() -> list[tuple[str, str]]:
    return [("Authorization", "Bearer tok")]


def test_truncated_request_body_is_rejected(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                (
                    f"POST /p/{fake.port}/echo-body HTTP/1.1\r\nHost: t\r\n"
                    "Authorization: Bearer tok\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhel"
                ).encode("latin-1")
            )
            sock.shutdown(socket.SHUT_WR)
            out = b""
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                out += data
        assert b"400" in out
        assert fake.seen == []
    finally:
        fake.close()
        server.shutdown()


def test_truncated_upstream_response_does_not_hang(tmp_path):
    fake = FakeUpstream(responder)
    server, port = start_ingress(tmp_path, allowed={fake.port})
    try:
        out = raw_request(
            port,
            request("GET", f"/p/{fake.port}/truncated", headers=_auth(), connection="keep-alive"),
        )
        assert b"content-length: 10" in out.lower()
        assert out.endswith(b"abc")
    finally:
        fake.close()
        server.shutdown()


def test_resolve_wraps_cmd_shim(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows cmd shim resolution")
    monkeypatch.setattr(hostos.shutil, "which", lambda name: r"C:\tools\npm.cmd" if name == "npm" else None)
    resolved = hostos._resolve(["npm", "install", "pkg"])
    assert resolved[:2] == ["cmd", "/c"]
    assert resolved[2].lower().endswith("npm.cmd")
    assert resolved[3:] == ["install", "pkg"]


def test_spawn_missing_binary_raises_oserror():
    with pytest.raises(OSError):
        hostos.spawn(["definitely-not-a-real-binary-xyz-123", "--version"])


def test_cmdline_matches_empty_marker_is_false():
    assert hostos.cmdline_matches(os.getpid(), "") is False


def test_docker_absent_returns_127(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(docker.subprocess, "run", boom)
    assert docker.compose("ps").returncode == 127
    assert docker.run("info").returncode == 127


def test_ports_guard_exits_on_garbage():
    with pytest.raises(SystemExit):
        _ports("80,http")


def test_chat_block_missing_tokens():
    assert "BRIDGE_TOKEN=..." in tokens.chat_block({}, "https://x", False)


def test_stop_ingress_uses_identity_guard(monkeypatch, tmp_path):
    killed: list[int] = []
    monkeypatch.setattr(hostos, "tempdir", lambda: tmp_path)
    monkeypatch.setattr(hostos, "cmdline_matches", lambda pid, marker: False)
    monkeypatch.setattr(hostos, "create_time", lambda pid: 999.0)
    monkeypatch.setattr(hostos, "kill_tree", lambda pid: killed.append(pid))
    directory = tmp_path / "rdm-ingress"
    directory.mkdir()
    (directory / "pids.txt").write_text("4242|111.0|rdm.proxy\n", encoding="utf-8")
    procman.stop_ingress()
    assert killed == []