#!/usr/bin/env python3
"""Шим: делегирует rdm.runner (home/ — корень импорта), сам логики не содержит."""

import runpy
import sys
from pathlib import Path

_HOME = Path(__file__).resolve().parent.parent
if str(_HOME) not in sys.path:
    sys.path.insert(0, str(_HOME))

runpy.run_module("rdm.runner", run_name="__main__")
