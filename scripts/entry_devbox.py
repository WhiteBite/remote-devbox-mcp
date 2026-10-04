import encodings.idna  # noqa: F401 — ленивый кодек socket.getaddrinfo; PyInstaller его теряет

from rdm.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
