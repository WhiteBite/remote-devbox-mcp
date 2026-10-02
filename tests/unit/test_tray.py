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


def test_tray_clipboard_does_not_crash():
    module = _load()
    if sys.platform != "win32":
        pytest.skip("clipboard is Windows-only")
    previous = module._clipboard_read()
    try:
        assert module._clipboard("rdm clipboard regression") in (True, False)
    finally:
        module._clipboard(previous)