import json
from pathlib import Path

import pytest
from rdm import profiles
from rdm.profiles import Profile, load

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