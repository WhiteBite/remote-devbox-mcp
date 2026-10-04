"""Пути и argv запуска для исходного и замороженного (PyInstaller) режимов.

Артефакт повторяет layout репо: home/ с exe внутри, projects/ рядом —
формулы путей одинаковы в обоих режимах, отличается только корень.
"""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Каталог home/: рядом с exe в заморозке, родитель rdm/ в исходниках."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def cli_entry() -> list[str]:
    """argv-префикс вызова devbox CLI: sibling-exe в заморозке, python devbox.py в исходниках."""
    if FROZEN:
        name = "devbox.exe" if sys.platform == "win32" else "devbox"
        return [str(app_dir() / name)]
    return [sys.executable, str(app_dir() / "devbox.py")]


def spawn_entry(subcommand: str, *args: str) -> list[str]:
    """argv вложенного процесса (proxy/runner): exe спавнит сам себя, исходники — devbox.py."""
    if FROZEN:
        return [sys.executable, subcommand, *args]
    return [*cli_entry(), subcommand, *args]
