from __future__ import annotations

import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
HOME = REPO / "home"
DEVOBOX = HOME / "devbox.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DEVOBOX), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_entrypoint_help():
    result = _run("--help")
    assert result.returncode == 0
    assert "issue-tokens" in result.stdout


def test_entrypoint_profile_show_muffin():
    result = _run("profile", "show", "muffin")
    assert result.returncode == 0
    assert '"project_dir"' in result.stdout