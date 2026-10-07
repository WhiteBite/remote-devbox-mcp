from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "home"))


def _load():
    spec = importlib.util.spec_from_file_location("rdm_tray", REPO / "home" / "tray.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tray_imports_without_gui_deps():
    module = _load()
    assert isinstance(module._profiles(), list)
    assert set(module.COLORS) == {"ok", "bad", "unknown"}


def test_tray_icon_render():
    pytest.importorskip("PIL")
    module = _load()
    assert module._icon_rgb("ok").size == (64, 64)


def test_tray_builds_menu():
    pytest.importorskip("pystray")
    module = _load()
    assert len(list(module._build_menu().items)) > 0


def test_tray_start_does_not_open_preview(monkeypatch):
    module = _load()
    calls: list[tuple[str, ...]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(module, "_devbox", lambda *args, **kwargs: calls.append(args) or _Result())
    monkeypatch.setattr(module, "_background", lambda icon, work: work())
    monkeypatch.setattr(module, "_copy_block_silent", lambda full=True: True)
    module._start(None, "p1")
    start = next(c for c in calls if c[0] == "start")
    assert "--preview" not in start


def test_tray_copy_block_silent_fails_on_devbox_error(monkeypatch):
    module = _load()

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(module, "_devbox", lambda *a, **k: _Result())
    monkeypatch.setattr(module, "_clipboard", lambda text: True)
    assert module._copy_block_silent() is False


def test_tray_rotate_reports_failure(monkeypatch):
    module = _load()

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "docker down"

    notes: list[str] = []
    monkeypatch.setattr(module, "_devbox", lambda *a, **k: _Result())
    monkeypatch.setattr(module, "_background", lambda icon, work: work())
    monkeypatch.setattr(module, "_notify", lambda icon, message: notes.append(message))
    module._rotate(None)
    assert notes and "не удалась" in notes[0]


def test_tray_menu_contains_cockpit_item():
    pytest.importorskip("pystray")
    module = _load()
    labels = {item.text for item in module._build_menu().items if isinstance(item.text, str)}
    assert "Открыть cockpit" in labels


def test_tray_open_cockpit_opens_parsed_url(monkeypatch):
    module = _load()
    url = f"http://127.0.0.1:{module.ports.COCKPIT_PORT}/?t=deadbeef"
    calls: list[tuple[str, ...]] = []
    opened: list[str] = []

    class _Result:
        returncode = 0
        stdout = f"поднимаю ui…\n{url}\n"
        stderr = ""

    monkeypatch.setattr(module, "_devbox", lambda *args, **kwargs: calls.append(args) or _Result())
    monkeypatch.setattr(module, "_background", lambda icon, work: work())
    monkeypatch.setattr(module.webbrowser, "open", lambda target: opened.append(target))
    module._open_cockpit(None, None)
    assert calls[0] == ("cockpit",)
    assert opened == [url]


def test_tray_open_cockpit_failure_does_not_open(monkeypatch):
    module = _load()
    notes: list[str] = []
    opened: list[str] = []

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "cockpit не поднялся на 127.0.0.1:8793"

    monkeypatch.setattr(module, "_devbox", lambda *a, **k: _Result())
    monkeypatch.setattr(module, "_background", lambda icon, work: work())
    monkeypatch.setattr(module, "_notify", lambda icon, message: notes.append(message))
    monkeypatch.setattr(module.webbrowser, "open", lambda target: opened.append(target))
    module._open_cockpit(None, None)
    assert opened == []
    assert notes


def test_tray_clipboard_does_not_crash():
    module = _load()
    if sys.platform != "win32":
        pytest.skip("clipboard is Windows-only")
    previous = module._clipboard_read()
    try:
        assert module._clipboard("rdm clipboard regression") in (True, False)
    finally:
        module._clipboard(previous)