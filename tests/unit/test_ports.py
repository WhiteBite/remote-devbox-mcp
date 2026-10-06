from rdm.ports import DEFAULT_RUNNER_PORT, compute_port_policy, is_port_allowed, with_runner_service
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


def test_policy_carries_ranges_and_deny():
    policy = compute_port_policy(_profile(port_ranges=((47000, 47999),), port_deny=(47500,)))
    assert policy.ranges == ((47000, 47999),)
    assert policy.denied == (47500,)


def test_policy_defaults_without_ranges_or_deny():
    policy = compute_port_policy(_profile(allowed_ports=(8765,)))
    assert policy.ranges == ()
    assert policy.denied == ()


def test_is_port_allowed_without_ranges_matches_allowed():
    policy = compute_port_policy(_profile(allowed_ports=(8765,)))
    assert is_port_allowed(policy, 8765)
    assert not is_port_allowed(policy, 8800)


def test_is_port_allowed_inside_and_outside_range():
    policy = compute_port_policy(_profile(allowed_ports=(), port_ranges=((47000, 47999),)))
    assert is_port_allowed(policy, 47000)
    assert is_port_allowed(policy, 47555)
    assert is_port_allowed(policy, 47999)
    assert not is_port_allowed(policy, 46999)
    assert not is_port_allowed(policy, 48000)


def test_port_deny_beats_allowed_and_range():
    policy = compute_port_policy(
        _profile(allowed_ports=(8765,), port_ranges=((47000, 47999),), port_deny=(8765, 47500))
    )
    assert not is_port_allowed(policy, 8765)
    assert not is_port_allowed(policy, 47500)
    assert is_port_allowed(policy, 47501)


def test_ranges_do_not_enter_self_authed():
    policy = compute_port_policy(_profile(port_ranges=((47000, 47999),)))
    assert all(not 47000 <= port <= 47999 for port in policy.self_authed)
