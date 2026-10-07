"""Политика безопасности аргументов агента (mcp-shell-server pattern):
exec-векторы отклоняются даже внутри разрешённой команды, секреты
маскируются в аудит-логе."""

import re
from collections.abc import Iterable

from rdm.redact import (
    redact_argv as _redact_argv,  # noqa: F401 — seam: test_runner_mcp.py импортирует это имя из policy
)

DENY_ARG = [
    r"find\b.*-exec", r"\bxargs\b", r"awk\b.*system\s*\(",
    r"tar\b.*--checkpoint-action", r"\bgit\s+-c\b",
    r"Invoke-Expression", r"\biex\b", r"certutil\s+-urlcache",
    r"(curl|wget)\b.*\|\s*(sh|bash|powershell|pwsh)",
]
CMD_UNSAFE = re.compile(r'[%!"\r\n]')


def _check_args(values: Iterable[object]) -> None:
    for v in values:
        s = str(v)
        for pat in DENY_ARG:
            if re.search(pat, s, re.I):
                raise ValueError(f"аргумент отклонён политикой безопасности: {s!r}")


def _check_cmd_shim(args: Iterable[str]) -> None:
    for a in args:
        if CMD_UNSAFE.search(a):
            raise ValueError(f"аргумент небезопасен для cmd-шима: {a!r}")
