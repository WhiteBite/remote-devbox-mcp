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


_SECRET_FLAG = re.compile(r"^--?.*(token|key|secret|password|passwd|bearer|credential|auth)", re.I)


def redact_argv(argv: list[str]) -> list[str]:
    # секрет идёт либо внутри элемента (--token=x / Bearer x / 32+hex), либо
    # значением следующего за секрет-флагом элемента (--token x)
    out: list[str] = []
    mask_next = False
    for a in argv:
        if mask_next or SECRETISH.search(a) or _BEARER.search(a):
            out.append("[REDACTED]")
        else:
            out.append(a)
        mask_next = bool(_SECRET_FLAG.match(a)) and not SECRETISH.search(a)
    return out


def redact_text(text: str) -> str:
    return _BEARER.sub("Bearer [REDACTED]", SECRETISH.sub("[REDACTED]", text))
