import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from rdm import profiles
from rdm.gateway.snapshot import (
    GatewaySnapshotError,
    build,
    load,
    load_verified,
    save,
    save_snapshot,
    snapshot_path,
    verify,
)
from rdm.profiles import ExposedMcp, Profile


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _local(command: list, environment: dict | None = None, enabled: bool | None = None) -> dict:
    entry: dict = {"type": "local", "command": command}
    if environment is not None:
        entry["environment"] = environment
    if enabled is not None:
        entry["enabled"] = enabled
    return entry


def _profile(project_dir: Path, *names: str) -> Profile:
    return Profile(project_dir=str(project_dir), exposed_mcps=tuple(ExposedMcp(name=name) for name in names))


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return project


def _global_config(tmp_path: Path) -> Path:
    return tmp_path / "global-opencode.json"


def _build(tmp_path: Path, mcp: dict, *exposed: str, layer: str = "opencode.json"):
    project = _project(tmp_path)
    _write_json(project / layer, {"mcp": mcp})
    return build(_profile(project, *exposed), global_config=_global_config(tmp_path))


def _server_of(snapshot, name: str):
    return dict(snapshot.servers)[name]


def _saved_snapshot_file(tmp_path: Path) -> Path:
    snapshot = _build(tmp_path, {"srv": _local(["npx", "-y", "pkg@1.0.0"])}, "srv")
    path = tmp_path / "p.mcpsnap.json"
    save(snapshot, path)
    return path


def test_build_merges_global_and_project_layers_project_wins(tmp_path):
    global_config = _global_config(tmp_path)
    _write_json(
        global_config,
        {
            "mcp": {
                "shared": _local(["npx", "-y", "shared@1.0.0"]),
                "global-only": _local(["npx", "-y", "global-only@1.0.0"]),
            }
        },
    )
    project = _project(tmp_path)
    _write_json(
        project / "opencode.json",
        {
            "mcp": {
                "shared": _local(["npx", "-y", "shared@2.0.0"]),
                "project-only": _local(["npx", "-y", "project-only@1.0.0"]),
            }
        },
    )

    snapshot = build(_profile(project, "shared", "global-only", "project-only"), global_config=global_config)

    assert _server_of(snapshot, "shared").args == ("-y", "shared@2.0.0")
    assert _server_of(snapshot, "global-only").args == ("-y", "global-only@1.0.0")
    assert _server_of(snapshot, "project-only").args == ("-y", "project-only@1.0.0")


def test_build_opencode_dir_layer_overrides_project_root(tmp_path):
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "srv@1.0.0"])}})
    _write_json(project / ".opencode" / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "srv@2.0.0"])}})

    snapshot = build(_profile(project, "srv"), global_config=_global_config(tmp_path))

    assert _server_of(snapshot, "srv").args == ("-y", "srv@2.0.0")


def test_project_layer_replaces_remote_global_entry(tmp_path):
    global_config = _global_config(tmp_path)
    _write_json(global_config, {"mcp": {"srv": {"type": "remote", "url": "https://mcp.example.com/mcp"}}})
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "srv@1.0.0"])}})

    snapshot = build(_profile(project, "srv"), global_config=global_config)

    assert _server_of(snapshot, "srv").command == "npx"


def test_build_ignores_servers_that_are_not_exposed(tmp_path):
    mcp = {
        "remote-one": {"type": "remote", "url": "https://mcp.example.com"},
        "unpinned": _local(["npx", "-y", "no-version"]),
        "exposed": _local(["npx", "-y", "exposed@1.0.0"]),
    }

    snapshot = _build(tmp_path, mcp, "exposed")

    assert set(dict(snapshot.servers)) == {"exposed"}


def test_build_rejects_remote_entry(tmp_path):
    mcp = {"srv": {"type": "remote", "url": "https://mcp.example.com/mcp", "enabled": True}}

    with pytest.raises(GatewaySnapshotError, match="remote MCP not supported in P1"):
        _build(tmp_path, mcp, "srv")


def test_build_rejects_missing_type(tmp_path):
    with pytest.raises(GatewaySnapshotError, match="type"):
        _build(tmp_path, {"srv": {"command": ["npx", "-y", "srv@1.0.0"]}}, "srv")


def test_build_rejects_command_not_an_array(tmp_path):
    with pytest.raises(GatewaySnapshotError, match="command"):
        _build(tmp_path, {"srv": {"type": "local", "command": "npx -y srv@1.0.0"}}, "srv")


def test_build_rejects_unpinned_npx(tmp_path):
    with pytest.raises(GatewaySnapshotError, match=r"unpinned-pkg@"):
        _build(tmp_path, {"srv": _local(["npx", "-y", "unpinned-pkg"])}, "srv")


def test_build_rejects_unpinned_scoped_package(tmp_path):
    with pytest.raises(GatewaySnapshotError, match=r"@scope/pkg@"):
        _build(tmp_path, {"srv": _local(["npx", "-y", "@scope/pkg"])}, "srv")


def test_build_rejects_unpinned_uvx(tmp_path):
    with pytest.raises(GatewaySnapshotError, match="unpinned"):
        _build(tmp_path, {"srv": _local(["uvx", "mcp-server-fetch"])}, "srv")


def test_build_rejects_exposed_server_missing_from_config(tmp_path):
    with pytest.raises(GatewaySnapshotError, match="missing or disabled"):
        _build(tmp_path, {}, "ghost")


def test_build_treats_disabled_server_as_missing(tmp_path):
    with pytest.raises(GatewaySnapshotError, match="missing or disabled"):
        _build(tmp_path, {"srv": _local(["npx", "-y", "srv@1.0.0"], enabled=False)}, "srv")


def test_build_rejects_malformed_layer_json(tmp_path):
    global_config = _global_config(tmp_path)
    global_config.parent.mkdir(exist_ok=True)
    global_config.write_text("{not json", encoding="utf-8")
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "srv@1.0.0"])}})

    with pytest.raises(GatewaySnapshotError, match="not valid JSON"):
        build(_profile(project, "srv"), global_config=global_config)


def test_build_pins_specifier_and_sha256_over_command_args_env(tmp_path):
    snapshot = _build(tmp_path, {"srv": _local(["npx", "-y", "pkg@1.0.0"], {"API": "secret"})}, "srv")

    server = _server_of(snapshot, "srv")
    assert server.command == "npx"
    assert server.args == ("-y", "pkg@1.0.0")
    assert server.env == (("API", "secret"),)
    assert server.pins.specifier == "pkg@1.0.0"
    expected = hashlib.sha256(
        json.dumps(
            {"args": ["-y", "pkg@1.0.0"], "command": "npx", "env": {"API": "secret"}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert server.pins.sha256 == expected


def test_build_pins_scoped_and_path_commands(tmp_path):
    snapshot = _build(
        tmp_path,
        {
            "scoped": _local(["npx", "-y", "@scope/pkg@1.3.0"]),
            "pathrun": _local([r"C:\py\python.exe", "server.py"]),
        },
        "scoped",
        "pathrun",
    )

    assert _server_of(snapshot, "scoped").pins.specifier == "@scope/pkg@1.3.0"
    assert _server_of(snapshot, "pathrun").pins.specifier == r"C:\py\python.exe"


def test_pin_sha256_changes_with_every_component(tmp_path):
    base = _server_of(_build(tmp_path, {"srv": _local(["npx", "-y", "pkg@1.0.0"], {"A": "1"})}, "srv"), "srv")

    changed = [
        _server_of(_build(tmp_path, {"srv": _local(["npx", "-y", "pkg@1.1.0"], {"A": "1"})}, "srv"), "srv"),
        _server_of(_build(tmp_path, {"srv": _local(["npx", "-y", "pkg@1.0.0"], {"A": "2"})}, "srv"), "srv"),
        _server_of(_build(tmp_path, {"srv": _local(["bunx", "pkg@1.0.0"], {"A": "1"})}, "srv"), "srv"),
    ]

    assert all(server.pins.sha256 != base.pins.sha256 for server in changed)


def test_verify_accepts_unchanged_config(tmp_path):
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"])}})
    profile = _profile(project, "srv")
    global_config = _global_config(tmp_path)

    snapshot = build(profile, global_config=global_config)

    verify(snapshot, profile, global_config=global_config)


def test_verify_refuses_drift_in_args(tmp_path):
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"])}})
    profile = _profile(project, "srv")
    global_config = _global_config(tmp_path)
    snapshot = build(profile, global_config=global_config)

    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@2.0.0"])}})

    with pytest.raises(GatewaySnapshotError, match="drift"):
        verify(snapshot, profile, global_config=global_config)


def test_verify_refuses_drift_in_env(tmp_path):
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"], {"A": "1"})}})
    profile = _profile(project, "srv")
    global_config = _global_config(tmp_path)
    snapshot = build(profile, global_config=global_config)

    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"], {"A": "2"})}})

    with pytest.raises(GatewaySnapshotError, match="drift"):
        verify(snapshot, profile, global_config=global_config)


def test_verify_refuses_drift_when_server_removed_from_config(tmp_path):
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"])}})
    profile = _profile(project, "srv")
    global_config = _global_config(tmp_path)
    snapshot = build(profile, global_config=global_config)

    _write_json(project / "opencode.json", {})

    with pytest.raises(GatewaySnapshotError):
        verify(snapshot, profile, global_config=global_config)


def test_verify_refuses_newly_exposed_server_absent_from_snapshot(tmp_path):
    project = _project(tmp_path)
    _write_json(
        project / "opencode.json",
        {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"]), "other": _local(["npx", "-y", "other@1.0.0"])}},
    )
    global_config = _global_config(tmp_path)
    snapshot = build(_profile(project, "srv"), global_config=global_config)

    with pytest.raises(GatewaySnapshotError, match="missing from the policy snapshot"):
        verify(snapshot, _profile(project, "srv", "other"), global_config=global_config)


def test_snapshot_path_sits_beside_profile_json():
    assert snapshot_path(Path("/profiles/foo.json")) == Path("/profiles/foo.mcpsnap.json")


def test_save_snapshot_persists_beside_profile_json(tmp_path):
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"])}})
    profile = _profile(project, "srv")
    profile_path = tmp_path / "foo.json"

    save_snapshot(profile_path, profile, global_config=_global_config(tmp_path))

    assert (tmp_path / "foo.mcpsnap.json").is_file()


def test_snapshot_lands_in_profiles_find_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"])}})
    _write_json(tmp_path / "myproj.json", {"project_dir": str(project), "exposed_mcps": [{"name": "srv"}]})

    profile_path = profiles.find("myproj")
    assert profile_path == tmp_path / "myproj.json"
    save_snapshot(profile_path, profiles.load(profile_path), global_config=_global_config(tmp_path))

    assert (tmp_path / "myproj.mcpsnap.json").is_file()


def test_env_values_roundtrip_verbatim(tmp_path):
    env = {"TOKEN": "sk-abc=def ghi+jkl", "NOTE": "значение ✓"}
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"], env)}})
    snapshot = build(_profile(project, "srv"), global_config=_global_config(tmp_path))
    path = tmp_path / "s.mcpsnap.json"

    save(snapshot, path)

    assert dict(_server_of(load(path), "srv").env) == env


def test_load_rejects_invalid_json(tmp_path):
    path = tmp_path / "s.mcpsnap.json"
    path.write_text("{oops", encoding="utf-8")

    with pytest.raises(GatewaySnapshotError):
        load(path)


def test_load_rejects_snapshot_without_servers(tmp_path):
    path = tmp_path / "s.mcpsnap.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(GatewaySnapshotError, match="servers"):
        load(path)


def test_load_rejects_unknown_server_fields(tmp_path):
    path = _saved_snapshot_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["servers"]["srv"]["cwd"] = "/tmp"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(GatewaySnapshotError):
        load(path)


def test_load_rejects_tampered_pin_digest(tmp_path):
    path = _saved_snapshot_file(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["servers"]["srv"]["args"] = ["-y", "pkg@9.9.9"]
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(GatewaySnapshotError, match="integrity"):
        load(path)


def test_load_verified_missing_snapshot_with_exposed_refuses(tmp_path):
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"])}})
    profile = _profile(project, "srv")

    with pytest.raises(GatewaySnapshotError, match="missing"):
        load_verified(tmp_path / "foo.json", profile, global_config=_global_config(tmp_path))


def test_load_verified_missing_snapshot_without_exposed_is_empty(tmp_path):
    profile = Profile(project_dir=str(_project(tmp_path)))

    snapshot = load_verified(tmp_path / "foo.json", profile, global_config=_global_config(tmp_path))

    assert snapshot.servers == ()


def test_load_verified_roundtrip_and_drift_refusal(tmp_path):
    project = _project(tmp_path)
    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@1.0.0"])}})
    profile = _profile(project, "srv")
    profile_path = tmp_path / "foo.json"
    global_config = _global_config(tmp_path)
    save_snapshot(profile_path, profile, global_config=global_config)

    load_verified(profile_path, profile, global_config=global_config)

    _write_json(project / "opencode.json", {"mcp": {"srv": _local(["npx", "-y", "pkg@2.0.0"])}})
    with pytest.raises(GatewaySnapshotError, match="drift"):
        load_verified(profile_path, profile, global_config=global_config)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_save_sets_owner_only_mode_on_posix(tmp_path):
    path = _saved_snapshot_file(tmp_path)

    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "nt", reason="icacls owner-only ACL")
def test_save_grants_owner_only_acl_on_windows(tmp_path):
    path = _saved_snapshot_file(tmp_path)

    listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    user = subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip()
    ace_lines = [line for line in listing.splitlines() if ":(" in line]
    assert len(ace_lines) == 1
    assert user.lower() in ace_lines[0].lower()
    assert "(F)" in ace_lines[0]
