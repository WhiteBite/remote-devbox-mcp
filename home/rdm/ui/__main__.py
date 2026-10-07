"""Cockpit entrypoint: redacted stdio, loopback-only HTTP server."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from typing import IO

from rdm import ports, redact
from rdm.ui import server


class _RedactingStream:
    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        return self._stream.write(redact.redact_text(text))

    def writelines(self, lines: Iterable[str]) -> None:
        self._stream.writelines(redact.redact_text(line) for line in lines)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rdm.ui")
    parser.add_argument("--server", action="store_true", help="run the cockpit server (spawn marker)")
    parser.add_argument("--host", default=server.LOOPBACK_HOST)
    parser.add_argument("--port", type=int, default=ports.COCKPIT_PORT)
    args = parser.parse_args(argv)
    sys.stdout = _RedactingStream(sys.stdout)
    sys.stderr = _RedactingStream(sys.stderr)
    cockpit = server.build_server(args.host, args.port)
    cockpit.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
