"""Общие хелперы спавна детей на Windows: resolve .cmd-шимов, сырая cmdline, скудное окружение."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path


def shim_cmdline(exe: str, args: list[str]) -> str:
    """Сырая командная строка для cmd-шимов: list2cmdline экранирует наши
    кавычки бэкслешами (\"), и аргумент приходит в .cmd с литеральными
    кавычками; поэтому шимовый путь собираем строкой и квотим сами. `"`/`%`/`!`
    в аргументах уже отклонены _check_cmd_shim, внутри кавычек метасимволы
    cmd литеральны. Внешняя пара кавычек обязательна: иначе cmd /c срезает
    первую и последнюю кавычки строки и ломает путь шима."""
    parts = [f'"{exe}"']
    for a in args:
        parts.append(f'"{a}"' if any(c in a for c in " \t&|<>()^") else a)
    return f'cmd /c "{" ".join(parts)}"'


def resolve(argv: list[str], cwd: Path) -> list[str] | str:
    """npm/npx на Windows = .cmd-шимы; shutil.which матчит и бесрасширенный
    sh-скрипт (WinError 193), поэтому ищем только явные расширения.
    Относительный exe Windows резолвит от cwd процесса (папка home), а не от
    cwd команды — поэтому сначала делаем его абсолютным от cwd проекта.
    argv остаётся allowlist-ным; .cmd-путь собирается в сырую строку
    (shim_cmdline), .exe идёт списком."""
    first = argv[0]
    if not os.path.isabs(first):
        candidate = cwd / first
        if candidate.exists():
            first = str(candidate)
    exe = None
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat"):
            exe = shutil.which(first + ext)
            if exe:
                break
    exe = exe or shutil.which(first) or first
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return shim_cmdline(exe, argv[1:])
    return [exe] + argv[1:]


def child_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Минимальное окружение ребёнка: системный минимум + env команды."""
    base = {}
    for k in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP",
              "USERPROFILE", "HOME", "JAVA_HOME", "FLUTTER_ROOT"):
        if os.environ.get(k):
            base[k] = os.environ[k]
    base.update(env or {})
    return base
