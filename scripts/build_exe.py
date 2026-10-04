"""Сборка devbox-артефакта: exe + compose/docker/projects в dist/, zip рядом.

Артефакт повторяет layout репо (home/ с exe внутри, projects/ рядом), чтобы
compose-проект и volumes совпадали с исходниковым запуском.
Запуск: python scripts/build_exe.py
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

# CI-консоль Windows — cp1252, кириллица в print падает с UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parent.parent
HOME = REPO / "home"
DIST = REPO / "dist"

_INSTALL_MD = """\
# Установка devbox

1. Распакуй архив; папка `home/` — рабочая, `projects/` — профили проектов.
2. `home\\.env.example` скопируй в `home\\.env`, заполни токены и `PROJECT_DIR`.
3. Нужен запущенный Docker Desktop.
4. Дальше как в README репозитория, только вместо `python devbox.py` — `devbox.exe`:

```
cd home
devbox.exe use <имя профиля из ..\\projects>
devbox.exe start <имя>
devbox.exe doctor
```

Без консоли — `devbox-tray.exe` (трей-пульт: статус, старт/стоп, блок агенту).
"""


def _pyinstaller(entry: Path, name: str, *, console: bool) -> None:
    argv = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--clean", "--noconfirm",
        "--name", name,
        "--distpath", str(DIST / "bin"),
        "--workpath", str(DIST / "work"),
        "--specpath", str(DIST / "work"),
        "--paths", str(HOME),
        "--hidden-import", "rdm.proxy.__main__",
        "--hidden-import", "rdm.runner.__main__",
        # idna импортируется лениво из socket.getaddrinfo — без него LookupError в рантайме
        "--hidden-import", "encodings.idna",
        # sentry_sdk в графе транзитивный и его pyinstaller-хук падает — рантайму не нужен
        "--exclude-module", "sentry_sdk",
        str(entry),
    ]
    if not console:
        argv.append("--noconsole")
    subprocess.run(argv, check=True)


def build() -> Path:
    if DIST.exists():
        shutil.rmtree(DIST)
    system = platform.system()
    suffix = ".exe" if system == "Windows" else ""
    plat = {"Windows": "windows-x64", "Linux": "linux-x64"}.get(system, system.lower())

    _pyinstaller(REPO / "scripts" / "entry_devbox.py", "devbox", console=True)
    binaries = ["devbox"]
    if system == "Windows":
        _pyinstaller(REPO / "scripts" / "entry_tray.py", "devbox-tray", console=False)
        binaries.append("devbox-tray")

    out = DIST / f"devbox-{plat}"
    home_out = out / "home"
    home_out.mkdir(parents=True)
    for name in binaries:
        shutil.copy2(DIST / "bin" / f"{name}{suffix}", home_out)
    shutil.copy2(HOME / "docker-compose.yml", home_out)
    shutil.copy2(HOME / ".env.example", home_out)
    shutil.copytree(HOME / "docker", home_out / "docker", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "projects", out / "projects", ignore=shutil.ignore_patterns("__pycache__"))
    (out / "INSTALL.md").write_text(_INSTALL_MD, encoding="utf-8")

    shutil.make_archive(str(DIST / f"devbox-{plat}"), "zip", root_dir=DIST, base_dir=out.name)
    shutil.rmtree(DIST / "bin")
    shutil.rmtree(DIST / "work")
    return out


if __name__ == "__main__":
    built = build()
    print(f"готово: {built.parent / (built.name + '.zip')}")
