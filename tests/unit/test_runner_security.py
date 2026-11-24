import importlib
import json
import os

import pytest
from rdm import hostos, ports, profiles


def _main():
    return importlib.import_module("rdm.runner.__main__")


def _ctx(main, tmp_path):
    return main._Ctx(cwd=tmp_path, run_dir=tmp_path, profile="p", default_timeout=60)


@pytest.mark.parametrize("value", ["/etc/passwd", "../x", "a/../../b", "\\Windows\\x", "C:x", "C:\\x"])
def test_safe_path_rejects_escape(value):
    if os.name != "nt" and value in ("\\Windows\\x", "C:x", "C:\\x"):
        pytest.skip("Windows path semantics")
    with pytest.raises(ValueError):
        _main()._safe_path(value)


@pytest.mark.parametrize("value", ["src/a.py", "a", "dir/sub/file.txt"])
def test_safe_path_accepts_relative(value):
    assert _main()._safe_path(value)


def test_public_commands_hides_env_and_cmd():
    cfg = {
        "commands": [{"Name": "dev", "Cmd": ["npm", "run"], "Env": {"TOKEN": "s3"}, "Port": 3000}],
        "scripts": [{"name": "lint", "cmd": ["ruff"], "description": "d"}],
    }
    out = _main()._public_commands(cfg)
    assert [item["kind"] for item in out] == ["command", "script"]
    dumped = json.dumps(out)
    assert "s3" not in dumped and "TOKEN" not in dumped and "npm" not in dumped
    assert out[0]["port"] == 3000 and out[1]["description"] == "d"


@pytest.mark.parametrize("value", ["--upload-pack=evil", "-o", "-"])
def test_dash_arg_rejected_by_default(value):
    spec = {"cmd": ["git", "fetch"], "args": {"ref": {"type": "str"}}}
    with pytest.raises(ValueError, match="-"):
        _main()._build_argv(spec, {"ref": value})


def test_dash_arg_allowed_with_flag():
    spec = {"cmd": ["pytest"], "args": {"k": {"type": "str", "allow_dash": True}}}
    assert _main()._build_argv(spec, {"k": "-x"}) == ["pytest", "-x"]


def test_dash_check_applies_to_template_position():
    spec = {"cmd": ["tool", "{t}"], "args": {"t": {"position": "template"}}}
    with pytest.raises(ValueError):
        _main()._build_argv(spec, {"t": "--evil"})


def test_profile_parses_allow_dash():
    assert profiles._argspec({"AllowDash": True}).allow_dash is True
    assert profiles._argspec({}).allow_dash is False
    with pytest.raises(ValueError):
        profiles._argspec({"allow_dash": "yes"})


def test_running_pid_missing_file(tmp_path):
    assert _main()._running_pid(tmp_path / "none.pid") is None


@pytest.mark.parametrize("content", ["garbage", "x|1|m", "12|1"])
def test_running_pid_corrupt_file_removed(tmp_path, content):
    pidfile = tmp_path / "a.pid"
    pidfile.write_text(content, encoding="utf-8")
    assert _main()._running_pid(pidfile) is None
    assert not pidfile.exists()


def test_running_pid_stale_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(hostos, "owned", lambda *_: False)
    pidfile = tmp_path / "a.pid"
    pidfile.write_text("4242|1.0|marker", encoding="utf-8")
    assert _main()._running_pid(pidfile) is None
    assert not pidfile.exists()


def test_running_pid_alive(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(hostos, "owned", lambda pid, ct, marker: seen.append((pid, ct, marker)) or True)
    pidfile = tmp_path / "a.pid"
    pidfile.write_text("4242|1.5|npm run dev", encoding="utf-8")
    assert _main()._running_pid(pidfile) == 4242
    assert seen == [(4242, 1.5, "npm run dev")]


def test_background_restart_refused(tmp_path, monkeypatch):
    main = _main()
    monkeypatch.setattr(hostos, "owned", lambda *_: True)
    (tmp_path / "p-dev.pid").write_text("777|1.0|m", encoding="utf-8")
    monkeypatch.setattr(main.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    tool = main._make_tool("dev", {"cmd": ["python", "-V"], "background": True}, _ctx(main, tmp_path))
    assert json.loads(tool()) == {"started": False, "pid": 777, "reason": "уже запущен; сначала вызови kill-тул"}


def test_background_marker_for_cmd_shim_is_whole_string(tmp_path, monkeypatch):
    main = _main()
    shim = 'cmd /c ""C:\\t\\npm.cmd" run dev"'
    monkeypatch.setattr(main, "_resolve", lambda argv, cwd: shim)

    class FakeProc:
        pid = 31337

        def poll(self):
            return None

    monkeypatch.setattr(main.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(hostos, "create_time", lambda pid: 2.0)
    tool = main._make_tool("dev", {"cmd": ["npm", "run", "dev"], "background": True}, _ctx(main, tmp_path))
    assert json.loads(tool())["started"] is True
    assert (tmp_path / "p-dev.pid").read_text(encoding="utf-8") == f"31337|2.0|{shim}"


def test_transport_security_allowlists_loopback_only():
    settings = _main()._transport_security(8797)
    assert settings.enable_dns_rebinding_protection is True
    assert set(settings.allowed_hosts) == {"127.0.0.1:8797", "localhost:8797"}
    assert all(o.startswith(("http://127.0.0.1:", "http://localhost:")) for o in settings.allowed_origins)


def test_runner_http_port_never_opened_by_ingress_range():
    policy = ports.PortPolicy(
        self_authed=(ports.DEFAULT_RUNNER_PORT,), allowed=(ports.DEFAULT_RUNNER_PORT + 1,),
        ranges=((8000, 9000),),
    )
    assert ports.is_port_allowed(policy, ports.DEFAULT_RUNNER_PORT + 1) is False
    assert ports.is_port_allowed(policy, 8500) is True
