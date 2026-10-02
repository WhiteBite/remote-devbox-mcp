from __future__ import annotations

import json
import subprocess

from rdm import cli


def _profile_json(root, **overrides) -> str:
    data = {
        "project_dir": str(root / "proj"),
        "toolchain": "",
        "git_name": "agent",
        "git_email": "agent@example.test",
        "preview_origin": "http://host.docker.internal:8080",
        "mode": "standard",
        "host_services": [],
        "allowed_ports": [1],
        "deny_mounts": [],
    }
    data.update(overrides)
    return json.dumps(data)


def _setup(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    log_root = tmp_path / "rdm-host"
    monkeypatch.setattr(cli, "PROJECTS_DIR", projects)
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(cli, "OVERRIDE_FILE", tmp_path / "docker-compose.override.yml")
    monkeypatch.setattr(cli, "LOG_ROOT", log_root)
    monkeypatch.setattr(cli, "MANIFEST_PATH", log_root / "rdm-manifest.json")
    monkeypatch.setattr(cli, "COMPOSE_FILE", str(tmp_path / "docker-compose.yml"))
    (tmp_path / "proj").mkdir()

    calls: dict[str, list] = {"compose": [], "restart": [], "stop": [], "ingress": [], "docker_run": [], "gitleaks": []}

    def fake_compose(*args, **kwargs):
        calls["compose"].append(args)
        return subprocess.CompletedProcess(["docker"], 0, stdout="", stderr="")

    monkeypatch.setattr(cli.docker, "compose", fake_compose)
    monkeypatch.setattr(cli.docker, "compose_ps", lambda *a, **k: "rdm-toolbox healthy")
    monkeypatch.setattr(cli.docker, "run", lambda *a, **k: calls["docker_run"].append(a) or subprocess.CompletedProcess(["docker"], 0, stdout="", stderr=""))
    monkeypatch.setattr(cli.procman, "restart_host_services", lambda *a, **k: calls["restart"].append(a) or [])
    monkeypatch.setattr(cli.procman, "stop_host_services", lambda *a, **k: calls["stop"].append(a))
    monkeypatch.setattr(cli.procman, "stop_ingress", lambda *a, **k: calls["ingress"].append("stop"))
    monkeypatch.setattr(cli.procman, "start_ingress", lambda *a, **k: calls["ingress"].append("start") or 1)
    monkeypatch.setattr(cli, "_start_gitleaks", lambda *a, **k: calls["gitleaks"].append(a))
    return projects, calls


def test_use_applies_profile_and_single_restart(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["use", "p1"]) == 0
    assert len(calls["restart"]) == 1
    assert calls["ingress"] == ["stop", "start"]
    assert ("up", "-d", "--force-recreate", "toolbox") in calls["compose"]
    assert (cli.ENV_FILE).read_text(encoding="utf-8").count("ACTIVE_PROFILE=p1") == 1


def test_use_rejects_invalid_profile_exit_1(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    (projects / "bad.json").write_text(_profile_json(tmp_path, project_dir=str(tmp_path / "missing")), encoding="utf-8")
    assert cli.main(["use", "bad"]) == 1
    assert calls["restart"] == []


def test_use_rejects_malformed_json_exit_1(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    (projects / "broken.json").write_text("{ not json", encoding="utf-8")
    assert cli.main(["use", "broken"]) == 1
    assert calls["restart"] == []


def test_use_preserves_env_comments(monkeypatch, tmp_path):
    projects, _ = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("# keep me\nACTIVE_PROFILE=\n", encoding="utf-8")
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["use", "p1"]) == 0
    assert "# keep me" in cli.ENV_FILE.read_text(encoding="utf-8")


def test_status_lists_services(monkeypatch, tmp_path, capsys):
    _, calls = _setup(monkeypatch, tmp_path)
    assert cli.main(["status"]) == 0
    assert ("ps", "--format", "{{.Name}} {{.Status}}") in calls["compose"]


def test_stop_host_calls_procman(monkeypatch, tmp_path):
    _, calls = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("ACTIVE_PROFILE=foo\n", encoding="utf-8")
    assert cli.main(["stop-host"]) == 0
    assert calls["stop"] == [("foo",)]


def test_info_masked_vs_share_full(monkeypatch, tmp_path, capsys):
    _, _ = _setup(monkeypatch, tmp_path)
    token = "a" * 64
    cli.ENV_FILE.write_text(
        f"MCP_BEARER_TOKEN={token}\nMCP_PUBLIC_TOKEN={'b' * 64}\nINGRESS_TOKEN={'c' * 64}\nPUBLIC_URL=https://x.example\n",
        encoding="utf-8",
    )
    assert cli.main(["info"]) == 0
    masked = capsys.readouterr().out
    assert token not in masked and "aaaa...aaaa" in masked
    assert cli.main(["share"]) == 0
    full = capsys.readouterr().out
    assert token in full


def test_profile_show_muffin(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "PROJECTS_DIR", cli.HOME_DIR.parent / "projects")
    assert cli.main(["profile", "show", "muffin"]) == 0
    assert "project_dir" in capsys.readouterr().out


def test_issue_tokens_rotates_three(monkeypatch, tmp_path):
    _, _ = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text(
        "MCP_BEARER_TOKEN=" + "0" * 64 + "\nMCP_PUBLIC_TOKEN=" + "0" * 64 + "\nINGRESS_TOKEN=" + "0" * 64 + "\n",
        encoding="utf-8",
    )
    assert cli.main(["issue-tokens"]) == 0
    text = cli.ENV_FILE.read_text(encoding="utf-8")
    assert text.count("0" * 64) == 0
    assert text.count("MCP_BEARER_TOKEN=") == 1


def test_start_prints_handoff_block(monkeypatch, tmp_path, capsys):
    projects, calls = _setup(monkeypatch, tmp_path)
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["start", "p1"]) == 0
    out = capsys.readouterr().out
    assert "Репозиторий: https://github.com/WhiteBite/remote-devbox-mcp" in out
    assert "Скилл + инструкция:" in out
    assert "INGRESS=" in out and "BRIDGE_TOKEN=" in out
    assert "(хост) профили:" in out


def test_start_with_preview_starts_preview_profile(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["start", "p1", "--preview"]) == 0
    assert any("--profile" in args and "preview" in args for args in calls["compose"])


def test_preview_sets_origin(monkeypatch, tmp_path):
    _, calls = _setup(monkeypatch, tmp_path)
    assert cli.main(["preview", "http://host.docker.internal:8080"]) == 0
    assert "PREVIEW_ORIGIN=http://host.docker.internal:8080" in cli.ENV_FILE.read_text(encoding="utf-8")
    assert any("cloudflared-preview" in args for args in calls["compose"])


def test_block_and_url_subcommands(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text(
        "PUBLIC_URL=https://devbox.example.test\n"
        "MCP_BEARER_TOKEN=" + "a" * 64 + "\n"
        "MCP_PUBLIC_TOKEN=" + "b" * 64 + "\n"
        "INGRESS_TOKEN=" + "c" * 64 + "\n",
        encoding="utf-8",
    )
    assert cli.main(["block", "--masked"]) == 0
    assert "BRIDGE_TOKEN=" in capsys.readouterr().out
    assert cli.main(["url"]) == 0
    assert capsys.readouterr().out.strip() == "https://devbox.example.test"


def test_preview_url_prefers_named_tunnel(monkeypatch, tmp_path, capsys):
    _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("PUBLIC_PREVIEW_URL=https://preview.example.test\n", encoding="utf-8")
    assert cli.main(["url", "--preview"]) == 0
    assert capsys.readouterr().out.strip() == "https://preview.example.test"


def test_down_stops_host_ingress_and_tunnels(monkeypatch, tmp_path):
    _, calls = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("ACTIVE_PROFILE=p1\n", encoding="utf-8")
    assert cli.main(["down"]) == 0
    assert calls["stop"] == [("p1",)]
    assert "stop" in calls["ingress"]
    assert any("cloudflared-ingress" in args for args in calls["compose"])


def test_health_reflects_stack(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("PUBLIC_URL=https://devbox.example.test\n", encoding="utf-8")
    monkeypatch.setattr(cli.netprobe, "probe_http", lambda url, token, timeout=8.0: 200)
    assert cli.main(["health"]) == 0
    monkeypatch.setattr(cli.netprobe, "probe_http", lambda url, token, timeout=8.0: 530)
    assert cli.main(["health"]) == 1


def test_start_preview_skipped_with_named_preview(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("PUBLIC_PREVIEW_URL=https://preview.example.test\n", encoding="utf-8")
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["start", "p1", "--preview"]) == 0
    assert not any("--profile" in args for args in calls["compose"])


def test_allow_opens_port_and_sets_ui(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("ACTIVE_PROFILE=p1\n", encoding="utf-8")
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["allow", "12345", "--ui"]) == 0
    data = json.loads((projects / "p1.json").read_text(encoding="utf-8"))
    assert 12345 in data["allowed_ports"]
    assert data["ui_port"] == 12345
    assert "12345" in cli.ENV_FILE.read_text(encoding="utf-8")
    assert "start" in calls["ingress"]


def _runner_profile_json(root, **overrides) -> str:
    data = json.loads(_profile_json(root))
    data["runner_commands"] = [{"name": "build", "cmd": ["npm", "run", "build"]}]
    data["runner_port"] = 8796
    data.update(overrides)
    return json.dumps(data)


def _env_value(text: str, key: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return ""


def test_allow_keeps_runner_auth_proxy_in_self_authed(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("ACTIVE_PROFILE=p1\n", encoding="utf-8")
    (projects / "p1.json").write_text(_runner_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["allow", "9000"]) == 0
    text = cli.ENV_FILE.read_text(encoding="utf-8")
    assert "8796" in _env_value(text, "SELF_AUTHED_PORTS").split(",")
    assert "9000" in _env_value(text, "ALLOWED_PORTS").split(",")
    manifest = json.loads(cli.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert 8796 in {entry["port"] for entry in manifest["endpoints"]}


def test_allow_rejects_naked_runner_http_port(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("ACTIVE_PROFILE=p1\n", encoding="utf-8")
    (projects / "p1.json").write_text(_runner_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["allow", "8797"]) == 1
    data = json.loads((projects / "p1.json").read_text(encoding="utf-8"))
    assert 8797 not in data["allowed_ports"]
    assert calls["ingress"] == []


def test_allow_rejects_port_out_of_range(monkeypatch, tmp_path):
    projects, _ = _setup(monkeypatch, tmp_path)
    cli.ENV_FILE.write_text("ACTIVE_PROFILE=p1\n", encoding="utf-8")
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    for bad in (0, -1, 65536, 99999):
        assert cli.main(["allow", str(bad)]) == 1
    data = json.loads((projects / "p1.json").read_text(encoding="utf-8"))
    assert data["allowed_ports"] == [1]


def test_use_fails_when_compose_up_fails(monkeypatch, tmp_path):
    projects, _ = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli.docker, "compose",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 1, "", "compose failed"),
    )
    (projects / "p1.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["use", "p1"]) == 1


def test_use_fails_when_agent_artifacts_fail(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli.docker, "run",
        lambda *a, **k: subprocess.CompletedProcess(["docker"], 1, "", "docker: error"),
    )
    (projects / "p1.json").write_text(_profile_json(tmp_path, toolchain="node22"), encoding="utf-8")
    assert cli.main(["use", "p1"]) == 1
    assert calls["gitleaks"] == []


def test_use_rejects_profile_name_with_shell_characters(monkeypatch, tmp_path):
    projects, calls = _setup(monkeypatch, tmp_path)
    (projects / "bad name.json").write_text(_profile_json(tmp_path), encoding="utf-8")
    assert cli.main(["use", "bad name"]) == 1
    assert calls["restart"] == []