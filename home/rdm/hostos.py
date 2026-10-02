import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

try:
    import psutil
except ImportError:
    psutil = None

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

_LAUNCH_BASE = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP

LAUNCH_FLAGS = (
    _LAUNCH_BASE | _CREATE_BREAKAWAY_FROM_JOB
    if sys.platform == "win32"
    else 0
)

_spawned: set[int] = set()


def tempdir() -> pathlib.Path:
    return pathlib.Path(tempfile.gettempdir())


def _netstat_parse(text: str, port: int) -> int | None:
    suffix = f":{port}"
    for line in text.splitlines():
        fields = line.split()
        if (
            len(fields) < 5
            or fields[0].upper() != "TCP"
            or fields[3].upper() != "LISTENING"
        ):
            continue
        if not fields[1].endswith(suffix):
            continue
        try:
            return int(fields[4])
        except ValueError:
            return None
    return None


def find_pid_by_port(port: int) -> int | None:
    if psutil is not None:
        for conn in psutil.net_connections(kind="tcp"):
            if (
                conn.status == psutil.CONN_LISTEN
                and conn.laddr
                and conn.laddr.port == port
            ):
                return conn.pid
        return None
    result = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True)
    return _netstat_parse(result.stdout.decode("utf-8", errors="replace"), port)


def cmdline_matches(pid: int, marker: str) -> bool:
    if psutil is None or not marker:
        return False
    try:
        cmdline = psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return marker in " ".join(" ".join(cmdline).split())


def create_time(pid: int) -> float | None:
    if psutil is None:
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def owned(pid: int, recorded: float | None, marker: str) -> bool:
    if cmdline_matches(pid, marker):
        return True
    return recorded is not None and create_time(pid) == recorded


def _resolve(argv: list[str]) -> list[str]:
    exe = argv[0]
    if not os.path.isabs(exe):
        found = shutil.which(exe)
        if found:
            exe = found
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *argv[1:]]
    return [exe, *argv[1:]]


def spawn(
    argv: list[str],
    *,
    cwd: pathlib.Path | str | None = None,
    stdout_path: pathlib.Path | str | None = None,
    stderr_path: pathlib.Path | str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    stdout = open(stdout_path, "ab") if stdout_path is not None else subprocess.DEVNULL
    stderr = open(stderr_path, "ab") if stderr_path is not None else subprocess.DEVNULL
    resolved = _resolve(list(argv))
    kwargs = {
        "cwd": cwd,
        "stdout": stdout,
        "stderr": stderr,
        "env": env,
        "shell": False,
        "start_new_session": sys.platform != "win32",
    }
    try:
        try:
            proc = subprocess.Popen(resolved, creationflags=LAUNCH_FLAGS, **kwargs)
        except OSError:
            if sys.platform != "win32" or LAUNCH_FLAGS == _LAUNCH_BASE:
                raise
            # CREATE_BREAKAWAY_FROM_JOB отклоняется, когда процесс в job без breakaway
            proc = subprocess.Popen(resolved, creationflags=_LAUNCH_BASE, **kwargs)
    finally:
        if stdout_path is not None:
            stdout.close()
        if stderr_path is not None:
            stderr.close()
    _spawned.add(proc.pid)
    return proc.pid


def _reap_if_direct_child(pid: int) -> None:
    if pid not in _spawned:
        return
    _spawned.discard(pid)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            reaped, _ = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if reaped == pid:
            return
        time.sleep(0.05)


def kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _reap_if_direct_child(pid)