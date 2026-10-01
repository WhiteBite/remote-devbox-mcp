import pathlib
import socket
import sys
import tempfile
import time

from rdm import hostos, procman
from rdm.profiles import HostService, Profile

HOME_DIR = pathlib.Path(__file__).resolve().parents[2] / "home"
SLEEP_60 = "import time; time.sleep(60)"
FAKE_PROXY_ARGV = [sys.executable, "-c", SLEEP_60]


def _isolate_tempdir(monkeypatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _ephemeral_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pids_file(profile_name: str) -> pathlib.Path:
    return hostos.tempdir() / "rdm-host" / f"{profile_name}-pids.txt"


def _entries(path: pathlib.Path) -> list[tuple[int, str, str]]:
    fields = [line.split("|", 2) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [(int(f[0]), f[1], f[2]) for f in fields]


def _wait_until(predicate, timeout: float = 15.0, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _alive(pid: int) -> bool:
    return hostos.create_time(pid) is not None


def _sleep_profile(tmp_path: pathlib.Path, port: int, auth: str) -> Profile:
    cmd = f'"{sys.executable}" -c "{SLEEP_60}"'
    return Profile(host_services=(HostService(port=port, auth=auth, cwd=str(tmp_path), cmd=cmd),))


def _can_connect(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def test_start_stop_service_roundtrip(monkeypatch, tmp_path):
    _isolate_tempdir(monkeypatch, tmp_path)
    profile = _sleep_profile(tmp_path, _ephemeral_port(), "bearer")
    report = procman.restart_host_services(profile, "rt", HOME_DIR, "token", FAKE_PROXY_ARGV)
    path = _pids_file("rt")
    recorded = _entries(path)
    assert len(recorded) == 2
    assert report and all(isinstance(line, str) for line in report)
    assert all(_alive(pid) for pid, _, _ in recorded)

    procman.stop_host_services("rt")

    assert not path.exists()
    assert all(_wait_until(lambda p=pid: not _alive(p)) for pid, _, _ in recorded)


def test_pids_file_schema_includes_create_time(monkeypatch, tmp_path):
    _isolate_tempdir(monkeypatch, tmp_path)
    profile = _sleep_profile(tmp_path, _ephemeral_port(), "")
    procman.restart_host_services(profile, "schema", HOME_DIR, "token")
    ((pid, create_time, marker),) = _entries(_pids_file("schema"))
    assert float(create_time) > 0
    assert hostos.create_time(pid) == float(create_time)
    assert marker
    procman.stop_host_services("schema")


def test_stop_refuses_pid_with_changed_cmdline_and_create_time(monkeypatch, tmp_path):
    _isolate_tempdir(monkeypatch, tmp_path)
    pid = hostos.spawn([sys.executable, "-c", SLEEP_60])
    try:
        path = _pids_file("guard")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{pid}|{hostos.create_time(pid) + 99999.0}|not-our-marker\n", encoding="utf-8"
        )
        procman.stop_host_services("guard")
        assert _alive(pid)
        assert not path.exists()
    finally:
        hostos.kill_tree(pid)


def test_restart_uses_single_path(monkeypatch, tmp_path):
    _isolate_tempdir(monkeypatch, tmp_path)
    profile = _sleep_profile(tmp_path, _ephemeral_port(), "bearer")
    procman.restart_host_services(profile, "once", HOME_DIR, "token", FAKE_PROXY_ARGV)
    first = _entries(_pids_file("once"))
    assert len(first) == 2

    procman.restart_host_services(profile, "once", HOME_DIR, "token", FAKE_PROXY_ARGV)
    second = _entries(_pids_file("once"))

    assert len(second) == 2
    assert all(
        _wait_until(lambda p=pid, c=float(ct): hostos.create_time(p) != c)
        for pid, ct, _ in first
    )
    assert all(_alive(pid) for pid, _, _ in second)
    procman.stop_host_services("once")


def test_restart_frees_port_held_by_orphan_runner(monkeypatch, tmp_path):
    _isolate_tempdir(monkeypatch, tmp_path)
    port = _ephemeral_port()
    orphan_code = (
        "import socket,time;s=socket.socket();"
        "s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('127.0.0.1',{port}));s.listen();time.sleep(60)"
    )
    orphan = hostos.spawn([sys.executable, "-c", orphan_code, "runner-mcp.py"])
    try:
        assert _wait_until(lambda: _can_connect(port))
        profile = _sleep_profile(tmp_path, port, "")
        procman.restart_host_services(profile, "orphan", HOME_DIR, "token")
        assert _wait_until(lambda: not _alive(orphan))
        procman.stop_host_services("orphan")
    finally:
        hostos.kill_tree(orphan)


def test_host_services_dead_uses_create_time_fallback(monkeypatch, tmp_path):
    _isolate_tempdir(monkeypatch, tmp_path)
    pid = hostos.spawn([sys.executable, "-c", SLEEP_60])
    path = _pids_file("ct")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}|{hostos.create_time(pid)}|marker-that-never-matches\n", encoding="utf-8")
    try:
        assert not procman.host_services_dead("ct")
    finally:
        hostos.kill_tree(pid)
    assert _wait_until(lambda: procman.host_services_dead("ct"))


def test_missing_pidfile_is_noop(monkeypatch, tmp_path):
    _isolate_tempdir(monkeypatch, tmp_path)
    assert procman.stop_host_services("never-started") is None
    assert procman.stop_ingress() is None


def test_start_ingress_listens(monkeypatch, tmp_path):
    _isolate_tempdir(monkeypatch, tmp_path)
    port = _ephemeral_port()
    env_map = {
        "INGRESS_TOKEN": "integration-test-token",
        "PROXY_PORT": str(port),
        "SELF_AUTHED_PORTS": "",
        "ALLOWED_PORTS": "",
        "RDM_MANIFEST_PATH": str(tmp_path / "manifest.json"),
    }
    state_dir = hostos.tempdir() / "rdm-ingress"
    pid = procman.start_ingress(env_map, HOME_DIR, state_dir)
    try:
        assert _wait_until(lambda: _can_connect(port))
        procman.stop_ingress()
        assert _wait_until(lambda: not _can_connect(port))
        assert not (state_dir / "pids.txt").exists()
    finally:
        hostos.kill_tree(pid)
