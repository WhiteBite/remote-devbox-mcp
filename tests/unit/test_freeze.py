import sys

from rdm import freeze, ports, procman
from rdm.profiles import Profile, RunnerCommand


def _profile() -> Profile:
    return Profile(
        project_dir=".",
        runner_commands=(RunnerCommand(name="x", cmd=("npm",)),),
        runner_port=8796,
    )


def test_app_dir_source_mode_points_to_home():
    assert (freeze.app_dir() / "docker-compose.yml").exists()


def test_cli_entry_source_mode_runs_devbox_py():
    entry = freeze.cli_entry()
    assert entry[0] == sys.executable
    assert entry[1].endswith("devbox.py")


def test_spawn_entry_source_mode_routes_through_devbox():
    argv = freeze.spawn_entry("proxy", "--mode", "ingress")
    assert argv[0] == sys.executable
    assert argv[1].endswith("devbox.py")
    assert argv[2:] == ["proxy", "--mode", "ingress"]


def test_spawn_entry_frozen_mode_self_spawns(monkeypatch):
    monkeypatch.setattr(freeze, "FROZEN", True)
    argv = freeze.spawn_entry("runner")
    assert argv == [sys.executable, "runner"]


def test_procman_proxy_argv_routes_through_devbox():
    assert procman.DEFAULT_PROXY_ARGV[1].endswith("devbox.py")
    assert procman.DEFAULT_PROXY_ARGV[-3:] == ["proxy", "--mode", "target"]


def test_with_runner_service_cmd_is_cross_platform():
    service = ports.with_runner_service(_profile()).host_services[-1]
    assert "host/runner-mcp.py" in service.cmd.replace("\\", "/")


def test_with_runner_service_cmd_frozen(monkeypatch):
    monkeypatch.setattr(freeze, "FROZEN", True)
    service = ports.with_runner_service(_profile()).host_services[-1]
    assert f'"{sys.executable}" runner' in service.cmd
