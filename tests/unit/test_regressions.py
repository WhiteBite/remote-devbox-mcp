from __future__ import annotations

import json
import os
import pathlib
import socket

import pytest
from rdm import cli, docker, hostos, procman, render, tokens
from rdm.envfile import EnvFile
from rdm.profiles import HostService, Profile, RunnerCommand, Script, load, validate
from rdm.proxy.__main__ import _ports
from rdm.runner.policy import _check_cmd_shim

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


def test_override_directory_mount_uses_tmpfs():
    profile = Profile(deny_mounts=("apps/backend/.env", "secrets_dir"))
    text = render.render_override(profile, {"secrets_dir"})
    assert "- /dev/null:/workspace/apps/backend/.env:ro" in text
    assert "- /workspace/secrets_dir" in text
    assert "/dev/null:/workspace/secrets_dir" not in text


def test_cmd_shim_rejects_cmd_unsafe_arg():
    with pytest.raises(ValueError):
        _check_cmd_shim(["a%b"])
    _check_cmd_shim(["safe", "arg-1"])


def test_manifest_uses_computed_allowlist():
    profile = Profile(host_services=(HostService(port=8792, auth="bearer"),), allowed_ports=(8765,))
    manifest = render.build_manifest(profile, "p", "https://x", "standard", [8765, 8796], {8787, 8792, 8796})
    assert manifest["allowed_ports"] == [8765, 8796]
    assert manifest["endpoints"][0]["name"] == "manifest"
    auth = {entry["port"]: entry["auth"] for entry in manifest["endpoints"]}
    assert auth[8792] == "self"
    assert auth[8796] == "ingress"
    token = {entry["port"]: entry["token"] for entry in manifest["endpoints"]}
    assert token[8787] == "BRIDGE_TOKEN"
    assert token[8792] == "HOST_TOKEN"
    assert token[8796] == "INGRESS_TOKEN"
    assert all(entry["header"] == "Authorization: Bearer" for entry in manifest["endpoints"])


def test_validate_warns_host_shell_operator():
    profile = Profile(host_services=(HostService(port=80, auth="", cmd="a && b"),))
    assert any(problem.startswith("WARN R18") for problem in validate(profile))


def test_with_runner_includes_scripts(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "LOG_ROOT", tmp_path)
    profile = Profile(
        runner_commands=(RunnerCommand(name="t", cmd=("npm", "test"), description="d"),),
        scripts=(Script(name="shots", cmd=("python", "x.py"), description="s"),),
        runner_port=8796,
    )
    try:
        cli._with_runner(profile, "p")
        config = json.loads(pathlib.Path(os.environ["RUNNER_CONFIG"]).read_text(encoding="utf-8"))
    finally:
        os.environ.pop("RUNNER_CONFIG", None)
    assert config["scripts"] == [{"name": "shots", "cmd": ["python", "x.py"], "description": "s"}]


def test_with_runner_emits_command_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "LOG_ROOT", tmp_path)
    profile = Profile(
        runner_commands=(RunnerCommand(name="t", cmd=("npm", "test"), timeout=120),),
        runner_port=8796,
    )
    try:
        cli._with_runner(profile, "p")
        config = json.loads(pathlib.Path(os.environ["RUNNER_CONFIG"]).read_text(encoding="utf-8"))
    finally:
        os.environ.pop("RUNNER_CONFIG", None)
    assert config["commands"][0]["timeout"] == 120


def test_profile_rejects_wrong_shape(tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{"host_services": {"port": 1}}', encoding="utf-8")
    with pytest.raises(ValueError):
        load(path)


def test_envfile_get_strips_render_preserves(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"KEY=  a b  \n")
    env = EnvFile.load(path)
    assert env.get("KEY") == "a b"
    assert env.render() == "KEY=  a b  \n"


def test_profile_ui_port():
    repo = pathlib.Path(__file__).resolve().parents[2]
    assert load(repo / "projects" / "muffin.json").ui_port == 47095