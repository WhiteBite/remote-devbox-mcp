from rdm.ports import DEFAULT_RUNNER_PORT, compute_port_policy, with_runner_service
from rdm.profiles import HostService, Profile, RunnerCommand


def _profile(**overrides) -> Profile:
    data = dict(
        host_services=(HostService(port=8792, auth="bearer", cmd="run"),),
        runner_commands=(RunnerCommand(name="build", cmd=("npm", "run", "build")),),
        runner_port=8796,
        allowed_ports=(8765,),
    )
    data.update(overrides)
    return Profile(project_dir=".", **data)


def test_policy_marks_runner_auth_proxy_self_authed():
    policy = compute_port_policy(_profile())
    assert policy.self_authed == (8787, 8792, 8796)
    assert 8797 not in policy.allowed
    assert 8797 not in policy.self_authed
    assert policy.runner_port == 8796
    assert policy.runner_http_port == 8797


def test_policy_idempotent_with_runner_service():
    profile = _profile()
    assert compute_port_policy(with_runner_service(profile)) == compute_port_policy(profile)


def test_policy_without_runner_commands():
    policy = compute_port_policy(_profile(runner_commands=(), allowed_ports=(35205,)))
    assert policy.runner_port is None
    assert policy.runner_http_port is None
    assert policy.self_authed == (8787, 8792)
    assert policy.allowed == (35205,)


def test_policy_runner_command_ports_allowed():
    policy = compute_port_policy(_profile(runner_commands=(RunnerCommand(name="dev", cmd=("npm",), port=35210),)))
    assert 35210 in policy.allowed


def test_policy_falls_back_to_default_runner_port():
    policy = compute_port_policy(_profile(runner_port=None))
    assert policy.runner_port == DEFAULT_RUNNER_PORT


def test_policy_non_bearer_services_allowed():
    policy = compute_port_policy(_profile(host_services=(HostService(port=8080, cmd="ui"),)))
    assert 8080 in policy.allowed
    assert policy.self_authed == (8787, 8796)
