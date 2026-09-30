"""Path routing and manifest lookup for ingress mode."""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

ROUTE = re.compile(rb"^/p/([0-9]{1,5})(/.*)?$", re.ASCII)
MANIFEST_PATH = b"/p/9000/manifest.json"


@dataclass(frozen=True, slots=True)
class Route:
    port: int
    rest: bytes


def parse_route(target: bytes) -> Route | None:
    match = ROUTE.match(target)
    if match is None:
        return None
    port = int(match.group(1))
    if not 1 <= port <= 65535:
        return None
    return Route(port, match.group(2) or b"/")


def is_manifest(target: bytes) -> bool:
    return target == MANIFEST_PATH


def load_manifest(path: str | None) -> bytes | None:
    if not path:
        return None
    candidate = pathlib.Path(path)
    if not candidate.is_file():
        return None
    return candidate.read_bytes()