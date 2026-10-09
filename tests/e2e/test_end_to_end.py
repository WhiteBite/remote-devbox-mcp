from __future__ import annotations

import os
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
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_entrypoint_help():
    result = _run("--help")
    assert result.returncode == 0
    assert "issue-tokens" in result.stdout


def test_entrypoint_profile_show_example_service():
    result = _run("profile", "show", "_template")
    assert result.returncode == 0
    assert '"project_dir"' in result.stdout


def test_profile_show_survives_cp1252_console():
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    result = subprocess.run(
        [sys.executable, str(DEVOBOX), "profile", "show", "_template"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    assert result.returncode == 0
    assert '"project_dir"' in result.stdout