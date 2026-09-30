"""Политика безопасности аргументов агента (mcp-shell-server pattern):
exec-векторы отклоняются даже внутри разрешённой команды, секреты
маскируются в аудит-логе."""

import re
from collections.abc import Iterable

DENY_ARG = [
    r"find\b.*-exec", r"\bxargs\b", r"awk\b.*system\s*\(",
    r"tar\b.*--checkpoint-action", r"\bgit\s+-c\b",
    r"Invoke-Expression", r"\biex\b", r"certutil\s+-urlcache",
    r"(curl|wget)\b.*\|\s*(sh|bash|powershell|pwsh)",
]
SECRETISH = re.compile(
    r"(token|key|secret|password|passwd|bearer)\s*[=:]\s*\S+|^[A-Fa-f0-9]{32,}$", re.I)


def _redact_argv(argv: Iterable[str]) -> list[str]:
    return ["[REDACTED]" if SECRETISH.search(a) else a for a in argv]


def _check_args(values: Iterable[object]) -> None:
    for v in values:
        s = str(v)
        for pat in DENY_ARG:
            if re.search(pat, s, re.I):
                raise ValueError(f"аргумент отклонён политикой безопасности: {s!r}")
