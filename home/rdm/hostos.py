import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile

try:
    import psutil
except ImportError:
    psutil = None

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

LAUNCH_FLAGS = (
    _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB
    if sys.platform == "win32"
    else 0
)


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
    try:
        proc = subprocess.Popen(
            _resolve(list(argv)),
            cwd=cwd,
            stdout=stdout,
            stderr=stderr,
            env=env,
            creationflags=LAUNCH_FLAGS,
            shell=False,
            start_new_session=sys.platform != "win32",
        )
    finally:
        if stdout_path is not None:
            stdout.close()
        if stderr_path is not None:
            stderr.close()
    return proc.pid


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
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass