from __future__ import annotations

import subprocess
from pathlib import Path

_DEFAULT_COMPOSE_FILE = str(Path(__file__).resolve().parent.parent / "docker-compose.yml")


def compose(
    *args: str,
    compose_file: str = _DEFAULT_COMPOSE_FILE,
    cwd: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", compose_file, *args],
        cwd=cwd,
        timeout=timeout,
        capture_output=True,
        text=True,
        shell=False,
    )


def compose_ps(compose_file: str, fmt: str = "{{.Name}} {{.Status}}") -> str:
    result = compose("ps", "--format", fmt, compose_file=compose_file)
    return result.stdout


def compose_config(compose_file: str = _DEFAULT_COMPOSE_FILE) -> str:
    result = compose("config", compose_file=compose_file)
    if result.returncode != 0:
        raise RuntimeError(f"docker compose config failed: {result.stderr}")
    return result.stdout


def run(*args: str, input: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        input=input,
        timeout=timeout,
        capture_output=True,
        text=True,
        shell=False,
    )
