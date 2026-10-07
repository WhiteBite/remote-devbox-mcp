"""Трей-пульт devbox (Windows): статус, one-click handoff, запуск/остановка.

Тонкая обёртка над `devbox.py`: трей не дублирует логику, а вызывает CLI.
Зависимости — только у трея: `pip install -r requirements-tray.txt`.
Запуск без консоли: `tray.cmd` (pythonw).
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rdm import envfile, freeze, ports, profiles

HOME = freeze.app_dir()
DEVBOX_ARGV = freeze.cli_entry()
ENV_FILE = HOME / ".env"
_TEMP = pathlib.Path(os.environ.get("TEMP", "/tmp"))
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
STATUS_INTERVAL = 30.0
COLORS = {"ok": (34, 197, 94), "bad": (239, 68, 68), "unknown": (148, 163, 184)}

_stop = threading.Event()
_watch: subprocess.Popen[str] | None = None
_status_text = "проверка…"


def _devbox(*args: str, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*DEVBOX_ARGV, *args],
        cwd=str(HOME),
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _clipboard(text: str) -> bool:
    if sys.platform != "win32":
        return False
    from ctypes import wintypes

    cf_unicodetext, gmem_moveable = 13, 0x0002
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(gmem_moveable, size)
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, buf, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(cf_unicodetext, handle):
            return False
    finally:
        user32.CloseClipboard()
    return True


def _clipboard_read() -> str:
    if sys.platform != "win32":
        return ""
    from ctypes import wintypes

    cf_unicodetext = 13
    kernel32, user32 = ctypes.windll.kernel32, ctypes.windll.user32
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(cf_unicodetext)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _active() -> str:
    return envfile.EnvFile.load(ENV_FILE).as_map().get("ACTIVE_PROFILE", "")


def _profiles() -> list[str]:
    return profiles.available()


def _ui_port() -> str:
    return envfile.EnvFile.load(ENV_FILE).as_map().get("UI_PORT", "")


def _profile_label(name: str):
    def label(icon) -> str:
        return f"{name} ✓ (активный)" if name == _active() else name

    return label


def _open_ui(icon, item) -> None:
    port = _ui_port()
    if port:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    else:
        _notify(icon, "у профиля не задан ui_port (см. projects/<имя>.json)")


def _ui_listening() -> str:
    port = _ui_port()
    if not port.isdigit():
        return "n/a"
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
            return "up"
    except OSError:
        return "down"


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
        # упавший тост не должен ронять поток трея
        pass


def _copy_block_silent(full: bool = True) -> bool:
    result = _devbox("block", *([] if full else ["--masked"]))
    if result.returncode != 0 or not result.stdout.strip():
        return False
    return _clipboard(result.stdout.strip())


def _copy_block(icon, full: bool = True) -> None:
    if _copy_block_silent(full):
        _notify(icon, "блок агенту в буфере")
    else:
        _notify(icon, "не удалось скопировать блок")


def _background(icon, work) -> None:
    def runner() -> None:
        try:
            work()
        except Exception as error:
            _notify(icon, f"ошибка: {error}")

    threading.Thread(target=runner, daemon=True).start()


def _start(icon, profile: str) -> None:
    if not profile:
        _notify(icon, "нет активного профиля — открой «Другой профиль»")
        return

    def work() -> None:
        _notify(icon, f"поднимаю стек {profile}… (может занять минуту)")
        result = _devbox("start", profile, timeout=600)
        copied = _copy_block_silent(True)
        healthy = _devbox("health", timeout=30).returncode == 0
        if result.returncode == 0 and healthy and copied:
            _notify(icon, f"готово: стек {profile} поднят, блок агенту в буфере")
        elif result.returncode != 0:
            line = (result.stderr.strip().splitlines() or ["без вывода"])[-1]
            _notify(icon, f"{profile}: не поднялся — {line[:150]}")
        else:
            _notify(icon, f"{profile}: стек не healthy — проверь логи/доктора")

    _background(icon, work)


def _stop_all(icon) -> None:
    def work() -> None:
        _devbox("down")
        _notify(icon, "остановлено")

    _background(icon, work)


def _rotate(icon) -> None:
    def work() -> None:
        result = _devbox("issue-tokens", timeout=300)
        if result.returncode != 0 or not _clipboard(result.stdout.strip()):
            line = (result.stderr.strip().splitlines() or ["без вывода"])[-1]
            _notify(icon, f"ротация не удалась — {line[:150]}")
            return
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


def _open_cockpit(icon, item) -> None:
    def work() -> None:
        result = _devbox("cockpit")
        prefix = f"http://127.0.0.1:{ports.COCKPIT_PORT}/?t="
        url = next((ln for ln in result.stdout.splitlines() if ln.startswith(prefix)), "")
        if result.returncode == 0 and url:
            webbrowser.open(url)
        else:
            line = (result.stderr.strip().splitlines() or ["без вывода"])[-1]
            _notify(icon, f"cockpit: {line[:150]}")

    _background(icon, work)


def _toggle_watch(icon, item) -> None:
    global _watch
    if _watch is not None and _watch.poll() is None:
        _watch.terminate()
        _watch = None
        _notify(icon, "авто-восстановление выключено")
        return
    _watch = subprocess.Popen(
        [*DEVBOX_ARGV, "watch"],
        cwd=str(HOME),
        creationflags=CREATE_NO_WINDOW,
    )
    _notify(icon, "авто-восстановление включено (watchdog)")


def _quit(icon, item) -> None:
    _stop.set()
    if _watch is not None and _watch.poll() is None:
        _watch.terminate()
    icon.stop()


def _status_loop(icon) -> None:
    global _status_text
    while not _stop.is_set():
        try:
            code = _devbox("health", timeout=30).returncode
        except Exception:
            code = 1
        icon.icon = _icon_rgb("ok" if code == 0 else "bad")
        _status_text = f"stack {'healthy' if code == 0 else 'problems'}, UI {_ui_listening()}"
        icon.title = f"remote-devbox — {_active() or 'нет профиля'} ({_status_text})"
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
            [pystray.MenuItem(_profile_label(p), _start_action(p)) for p in profiles]
            or [pystray.MenuItem("(нет профилей)", None, enabled=False)]
        )
    )
    return pystray.Menu(
        pystray.MenuItem(lambda i: f"remote-devbox — {_active() or 'нет профиля'} · {_status_text}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Скопировать блок агенту", lambda i, it: _copy_block(i), default=True),
        pystray.MenuItem("Поднять стек (если не поднят)", lambda i, it: _start(i, _active())),
        pystray.MenuItem("Другой профиль", start_menu),
        pystray.MenuItem("Скопировать блок ещё раз", lambda i, it: _copy_block(i)),
        pystray.MenuItem("Скопировать маскированный", lambda i, it: _copy_block(i, full=False)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Открыть UI приложения (локально)", _open_ui),
        pystray.MenuItem("Открыть cockpit", _open_cockpit),
        pystray.MenuItem("Остановить всё", lambda i, it: _stop_all(i)),
        pystray.MenuItem("Обновить токены", lambda i, it: _rotate(i)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Открыть INGRESS", _open_url(False)),
        pystray.MenuItem("Открыть Preview (UI приложения)", _open_url(True)),
        pystray.MenuItem(
            "Логи",
            pystray.Menu(
                pystray.MenuItem("host", _open_folder("rdm-host")),
                pystray.MenuItem("ingress", _open_folder("rdm-ingress")),
                pystray.MenuItem("watchdog", _open_folder("rdm-watchdog")),
            ),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Проверка (doctor)", lambda i, it: _doctor(i)),
        pystray.MenuItem(
            lambda i: "Авто-восстановление: вкл" if _watch is not None and _watch.poll() is None else "Авто-восстановление: выкл",
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