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


def test_load_muffin_json():
    profile = load(PROJECTS / "muffin.json")

    assert profile.project_dir == "d:/Sources/StartUp/Muffin"
    assert profile.toolchain == "java21 flutter:3.44.9"
    assert profile.git_name == "WhiteBite"
    assert profile.git_email == "ad.lord9000@yandex.ru"
    assert profile.preview_origin == "http://host.docker.internal:47095"
    assert profile.allowed_ports == (47090, 47765)
    assert profile.port_ranges == ((47080, 47100), (47760, 47770))
    assert profile.port_deny == (47091, 47093, 47094)
    assert profile.deny_mounts == (".env.staging", "apps/backend/.env")
    assert profile.mode == "standard"
    assert profile.runner_port == 8796
    assert [c.name for c in profile.runner_commands] == ["start_infra", "staging_status", "staging_logs"]

    assert len(profile.host_services) == 1
    service = profile.host_services[0]
    assert service.port == 8792
    assert service.auth == "bearer"
    assert service.cwd == "D:\\Sources\\StartUp\\Muffin"
    assert service.cmd == "python tools/muffin-supervisor/server.py"


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