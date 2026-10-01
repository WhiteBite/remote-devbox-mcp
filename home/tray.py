"""Трей-пульт devbox (Windows): статус, one-click handoff, запуск/остановка.

Тонкая обёртка над `devbox.py`: трей не дублирует логику, а вызывает CLI.
Зависимости — только у трея: `pip install -r requirements-tray.txt`.
Запуск без консоли: `tray.cmd` (pythonw).
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import subprocess
import sys
import threading
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rdm import envfile

HOME = pathlib.Path(__file__).resolve().parent
DEVBOX = HOME / "devbox.py"
ENV_FILE = HOME / ".env"
PROJECTS = HOME.parent / "projects"
_TEMP = pathlib.Path(os.environ.get("TEMP", "/tmp"))
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
STATUS_INTERVAL = 30.0
COLORS = {"ok": (34, 197, 94), "bad": (239, 68, 68), "unknown": (148, 163, 184)}

_stop = threading.Event()
_watch: subprocess.Popen[str] | None = None


def _devbox(*args: str, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DEVBOX), *args],
        cwd=str(HOME),
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _clipboard(text: str) -> bool:
    if sys.platform != "win32":
        return False
    cf_unicodetext, gmem_moveable = 13, 0x0002
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(gmem_moveable, size)
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, buf, size)
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(cf_unicodetext, handle)
    finally:
        user32.CloseClipboard()
    return True


def _active() -> str:
    return envfile.EnvFile.load(ENV_FILE).as_map().get("ACTIVE_PROFILE", "")


def _profiles() -> list[str]:
    try:
        return sorted(p.stem for p in PROJECTS.glob("*.json") if not p.stem.startswith("_"))
    except OSError:
        return []


def _icon_rgb(kind: str):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=COLORS.get(kind, COLORS["unknown"]))
    draw.ellipse((17, 17, 47, 47), outline=(255, 255, 255, 235), width=4)
    draw.line((25, 33, 31, 41), fill=(255, 255, 255, 255), width=5)
    draw.line((31, 41, 44, 23), fill=(255, 255, 255, 255), width=5)
    return image


def _notify(icon, message: str) -> None:
    try:
        icon.notify(message, "remote-devbox")
    except Exception:
        pass


def _copy_block(icon, full: bool = True) -> None:
    result = _devbox("block", *([] if full else ["--masked"]))
    text = result.stdout.strip()
    if _clipboard(text):
        _notify(icon, "блок агенту скопирован в буфер")
    else:
        _notify(icon, "не удалось положить в буфер")


def _background(icon, work) -> None:
    def runner() -> None:
        try:
            work()
        except Exception as error:
            _notify(icon, f"ошибка: {error}")

    threading.Thread(target=runner, daemon=True).start()


def _start(icon, profile: str) -> None:
    def work() -> None:
        result = _devbox("start", profile, "--preview", timeout=600)
        if result.returncode != 0:
            _notify(icon, f"start {profile}: ошибка (код {result.returncode})")
            return
        _clipboard(result.stdout.strip())
        _notify(icon, f"профиль {profile} поднят; блок скопирован")

    _background(icon, work)


def _stop_all(icon) -> None:
    def work() -> None:
        _devbox("down")
        _notify(icon, "остановлено")

    _background(icon, work)


def _rotate(icon) -> None:
    def work() -> None:
        result = _devbox("issue-tokens", timeout=300)
        _clipboard(result.stdout.strip())
        _notify(icon, "токены ротированы; новый блок в буфере")

    _background(icon, work)


def _doctor(icon) -> None:
    def work() -> None:
        result = _devbox("doctor", timeout=120)
        lines = [ln for ln in result.stdout.splitlines() if ln.startswith("[FAIL]")]
        if result.returncode == 0:
            _notify(icon, "doctor: all PASS")
        else:
            _notify(icon, "doctor: " + ("; ".join(lines)[:200] or "FAIL"))

    _background(icon, work)


def _open_folder(name: str):
    def action(icon, item) -> None:
        path = _TEMP / name
        path.mkdir(parents=True, exist_ok=True)
        opener = getattr(os, "startfile", None)
        if opener is not None:
            opener(path)
        else:
            webbrowser.open(path.as_uri())

    return action


def _open_url(preview: bool):
    def action(icon, item) -> None:
        result = _devbox("url", *(["--preview"] if preview else []))
        url = result.stdout.strip()
        if url:
            webbrowser.open(url)
        else:
            _notify(icon, "URL пуст (туннель не поднят?)")

    return action


def _toggle_watch(icon, item) -> None:
    global _watch
    if _watch is not None and _watch.poll() is None:
        _watch.terminate()
        _watch = None
        _notify(icon, "watchdog выключен")
        return
    _watch = subprocess.Popen(
        [sys.executable, str(DEVBOX), "watch"],
        cwd=str(HOME),
        creationflags=CREATE_NO_WINDOW,
    )
    _notify(icon, "watchdog включён")


def _quit(icon, item) -> None:
    _stop.set()
    if _watch is not None and _watch.poll() is None:
        _watch.terminate()
    icon.stop()


def _status_loop(icon) -> None:
    while not _stop.is_set():
        try:
            code = _devbox("doctor", timeout=120).returncode
        except Exception:
            code = 1
        icon.icon = _icon_rgb("ok" if code == 0 else "bad")
        label = "healthy" if code == 0 else "problems"
        icon.title = f"remote-devbox — {_active() or 'нет профиля'} ({label})"
        _stop.wait(STATUS_INTERVAL)


def _start_action(profile: str):
    def action(icon, item) -> None:
        _start(icon, profile)

    return action


def _build_menu():
    import pystray

    profiles = _profiles()
    start_menu = pystray.Menu(
        *(
            [pystray.MenuItem(p, _start_action(p)) for p in profiles]
            or [pystray.MenuItem("(нет профилей)", None, enabled=False)]
        )
    )
    return pystray.Menu(
        pystray.MenuItem(lambda i: f"remote-devbox — {_active() or 'нет профиля'}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Скопировать блок агенту", lambda i, it: _copy_block(i), default=True),
        pystray.MenuItem("Скопировать маскированный", lambda i, it: _copy_block(i, full=False)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Запустить", start_menu),
        pystray.MenuItem("Перезапустить текущий", lambda i, it: _start(i, _active())),
        pystray.MenuItem("Остановить (стек + host)", lambda i, it: _stop_all(i)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Ротация токенов + блок", lambda i, it: _rotate(i)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Открыть INGRESS", _open_url(False)),
        pystray.MenuItem("Открыть Preview", _open_url(True)),
        pystray.MenuItem(
            "Логи",
            pystray.Menu(
                pystray.MenuItem("host", _open_folder("rdm-host")),
                pystray.MenuItem("ingress", _open_folder("rdm-ingress")),
                pystray.MenuItem("watchdog", _open_folder("rdm-watchdog")),
            ),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Doctor", lambda i, it: _doctor(i)),
        pystray.MenuItem(
            lambda i: "Watch: вкл" if _watch is not None and _watch.poll() is None else "Watch: выкл",
            _toggle_watch,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", _quit),
    )


def main() -> None:
    import pystray

    icon = pystray.Icon("remote-devbox", _icon_rgb("unknown"), "remote-devbox")
    icon.menu = _build_menu()
    threading.Thread(target=_status_loop, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        crash = pathlib.Path(os.environ.get("TEMP", "/tmp")) / "rdm-tray.log"
        crash.write_text(traceback.format_exc(), encoding="utf-8")