from rdm.ports import DEFAULT_GATEWAY_PORT, PortPolicy, compute_port_policy, is_port_allowed
from rdm.profiles import ExposedMcp, HostService, Profile, RunnerCommand, validate

COCKPIT = 8793
GATEWAY = DEFAULT_GATEWAY_PORT


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


def _gw(tmp_path, **overrides):
    data = dict(exposed_mcps=(ExposedMcp(name="websearch"),))
    data.update(overrides)
    return _valid(tmp_path, **data)


def test_validate_reports_r42_for_gateway_on_bridge_port(tmp_path):
    for port in (8787, 8786):
        problems = validate(_gw(tmp_path, gateway_port=port))
        assert any(problem.startswith("R42") for problem in problems), port


def test_validate_reports_r42_for_gateway_on_runner_port(tmp_path):
    runner = RunnerCommand(name="c", cmd=("npm",))
    for port in (8796, 8795, 8797):
        problems = validate(
            _gw(tmp_path, gateway_port=port, runner_commands=(runner,), runner_port=8796)
        )
        assert any(problem.startswith("R42") for problem in problems), port


def test_validate_reports_r42_for_gateway_on_cockpit_port(tmp_path):
    for port in (COCKPIT, COCKPIT - 1):
        problems = validate(_gw(tmp_path, gateway_port=port))
        assert any(problem.startswith("R42") for problem in problems), port


def test_validate_reports_r42_for_gateway_on_host_service_port(tmp_path):
    for port in (GATEWAY, GATEWAY + 1):
        problems = validate(
            _gw(tmp_path, host_services=(HostService(port=port, cmd="svc"),))
        )
        assert any(problem.startswith("R42") for problem in problems), port


def test_validate_reports_r42_for_gateway_in_allowed_ports(tmp_path):
    for port in (GATEWAY, GATEWAY + 1):
        problems = validate(_gw(tmp_path, allowed_ports=(port,)))
        assert any(problem.startswith("R42") for problem in problems), port


def test_validate_reports_r42_for_gateway_inside_range(tmp_path):
    problems = validate(_gw(tmp_path, port_ranges=((GATEWAY - 2, GATEWAY + 2),)))

    assert any(problem.startswith("R42") for problem in problems)


def test_validate_no_r42_without_exposed_mcps(tmp_path):
    problems = validate(_valid(tmp_path, gateway_port=8787))

    assert not [problem for problem in problems if problem.startswith("R42")]


def test_validate_accepts_default_gateway_layout(tmp_path):
    problems = validate(
        _gw(
            tmp_path,
            runner_commands=(RunnerCommand(name="c", cmd=("npm",)),),
            runner_port=8796,
        )
    )

    assert not [problem for problem in problems if problem.startswith("R42")]


def test_validate_gateway_port_out_of_bounds(tmp_path):
    problems = validate(_gw(tmp_path, gateway_port=70000))

    assert any(problem.startswith("R42") for problem in problems)
