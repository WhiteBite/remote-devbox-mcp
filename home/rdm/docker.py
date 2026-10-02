from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_DEFAULT_COMPOSE_FILE = str(Path(__file__).resolve().parent.parent / "docker-compose.yml")
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def compose(
    *args: str,
    compose_file: str = _DEFAULT_COMPOSE_FILE,
    cwd: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "compose", "-f", compose_file, *args]
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
            shell=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(["docker"], 127, "", "docker not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "docker timeout")


def compose_ps(compose_file: str, fmt: str = "{{.Name}} {{.Status}}") -> str:
    result = compose("ps", "--format", fmt, compose_file=compose_file)
    return result.stdout


def run(*args: str, input: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    argv = ["docker", *args]
    try:
        return subprocess.run(
            argv,
            input=input,
            timeout=timeout,
            capture_output=True,
            text=True,
            shell=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(["docker"], 127, "", "docker not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", "docker timeout")
