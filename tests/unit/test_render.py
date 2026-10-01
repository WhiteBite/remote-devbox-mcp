import base64
import hashlib
from dataclasses import replace

from rdm.profiles import HostService, Profile, RunnerCommand, Script, SetupCommand
from rdm.render import (
    build_manifest,
    build_setup_script,
    render_agents_md,
    render_override,
    setup_script_b64,
    toolchain_ref,
    tunnel_tail,
)

PROFILE = Profile(
    project_dir="d:/Sources/StartUp/Muffin",
    toolchain="java21 flutter:3.44.9",
    git_name="WhiteBite",
    git_email="ad.lord9000@yandex.ru",
    preview_origin="http://host.docker.internal:8080",
    host_services=(
        HostService(
            port=8792,
            auth="bearer",
            cwd="D:\\Sources\\StartUp\\Muffin",
            cmd="python tools/muffin-supervisor/server.py",
        ),
    ),
    runner_commands=(
        RunnerCommand(name="gradle-test", cmd=("./gradlew", "test"), description="run tests"),
        RunnerCommand(name="psql:dump", cmd=("pg_dump",), description="dump db"),
    ),
    allowed_ports=(8765, 8080),
    deny_mounts=(".env.staging", "apps/backend/.env"),
    setup_cmds=(
        SetupCommand(cmd="echo one", marker="", required=False),
        SetupCommand(cmd="echo two", marker="m2", required=True),
    ),
    scripts=(Script(name="shots", cmd=("python", "shots.py"), description="take screenshots"),),
)


def test_override_includes_shadow_and_deny_mounts():
    override = render_override(PROFILE)

    assert override == (
        "services:\n"
        "  toolbox:\n"
        "    volumes:\n"
        "      - ./docker/workspace-empty:/workspace/.opencode:ro\n"
        "      - /dev/null:/workspace/.env.staging:ro\n"
        "      - /dev/null:/workspace/apps/backend/.env:ro\n"
    )


def test_override_without_deny_mounts():
    override = render_override(replace(PROFILE, deny_mounts=()))

    assert override == (
        "services:\n"
        "  toolbox:\n"
        "    volumes:\n"
        "      - ./docker/workspace-empty:/workspace/.opencode:ro\n"
    )


def test_setup_script_is_lf_and_marked():
    script = build_setup_script(PROFILE, "muffin")

    assert "\r" not in script
    first = hashlib.md5(b"echo onemuffin").hexdigest()
    second = hashlib.md5(b"echo twom2muffin").hexdigest()
    assert script == (
        "# generated: devbox.py use muffin\n"
        f"if [ ! -f /opt/tools/.setup-1-{first} ]; then\n"
        "  echo one || echo '[setup] WARN: cmd 1 failed, continue'\n"
        f"  touch /opt/tools/.setup-1-{first}\n"
        "fi\n"
        f"if [ ! -f /opt/tools/.setup-2-{second} ]; then\n"
        "  echo two || exit 1\n"
        f"  touch /opt/tools/.setup-2-{second}\n"
        "fi\n"
    )


def test_setup_required_vs_optional():
    required = build_setup_script(
        replace(PROFILE, setup_cmds=(SetupCommand(cmd="install x", marker="v1", required=True),)),
        "muffin",
    )
    optional = build_setup_script(
        replace(PROFILE, setup_cmds=(SetupCommand(cmd="install x", marker="v1", required=False),)),
        "muffin",
    )

    assert "install x || exit 1\n" in required
    assert "install x || echo '[setup] WARN: cmd 1 failed, continue'\n" in optional
    assert "exit 1" not in optional


def test_setup_script_b64_roundtrip():
    encoded = setup_script_b64(PROFILE, "muffin")

    assert base64.b64decode(encoded).decode("utf-8") == build_setup_script(PROFILE, "muffin")


def test_manifest_endpoints():
    manifest = build_manifest(PROFILE, "muffin", "https://x.trycloudflare.com", "standard")

    assert manifest == {
        "profile": "muffin",
        "project": "d:/Sources/StartUp/Muffin",
        "ingress_url": "https://x.trycloudflare.com",
        "endpoints": [
            {"name": "bridge", "port": 8787, "auth": "bearer", "path": "/p/8787/mcp"},
            {"name": "host-8792", "port": 8792, "auth": "bearer", "path": "/p/8792/mcp"},
            {"name": "allowed-8765", "port": 8765, "auth": "ingress", "path": "/p/8765"},
            {"name": "allowed-8080", "port": 8080, "auth": "ingress", "path": "/p/8080"},
        ],
        "allowed_ports": [8765, 8080],
        "runner_commands": ["gradle-test", "psql:dump"],
        "scripts": ["shots"],
        "preview_origin": "http://host.docker.internal:8080",
        "mode": "standard",
        "host_requirements": {
            "memory_mb": 4096,
            "storage_mb": 20480,
            "note": "toolchains in /opt/tools volume",
        },
    }


def test_tunnel_tail_named_vs_quick():
    assert tunnel_tail("eyJtoken") == "run --token eyJtoken"
    assert tunnel_tail("") == "--protocol http2 --url http://host.docker.internal:8799"


def test_agents_md_sections():
    text = render_agents_md(PROFILE, "muffin", "java21 flutter:3.44.9", "standard", "8765,8080")

    assert text == (
        "<!-- auto-generated: devbox.py use muffin -->\n"
        "## Environment\n"
        "- profile: muffin; project: d:/Sources/StartUp/Muffin\n"
        "- toolchain: java21 flutter:3.44.9\n"
        "- mode: standard; allowed ports: 8765,8080\n"
        "## Runner commands\n"
        "- run_gradle_test: run tests\n"
        "- run_psql_dump: dump db\n"
        "## Scripts\n"
        "- run_script_shots: take screenshots\n"
    )


def test_agents_md_empty_sections():
    empty = replace(PROFILE, runner_commands=(), scripts=())

    text = render_agents_md(empty, "muffin", "java21", "standard", "")

    assert "## Runner commands\n- (нет)\n" in text
    assert "## Scripts\n- (нет)\n" in text


def test_toolchain_ref():
    assert toolchain_ref(PROFILE) == "java21 flutter:3.44.9"
