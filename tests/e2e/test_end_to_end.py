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


def test_entrypoint_profile_import_roundtrip(tmp_path):
    source = tmp_path / "sample.ps1"
    source.write_text(
        "$ProjectDir = 'd:/tmp/proj'\n$Toolchain = 'java21'\n$GitName = 'a'\n$GitEmail = 'a@b'\n",
        encoding="utf-8",
    )
    project = tmp_path / "sample.json"
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, r'{str(HOME)}'); from rdm import cli, ps_import; "
         f"import pathlib, json; d = ps_import.parse_profile_ps1(pathlib.Path(r'{str(source)}').read_text(encoding='utf-8')); "
         f"pathlib.Path(r'{str(project)}').write_text(json.dumps(d), encoding='utf-8')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert '"project_dir"' in project.read_text(encoding="utf-8")