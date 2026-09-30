from rdm.profiles import HostService, Profile, RunnerCommand, validate


def test_validate_r14_reports_runner_port():
    profile = Profile(
        project_dir=".",
        host_services=(HostService(port=8792, cmd="run"),),
        runner_port=8796,
        allowed_ports=(8796,),
    )

    r14 = [m for m in validate(profile) if m.startswith("R14")]

    assert r14
    assert "8796" in r14[0]
    assert "8792" not in r14[0]


def test_validate_r15_reports_runner_port():
    profile = Profile(
        project_dir=".",
        host_services=(HostService(port=8796, cmd="run"), HostService(port=8792, cmd="other")),
        runner_port=8796,
    )

    r15 = [m for m in validate(profile) if m.startswith("R15")]

    assert r15
    assert "8796" in r15[0]
    assert "8792" not in r15[0]


def test_validate_duplicate_ports():
    profile = Profile(
        project_dir=".",
        host_services=(HostService(port=8792, cmd="a"), HostService(port=8792, cmd="b")),
    )

    assert "R7: дубликат порта в HostServices" in validate(profile)


def test_validate_shell_metachar():
    profile = Profile(
        project_dir=".",
        runner_commands=(RunnerCommand(name="build", cmd=("npm", "run", "a;b")),),
    )

    assert any(m.startswith("R9") for m in validate(profile))


def test_validate_warn_r16_r19(tmp_path):
    profile = Profile(project_dir=str(tmp_path), deny_mounts=("missing.env",), preview_origin="")

    messages = validate(profile)

    assert any(m.startswith("WARN R16") for m in messages)
    assert any(m.startswith("WARN R19") for m in messages)