from rdm.ports import PortPolicy, compute_port_policy, is_port_allowed
from rdm.profiles import HostService, Profile, RunnerCommand, validate

COCKPIT = 8793


def _valid(tmp_path, **overrides):
    data = dict(
        project_dir=str(tmp_path),
        git_name="agent",
        git_email="agent@example.test",
        preview_origin="http://host.docker.internal:8080",
    )
    data.update(overrides)
    return Profile(**data)


def test_cockpit_denied_even_when_explicitly_allowed():
    policy = PortPolicy(self_authed=(), allowed=(COCKPIT,))
    assert not is_port_allowed(policy, COCKPIT)


def test_cockpit_denied_even_when_computed_from_profile():
    policy = compute_port_policy(Profile(project_dir=".", allowed_ports=(COCKPIT,)))
    assert not is_port_allowed(policy, COCKPIT)


def test_cockpit_denied_even_inside_range():
    policy = PortPolicy(self_authed=(), allowed=(), ranges=((COCKPIT - 3, COCKPIT + 7),))
    assert not is_port_allowed(policy, COCKPIT)
    assert is_port_allowed(policy, COCKPIT + 1)


def test_cockpit_denied_even_via_host_service():
    policy = compute_port_policy(
        Profile(project_dir=".", host_services=(HostService(port=COCKPIT, cmd="cockpit"),))
    )
    assert not is_port_allowed(policy, COCKPIT)


def test_validate_reports_r35_for_cockpit_in_allowed_ports(tmp_path):
    problems = validate(_valid(tmp_path, allowed_ports=(COCKPIT,)))
    assert any(problem.startswith("R35") for problem in problems)


def test_validate_reports_r35_for_range_covering_cockpit(tmp_path):
    problems = validate(_valid(tmp_path, port_ranges=((COCKPIT - 3, COCKPIT + 7),)))
    assert any(problem.startswith("R35") for problem in problems)


def test_validate_reports_r35_for_cockpit_in_host_services(tmp_path):
    problems = validate(_valid(tmp_path, host_services=(HostService(port=COCKPIT, cmd="cockpit"),)))
    assert any(problem.startswith("R35") for problem in problems)


def test_validate_reports_r35_for_cockpit_runner_ports(tmp_path):
    problems = validate(
        _valid(
            tmp_path,
            runner_port=COCKPIT,
            runner_commands=(RunnerCommand(name="c", cmd=("npm",), port=COCKPIT),),
        )
    )
    assert any(problem.startswith("R35") for problem in problems)
