"""Маскировка секретов в аудит-логе: единый примитив для argv и произвольного текста.

SECRETISH ловит два класса. Присваивания секретоподобных имён
(token|key|secret|password|passwd|bearer, затем = или :) — срабатывают в любом
месте текста. Голый сгенерированный hex-токен (32+ hex-символов) — только если
вся строка им является: якоря ^...$ без re.MULTILINE не маскируют hex-подстроки
внутри обычного текста, иначе маска съедала бы длинные слова-аргументы.
"""

import re

SECRETISH = re.compile(
    r"(token|key|secret|password|passwd|bearer)\s*[=:]\s*\S+|^[A-Fa-f0-9]{32,}$", re.I)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")


def redact_argv(argv: list[str]) -> list[str]:
    return ["[REDACTED]" if SECRETISH.search(a) else a for a in argv]


def redact_text(text: str) -> str:
    return _BEARER.sub("Bearer [REDACTED]", SECRETISH.sub("[REDACTED]", text))
