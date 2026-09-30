import subprocess
import sys
import tempfile
import time

from rdm import hostos

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_DETACHED_PROCESS = 0x00000008

TOY_SCRIPT = (
    "import socket,time;"
    "s=socket.socket();s.bind(('127.0.0.1',0));s.listen();"
    "print(s.getsockname()[1],flush=True);time.sleep(60)"
)


class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


class _FakeProcess:
    def __init__(self, cmdline):
        self._cmdline = cmdline

    def cmdline(self):
        return self._cmdline

    def create_time(self):
        return _FakePsutil.create_time_value


class _FakePsutil:
    NoSuchProcess = _FakeNoSuchProcess
    AccessDenied = _FakeAccessDenied
    create_time_value = 100.0

    @staticmethod
    def Process(pid):
        if pid < 0:
            raise _FakeNoSuchProcess(pid)
        return _FakeProcess(["python", "serve.py", "--port", "8080"])


def _wait_until(predicate, timeout=15.0, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _read_port(path, timeout=15.0):
    def read():
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return int(text.split()[0]) if text else None

    return _wait_until(read, timeout=timeout)


def test_tempdir_uses_env(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert hostos.tempdir() == tmp_path


def test_netstat_parse_port_not_substring():
    text = (
        "  TCP    0.0.0.0:8080    0.0.0.0:0    LISTENING    4321\n"
        "  TCP    0.0.0.0:80      0.0.0.0:0    LISTENING    8765\n"
    )
    assert hostos._netstat_parse(text, 80) == 8765
    assert hostos._netstat_parse(text, 8080) == 4321
    only_8080 = "  TCP    0.0.0.0:8080    0.0.0.0:0    LISTENING    4321\n"
    assert hostos._netstat_parse(only_8080, 80) is None


def test_psutil_absent_falls_back_to_netstat(monkeypatch):
    monkeypatch.setattr(hostos, "psutil", None)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=b"  TCP    127.0.0.1:54321    0.0.0.0:0    LISTENING    4242\n",
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert hostos.find_pid_by_port(54321) == 4242
    assert seen["argv"][0] == "netstat"


def test_cmdline_marker_matches(monkeypatch):
    monkeypatch.setattr(hostos, "psutil", _FakePsutil)
    assert hostos.cmdline_matches(1, "--port 8080") is True
    assert hostos.cmdline_matches(1, "missing") is False
    assert hostos.cmdline_matches(-1, "python") is False


def test_cmdline_absent_psutil_returns_false(monkeypatch):
    monkeypatch.setattr(hostos, "psutil", None)
    assert hostos.cmdline_matches(1, "python") is False


def test_create_time_guard_rejects_reused_pid(monkeypatch):
    monkeypatch.setattr(hostos, "psutil", _FakePsutil)
    recorded = hostos.create_time(42)
    assert recorded == 100.0
    monkeypatch.setattr(_FakePsutil, "create_time_value", 200.0)
    assert hostos.create_time(42) != recorded


def test_create_time_absent_psutil_returns_none(monkeypatch):
    monkeypatch.setattr(hostos, "psutil", None)
    assert hostos.create_time(1) is None


def test_spawn_flags_win32():
    assert hostos.LAUNCH_FLAGS & CREATE_DETACHED_PROCESS == 0
    if sys.platform == "win32":
        assert hostos.LAUNCH_FLAGS == (
            CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
        )
    else:
        assert hostos.LAUNCH_FLAGS == 0


def test_spawn_find_kill_roundtrip(tmp_path):
    stdout = tmp_path / "toy.out"
    stderr = tmp_path / "toy.err"
    pid = hostos.spawn(
        [sys.executable, "-c", TOY_SCRIPT],
        cwd=tmp_path,
        stdout_path=stdout,
        stderr_path=stderr,
    )
    try:
        port = _read_port(stdout)
        assert port is not None
        found = _wait_until(lambda: hostos.find_pid_by_port(port))
        assert found == pid
        hostos.kill_tree(pid)
        assert _wait_until(lambda: hostos.find_pid_by_port(port) is None)
    finally:
        hostos.kill_tree(pid)