from __future__ import annotations

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=180)


def _require_image(name: str) -> None:
    probe = _docker("image", "inspect", name)
    if probe.returncode != 0:
        pytest.skip(f"{name} image not present (skip to avoid a network pull)")


def test_devnull_hides_secret_file(tmp_path):
    _require_image("alpine")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".env.staging").write_text("TOKEN=supersecret", encoding="utf-8")
    (project / "main.py").write_text("print(1)", encoding="utf-8")
    mount = str(project).replace("\\", "/")
    probe = _docker(
        "run", "--rm",
        "-v", f"{mount}:/workspace",
        "-v", "/dev/null:/workspace/.env.staging:ro",
        "alpine", "sh", "-c", "cat /workspace/.env.staging | wc -c",
    )
    if probe.returncode != 0:
        pytest.skip(f"docker run unavailable: {probe.stderr.strip()[:200]}")
    assert probe.stdout.strip() == "0"


def test_tmpfs_hides_secret_dir(tmp_path):
    _require_image("alpine")
    project = tmp_path / "proj"
    project.mkdir()
    secrets_dir = project / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "key.txt").write_text("secret", encoding="utf-8")
    mount = str(project).replace("\\", "/")
    probe = _docker(
        "run", "--rm",
        "-v", f"{mount}:/workspace",
        "--tmpfs", "/workspace/secrets",
        "alpine", "sh", "-c", "ls /workspace/secrets | wc -l",
    )
    if probe.returncode != 0:
        pytest.skip(f"docker run unavailable: {probe.stderr.strip()[:200]}")
    assert probe.stdout.strip() == "0"