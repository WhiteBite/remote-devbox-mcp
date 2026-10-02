from __future__ import annotations

import builtins

from rdm.proxy import access_log


def test_log_appends_lines_and_creates_parent(tmp_path):
    path = tmp_path / "logs" / "access.log"
    access_log.log(path, 8787, "GET", "/p/8787/mcp", "forward")
    access_log.log(path, 8792, "POST", "/p/8792/mcp", "ingress")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" 8787 GET /p/8787/mcp forward")
    assert lines[1].endswith(" 8792 POST /p/8792/mcp ingress")


def test_log_opens_each_path_once(tmp_path, monkeypatch):
    opened: list[str] = []
    real_open = builtins.open

    def counting_open(file, mode="r", *args, **kwargs):
        if "a" in mode:
            opened.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(access_log, "open", counting_open, raising=False)
    path = tmp_path / "access.log"
    access_log.log(path, 8787, "GET", "/a", "forward")
    access_log.log(path, 8787, "GET", "/b", "forward")
    assert opened == [str(path)]


def test_log_rotates_on_size(tmp_path, monkeypatch):
    monkeypatch.setattr(access_log, "_CHECK_EVERY", 1)
    monkeypatch.setattr(access_log, "_MAX_BYTES", 10)
    path = tmp_path / "access.log"
    access_log.log(path, 8787, "GET", "/a", "forward")
    rotated = tmp_path / "access.log.1"
    assert rotated.exists()
    assert len(rotated.read_text(encoding="utf-8").splitlines()) == 1
    monkeypatch.setattr(access_log, "_MAX_BYTES", 10 * 1024 * 1024)
    access_log.log(path, 8787, "GET", "/b", "forward")
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
