from pathlib import Path

_HOME = Path(__file__).resolve().parents[2] / "home"


def _py_sources():
    for path in sorted((_HOME / "rdm").rglob("*.py")):
        yield path
    for path in sorted((_HOME / "host").rglob("*.py")):
        yield path
    yield _HOME / "devbox.py"
    yield _HOME / "tray.py"


def test_projects_dir_literal_only_in_profiles():
    offenders = [
        path for path in _py_sources()
        if '"projects"' in path.read_text(encoding="utf-8") and path.name != "profiles.py"
    ]
    assert offenders == []


def test_taskkill_appears_only_in_hostos():
    offenders = [
        path for path in _py_sources()
        if "taskkill" in path.read_text(encoding="utf-8") and path.name != "hostos.py"
    ]
    assert offenders == []


def test_cmdline_matches_call_sites_only_in_hostos_and_procman():
    allowed = {"hostos.py", "procman.py"}
    offenders = [
        path for path in _py_sources()
        if "cmdline_matches" in path.read_text(encoding="utf-8") and path.name not in allowed
    ]
    assert offenders == []


def test_kill_tree_call_sites_only_in_lifecycle_owners():
    allowed = {"hostos.py", "procman.py", "__main__.py"}
    offenders = [
        path for path in _py_sources()
        if "kill_tree" in path.read_text(encoding="utf-8") and path.name not in allowed
    ]
    assert offenders == []


def test_port_policy_computed_only_in_ports_module():
    allowed = {"ports.py", "cli.py", "render.py", "server.py", "__main__.py", "envfile.py"}
    offenders = [
        path for path in _py_sources()
        if "SELF_AUTHED" in path.read_text(encoding="utf-8") and path.name not in allowed
    ]
    assert offenders == []
    policy_callers = [
        path for path in _py_sources()
        if "compute_port_policy" in path.read_text(encoding="utf-8")
        and path.name not in {"ports.py", "cli.py", "render.py"}
    ]
    assert policy_callers == []
