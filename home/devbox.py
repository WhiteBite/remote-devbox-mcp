#!/usr/bin/env python3
"""devbox entry point: dispatches to rdm.cli."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rdm.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))