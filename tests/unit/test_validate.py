import pathlib

from rdm.profiles import ArgSpec, HostService, Profile, RunnerCommand, Script, load, validate


def test_repo_profiles_validate_clean():
    repo = pathlib.Path(__file__).resolve().parents[2]
    for name in ("example-service", "_template"):
        profile = load(repo / "projects" / f"{name}.json")
        problems = [
            problem for problem in validate(profile)
            if not problem.startswith("WARN") and not problem.startswith("R2:")
        ]
        assert problems == [], (name, problems)


def test_validate_r10_rejects_normalized_command_collision():
    profile = Profile(
        project_dir=".",
        runner_commands=(
            RunnerCommand(name="gradle-test", cmd=("npm",)),
            RunnerCommand(name="gradle_test", cmd=("npm",)),
        ),
    )

    assert any(m.startswith("R10") and "run_gradle_test" in m for m in validate(profile))


def test_validate_r10_rejects_command_script_tool_collision():
    profile = Profile(
        project_dir=".",
        runner_commands=(RunnerCommand(name="script:shots", cmd=("npm",)),),
        scripts=(Script(name="shots", cmd=("python",)),),
    )

    assert any(m.startswith("R10") and "run_script_shots" in m for m in validate(profile))


def test_validate_r10_allows_distinct_normalized_tools():
    profile = Profile(
        project_dir=".",
        runner_commands=(RunnerCommand(name="psql:dump", cmd=("pg_dump",)),),
        scripts=(Script(name="dump", cmd=("pg_dump",)),),
    )

    assert not any(m.startswith("R10") for m in validate(profile))


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


def test_validate_r20_rejects_naked_runner_http_port():
    profile = Profile(
        project_dir=".",
        runner_commands=(RunnerCommand(name="build", cmd=("npm", "run", "build")),),
        runner_port=8796,
        allowed_ports=(8797,),
    )
    assert any(m.startswith("R20") for m in validate(profile))


def test_validate_r20_rejects_naked_runner_port_in_services():
    profile = Profile(
        project_dir=".",
        runner_commands=(RunnerCommand(name="build", cmd=("npm",)),),
        runner_port=8796,
        host_services=(HostService(port=8797, cmd="run"),),
    )
    assert any(m.startswith("R20") for m in validate(profile))


def test_validate_r21_rejects_deny_mount_injection():
    profile = Profile(project_dir=".", deny_mounts=("a\n      - C:/:/host:rw",))
    assert any(m.startswith("R21") for m in validate(profile))


def test_validate_r22_rejects_toolchain_metachars():
    profile = Profile(project_dir=".", toolchain="x' && rm -rf /opt/tools #")
    assert any(m.startswith("R22") for m in validate(profile))


def test_validate_r23_argspec_enums():
    profile = Profile(
        project_dir=".",
        runner_commands=(
            RunnerCommand(name="x", cmd=("npm",), args=(("a", ArgSpec(type="Path", position="prepend")),)),
        ),
    )
    messages = validate(profile)
    assert any(m.startswith("R23") and "type" in m for m in messages)
    assert any(m.startswith("R23") and "position" in m for m in messages)


def test_validate_r23_rejects_template_in_argv0():
    profile = Profile(
        project_dir=".",
        runner_commands=(
            RunnerCommand(name="x", cmd=("{file}",), args=(("file", ArgSpec(position="template")),)),
        ),
    )
    assert any(m.startswith("R23") and "argv[0]" in m for m in validate(profile))


def test_validate_r20_rejects_runner_port_colliding_with_bridge():
    for port in (8787, 8786):
        profile = Profile(
            project_dir=".",
            runner_commands=(RunnerCommand(name="x", cmd=("npm",)),),
            runner_port=port,
        )
        assert any(m.startswith("R20") and "моста" in m for m in validate(profile))


def test_validate_r25_rejects_out_of_range_timeout():
    profile = Profile(
        project_dir=".",
        runner_commands=(RunnerCommand(name="x", cmd=("npm",), timeout=0),),
    )
    assert any(m.startswith("R25") for m in validate(profile))


def test_validate_r26_warns_on_unquoted_path_with_space(tmp_path):
    exe = tmp_path / "my tools" / "svc.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    profile = Profile(project_dir=".", host_services=(HostService(port=80, cmd=f"{exe} --flag"),))
    assert any(m.startswith("WARN R26") for m in validate(profile))