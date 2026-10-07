import threading
import time
from pathlib import Path

from rdm import cli
from rdm.ui import control

_RDM_SOURCE = Path(__file__).resolve().parents[2] / "home" / "rdm"


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)


def _wait_idle(queue: control.ActionQueue, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while queue.busy():
        if time.monotonic() >= deadline:
            raise AssertionError("action queue stayed busy")
        time.sleep(0.01)


def test_submit_returns_none_while_busy_then_accepts_again(monkeypatch):
    monkeypatch.setattr(control, "sink", _RecordingSink())
    queue = control.ActionQueue()
    release = threading.Event()

    def slow() -> int:
        release.wait(5)
        return 0

    assert queue.submit(slow) is not None
    assert queue.busy()
    assert queue.submit(lambda: 0) is None
    release.set()
    _wait_idle(queue)
    assert queue.submit(lambda: 0) is not None
    _wait_idle(queue)


def test_finished_event_surfaces_rc(monkeypatch):
    recorder = _RecordingSink()
    monkeypatch.setattr(control, "sink", recorder)
    queue = control.ActionQueue()
    release = threading.Event()

    def slow() -> int:
        release.wait(5)
        return 7

    action_id = queue.submit(slow)
    release.set()
    _wait_idle(queue)

    assert [event["status"] for event in recorder.events] == ["started", "finished"]
    assert all(event["kind"] == "action" for event in recorder.events)
    finished = recorder.events[-1]
    assert finished["job_id"] == action_id
    assert finished["exit"] == 7


def test_wrappers_delegate_to_cli_in_process(monkeypatch):
    seen: dict[str, tuple] = {}

    def fake(name: str, rc: int):
        def fn(*args):
            seen[name] = args
            return rc

        return fn

    for name, rc in {
        "apply_use": 3,
        "_allow": 4,
        "_issue_tokens": 5,
        "_doctor": 6,
        "_start": 7,
        "_down": 8,
        "_stop_host": 9,
        "_ingress": 10,
    }.items():
        monkeypatch.setattr(cli, name, fake(name, rc))

    assert control.apply_use("p") == 3
    assert control.allow_port(1234, True) == 4
    assert control.issue_tokens() == 5
    assert control.run_doctor() == 6
    assert control.start("p", False) == 7
    assert control.down() == 8
    assert control.stop_host() == 9
    assert control.ingress("stop") == 10

    assert seen["apply_use"] == ("p",)
    assert seen["_allow"] == (1234, True)
    assert seen["_issue_tokens"] == ()
    assert seen["_doctor"] == ()
    assert seen["_start"] == ("p", False)
    assert seen["_down"] == ()
    assert seen["_stop_host"] == ()
    assert seen["_ingress"] == ("stop",)


def test_no_redirect_stdout_in_rdm_source():
    offenders = [
        str(path.relative_to(_RDM_SOURCE))
        for path in _RDM_SOURCE.rglob("*.py")
        if "redirect_stdout" in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert offenders == []
