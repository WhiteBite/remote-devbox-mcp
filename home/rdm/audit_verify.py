"""Verify the runner audit.log SHA-256 chain (rdm.runner.audit) and report the first break.

Each entry hashes {ts, event, prev} and carries prev = hash of the previous
entry, genesis being 64 zeros. A line whose stored hash does not match its
payload, or whose prev does not match the preceding hash, is the first break.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import tempfile
from dataclasses import dataclass

GENESIS = "0" * 64


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    entries: int
    broken_line: int | None
    reason: str


def default_audit_log() -> pathlib.Path:
    return pathlib.Path(os.environ.get("TEMP") or tempfile.gettempdir()) / "rdm-runner" / "audit.log"


def _rehash(entry: dict) -> str:
    payload = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def verify_file(path: pathlib.Path) -> ChainReport:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return ChainReport(False, 0, None, f"cannot read {path}: {error}")
    expected = GENESIS
    for number, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except ValueError:
            return ChainReport(False, number - 1, number, "entry is not valid JSON")
        if not isinstance(entry, dict) or not isinstance(entry.get("hash"), str):
            return ChainReport(False, number - 1, number, "entry has no hash field")
        if _rehash(entry) != entry["hash"]:
            return ChainReport(False, number - 1, number, f"line {number}: entry tampered (hash mismatch)")
        if entry.get("prev") != expected:
            return ChainReport(False, number - 1, number, f"line {number}: chain break (prev != previous hash)")
        expected = entry["hash"]
    return ChainReport(True, len(lines), None, "")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    path = pathlib.Path(argv[0]) if argv else default_audit_log()
    report = verify_file(path)
    if report.ok:
        print(f"audit chain OK: {report.entries} entries in {path}")
        return 0
    print(f"audit chain BROKEN: {report.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
