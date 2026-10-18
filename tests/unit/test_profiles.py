import json
from pathlib import Path

import pytest
from rdm import profiles
from rdm.gateway.data import (
    ARG_CONSTRAINTS,
    DEFAULT_DENY_SERVERS,
    META_TOOL_DENY,
    NETWORK_EGRESS_SERVERS,
    SECRET_TOUCHING_SERVERS,
)
from rdm.profiles import ExposedMcp, Profile, load

REPO = Path(__file__).resolve().parents[2]
PROJECTS = REPO / "projects"


def _valid(tmp_path, **overrides):
    data = dict(
        project_dir=str(tmp_path),
        git_name="agent",
        git_email="agent@example.test",
        preview_origin="http://host.docker.internal:8080",
    )
    data.update(overrides)
    return Profile(**data)


def test_load_rejects_unknown_key(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"project_dir": "x", "bogus": 1}), encoding="utf-8")

    with pytest.raises(ValueError):
        load(path)


def test_load_example_service_json(tmp_path):
    path = tmp_path / "example-service.json"
    path.write_text(
        json.dumps(
            {
                "project_dir": "d:/work/example-service",
                "toolchain": "node22",
                "git_name": "agent",
                "git_email": "agent@example.test",
                "preview_origin": "http://host.docker.internal:48095",
                "ui_port": 48095,
                "host_services": [
                    {
                        "port": 8792,
                        "auth": "bearer",
                        "cwd": "d:/work/example-service",
                        "cmd": "python tools/example-supervisor/server.py",
                    }
                ],
                "runner_commands": [
                    {
                        "name": "start_infra",
                        "cmd": ["pwsh", "-NoProfile", "-File", "run.ps1", "infra"],
                        "description": "поднять локальные postgres+redis",
                    },
                    {
                        "name": "staging_logs",
                        "cmd": ["pwsh", "-NoProfile", "-File", "scripts/staging-logs.ps1"],
                        "args": {
                            "service": {"type": "str", "position": "append"},
                            "tail": {"type": "str", "position": "append"},
                        },
                        "timeout": 180,
                        "description": "логи staging-сервиса",
                    },
                ],
                "runner_port": 8796,
                "allowed_ports": [48090, 48765],
                "port_ranges": [[48080, 48100], [48760, 48770]],
                "port_deny": [48091, 48093, 48094],
                "deny_mounts": [".env.staging", "apps/backend/.env"],
            }
        ),
        encoding="utf-8",
    )

    profile = load(path)

    assert profile.project_dir == "d:/work/example-service"
    assert profile.toolchain == "node22"
    assert profile.git_name == "agent"
    assert profile.git_email == "agent@example.test"
    assert profile.preview_origin == "http://host.docker.internal:48095"
    assert profile.ui_port == 48095
    assert profile.allowed_ports == (48090, 48765)
    assert profile.port_ranges == ((48080, 48100), (48760, 48770))
    assert profile.port_deny == (48091, 48093, 48094)
    assert profile.deny_mounts == (".env.staging", "apps/backend/.env")
    assert profile.mode == "standard"
    assert profile.runner_port == 8796
    assert [c.name for c in profile.runner_commands] == ["start_infra", "staging_logs"]

    assert len(profile.host_services) == 1
    service = profile.host_services[0]
    assert service.port == 8792
    assert service.auth == "bearer"
    assert service.cwd == "d:/work/example-service"
    assert service.cmd == "python tools/example-supervisor/server.py"


def test_find_prefers_rdm_projects_dir(monkeypatch, tmp_path):
    (tmp_path / "example-service.json").write_text('{"project_dir": "x"}', encoding="utf-8")
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))

    assert profiles.find("example-service") == tmp_path / "example-service.json"


def test_find_falls_back_to_repo_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))

    assert profiles.find("_template") == PROJECTS / "_template.json"


def test_available_unions_candidate_dirs(monkeypatch, tmp_path):
    (tmp_path / "extra.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(tmp_path))

    names = profiles.available()

    assert "extra" in names
    assert "_template" not in names


def test_load_parses_port_ranges_and_deny(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps({"project_dir": "x", "port_ranges": [[47000, 47999]], "port_deny": [47500]}),
        encoding="utf-8",
    )

    profile = load(path)

    assert profile.port_ranges == ((47000, 47999),)
    assert profile.port_deny == (47500,)


def test_validate_accepts_valid_range(tmp_path):
    problems = profiles.validate(_valid(tmp_path, port_ranges=((47000, 47999),)))

    assert not [problem for problem in problems if problem.startswith(("R27", "R28", "R29", "R30", "R31", "R32"))]


def test_validate_rejects_range_out_of_bounds(tmp_path):
    problems = profiles.validate(_valid(tmp_path, port_ranges=((0, 10),)))

    assert any(problem.startswith("R27") for problem in problems)


def test_validate_rejects_reversed_range(tmp_path):
    problems = profiles.validate(_valid(tmp_path, port_ranges=((5000, 4000),)))

    assert any(problem.startswith("R28") for problem in problems)


def test_validate_rejects_too_many_ranges(tmp_path):
    ranges = tuple((1000 + index * 2, 1000 + index * 2) for index in range(33))

    problems = profiles.validate(_valid(tmp_path, port_ranges=ranges))

    assert any(problem.startswith("R29") for problem in problems)


def test_validate_rejects_overlapping_ranges(tmp_path):
    problems = profiles.validate(_valid(tmp_path, port_ranges=((40000, 41000), (40500, 42000))))

    assert any(problem.startswith("R30") for problem in problems)


def test_validate_rejects_range_containing_bridge_port(tmp_path):
    problems = profiles.validate(_valid(tmp_path, port_ranges=((8000, 9000),)))

    assert any(problem.startswith("R31") for problem in problems)


def test_validate_rejects_deny_port_out_of_bounds(tmp_path):
    problems = profiles.validate(_valid(tmp_path, port_deny=(70000,)))

    assert any(problem.startswith("R32") for problem in problems)


def test_validate_rejects_ui_port_outside_policy(tmp_path):
    problems = profiles.validate(_valid(tmp_path, ui_port=9999, allowed_ports=(8765,)))

    assert any(problem.startswith("R33") for problem in problems)


def test_validate_accepts_ui_port_in_allowed_ports(tmp_path):
    problems = profiles.validate(_valid(tmp_path, ui_port=8765, allowed_ports=(8765,)))

    assert not [problem for problem in problems if problem.startswith("R33")]


def test_validate_accepts_ui_port_inside_range(tmp_path):
    problems = profiles.validate(_valid(tmp_path, ui_port=47050, port_ranges=((47000, 47100),)))
    assert not [problem for problem in problems if problem.startswith("R33")]


def test_validate_rejects_ui_port_in_deny(tmp_path):
    problems = profiles.validate(
        _valid(tmp_path, ui_port=47050, port_ranges=((47000, 47100),), port_deny=(47050,))
    )
    assert any(problem.startswith("R33") for problem in problems)


def test_validate_rejects_range_containing_protected_port(tmp_path):
    problems = profiles.validate(_valid(tmp_path, port_ranges=((8788, 8788),)))
    assert any(problem.startswith("R34") for problem in problems)


def _exposed(**overrides):
    data = dict(name="websearch", mode="readonly", tool_allow=())
    data.update(overrides)
    return ExposedMcp(**data)


def test_load_defaults_gateway_keys(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"project_dir": "x"}), encoding="utf-8")

    profile = load(path)

    assert profile.gateway_port is None
    assert profile.exposed_mcps == ()


def test_load_parses_gateway_port_and_exposed_mcps(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "project_dir": "x",
                "GatewayPort": 8800,
                "ExposedMcps": [
                    {"Name": "websearch", "Mode": "full", "ToolAllow": ["web_search", "fetch"]},
                    {"name": "context7"},
                ],
            }
        ),
        encoding="utf-8",
    )

    profile = load(path)

    assert profile.gateway_port == 8800
    assert len(profile.exposed_mcps) == 2
    first = profile.exposed_mcps[0]
    assert first.name == "websearch"
    assert first.mode == "full"
    assert first.tool_allow == ("web_search", "fetch")
    second = profile.exposed_mcps[1]
    assert second.name == "context7"
    assert second.mode == "readonly"
    assert second.tool_allow == ()


def test_load_rejects_unknown_key_in_exposed_mcp(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps({"project_dir": "x", "exposed_mcps": [{"name": "websearch", "bogus": 1}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load(path)


def test_validate_rejects_bad_exposed_name(tmp_path):
    for name in ("Stitch-Manager", "bad.name", "bad__name", "-lead", ""):
        problems = profiles.validate(_valid(tmp_path, exposed_mcps=(_exposed(name=name),)))
        assert any(problem.startswith("R36") for problem in problems), name


def test_validate_rejects_duplicate_exposed_names(tmp_path):
    problems = profiles.validate(
        _valid(tmp_path, exposed_mcps=(_exposed(), _exposed()))
    )

    assert any(problem.startswith("R39") for problem in problems)


def test_validate_rejects_bad_exposed_mode(tmp_path):
    problems = profiles.validate(_valid(tmp_path, exposed_mcps=(_exposed(mode="standard"),)))

    assert any(problem.startswith("R37") for problem in problems)


def test_validate_rejects_empty_or_duplicate_tool_allow(tmp_path):
    problems = profiles.validate(
        _valid(tmp_path, exposed_mcps=(_exposed(tool_allow=("", "web_search", "web_search")),))
    )

    assert any(problem.startswith("R38") for problem in problems)


def test_validate_rejects_default_deny_server(tmp_path):
    for name in DEFAULT_DENY_SERVERS:
        problems = profiles.validate(_valid(tmp_path, exposed_mcps=(_exposed(name=name),)))
        assert any(problem.startswith("R40") for problem in problems), name


def test_validate_rejects_egress_and_secret_joint_exposure(tmp_path):
    problems = profiles.validate(
        _valid(
            tmp_path,
            exposed_mcps=(
                _exposed(name="websearch"),
                _exposed(name="stitch-mail"),
            ),
        )
    )

    assert any(problem.startswith("R41") for problem in problems)


def test_validate_accepts_egress_only_exposure(tmp_path):
    problems = profiles.validate(
        _valid(
            tmp_path,
            exposed_mcps=(_exposed(name="websearch"), _exposed(name="context7")),
        )
    )

    assert not [problem for problem in problems if problem.startswith("R41")]


def test_validate_accepts_secret_only_exposure(tmp_path):
    problems = profiles.validate(_valid(tmp_path, exposed_mcps=(_exposed(name="stitch-totp"),)))

    assert not [problem for problem in problems if problem.startswith(("R40", "R41"))]


def test_validate_accepts_valid_exposed_mcp(tmp_path):
    problems = profiles.validate(
        _valid(
            tmp_path,
            exposed_mcps=(_exposed(name="websearch", mode="full", tool_allow=("web_search",)),)
        )
    )

    assert not [problem for problem in problems if problem.startswith(("R36", "R37", "R38", "R39", "R40", "R41"))]


def test_validate_default_deny_not_bypassed_by_readonly_mode_or_tool_allow(tmp_path):
    problems = profiles.validate(
        _valid(
            tmp_path,
            exposed_mcps=(_exposed(name="stitch-manager", mode="readonly", tool_allow=("status",)),),
        )
    )

    assert any(problem.startswith("R40") for problem in problems)


def test_validate_default_deny_case_variant_rejected_by_name_regex(tmp_path):
    problems = profiles.validate(_valid(tmp_path, exposed_mcps=(_exposed(name="Stitch-Manager"),)))

    assert any(problem.startswith("R36") for problem in problems)
    assert not any(problem.startswith("R40") for problem in problems)


def test_gateway_data_tables_locked():
    assert DEFAULT_DENY_SERVERS >= frozenset({"stitch-manager", "lgm", "ssh", "playwright"})
    assert META_TOOL_DENY >= frozenset(
        {"execute_any", "list_mcp_commands", "import_accounts_payload", "bulk_export_accounts"}
    )
    assert "import_secrets" in META_TOOL_DENY
    assert NETWORK_EGRESS_SERVERS >= frozenset({"websearch", "context7"})
    assert SECRET_TOUCHING_SERVERS >= frozenset(
        {"stitch-manager", "lgm", "ssh", "playwright", "stitch-totp", "stitch-mail"}
    )
    assert not (NETWORK_EGRESS_SERVERS & SECRET_TOUCHING_SERVERS)
    assert DEFAULT_DENY_SERVERS <= SECRET_TOUCHING_SERVERS
    assert ARG_CONSTRAINTS["codegraph"]["projectPath"] == "project_dir"