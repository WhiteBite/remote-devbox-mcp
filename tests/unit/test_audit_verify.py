import json

from rdm import audit_verify
from rdm.runner.audit import _audit


def _chain(tmp_path, n=3):
    for i in range(n):
        _audit({"tool": f"t{i}"}, tmp_path)
    return tmp_path / "audit.log"


def _tamper(path, line_index, mutate):
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[line_index])
    mutate(entry)
    lines[line_index] = json.dumps(entry, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_valid_chain_reports_ok(tmp_path):
    report = audit_verify.verify_file(_chain(tmp_path))
    assert report.ok
    assert report.entries == 3
    assert report.broken_line is None


def test_tampered_event_detected(tmp_path):
    path = _chain(tmp_path)
    _tamper(path, 1, lambda entry: entry["event"].__setitem__("tool", "evil"))
    report = audit_verify.verify_file(path)
    assert not report.ok
    assert report.broken_line == 2
    assert "hash" in report.reason


def test_deleted_entry_breaks_chain(tmp_path):
    path = _chain(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = audit_verify.verify_file(path)
    assert not report.ok
    assert report.broken_line == 2
    assert "chain break" in report.reason


def test_forged_tail_detected(tmp_path):
    path = _chain(tmp_path)
    forged = {"ts": "2026-01-01T00:00:00", "event": {"tool": "ghost"}, "prev": "0" * 64, "hash": "a" * 64}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged) + "\n")
    report = audit_verify.verify_file(path)
    assert not report.ok
    assert report.broken_line == 4


def test_empty_log_is_ok(tmp_path):
    path = tmp_path / "audit.log"
    path.write_text("", encoding="utf-8")
    assert audit_verify.verify_file(path).ok


def test_missing_log_not_ok(tmp_path):
    report = audit_verify.verify_file(tmp_path / "absent.log")
    assert not report.ok
    assert report.broken_line is None


def test_main_exit_codes(tmp_path):
    path = _chain(tmp_path)
    assert audit_verify.main([str(path)]) == 0
    _tamper(path, 0, lambda entry: entry["event"].__setitem__("tool", "evil"))
    assert audit_verify.main([str(path)]) == 1


def test_default_audit_log_uses_temp(monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP", str(tmp_path))
    assert audit_verify.default_audit_log() == tmp_path / "rdm-runner" / "audit.log"
