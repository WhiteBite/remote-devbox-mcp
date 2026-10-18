from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping

from rdm.gateway.data import ARG_CONSTRAINTS, DEFAULT_DENY_SERVERS, META_TOOL_DENY

logger = logging.getLogger(__name__)

_SERVER_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_TOOL_NAME_MAX = 64


def valid_server_name(name: str) -> bool:
    return _SERVER_NAME_RE.fullmatch(name) is not None


def exposed_tool_name(name: str, tool: str) -> str:
    return f"{name}__{tool}"


def parse_exposed_tool_name(exposed: str) -> tuple[str, str] | None:
    server, sep, tool = exposed.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


def register_tools(exposed: Iterable[str]) -> dict[str, str]:
    registry: dict[str, str] = {}
    for entry in exposed:
        parsed = parse_exposed_tool_name(entry)
        if parsed is None or not valid_server_name(parsed[0]):
            logger.warning("gateway: skip unresolvable tool name %r", entry)
            continue
        registered = entry
        if len(registered) > _TOOL_NAME_MAX or registered in registry:
            suffix = 2
            while f"{entry}_{suffix}" in registry:
                suffix += 1
            registered = f"{entry}_{suffix}"
        registry[registered] = entry
    return registry


def check_call(
    server: str,
    tool: str,
    tool_allow: Iterable[str] | None,
    args: Mapping[str, object] | None = None,
    project_dir: str | None = None,
) -> tuple[bool, str]:
    if server in DEFAULT_DENY_SERVERS:
        return False, "default-deny"
    if tool in META_TOOL_DENY:
        return False, "meta-tool-deny"
    allowed = frozenset(tool_allow or ())
    if not allowed:
        return False, "empty-allowlist"
    if tool not in allowed:
        return False, "tool-not-allowed"
    for arg, bound in ARG_CONSTRAINTS.get(server, {}).items():
        if bound != "project_dir" or args is None or arg not in args:
            continue
        value = args[arg]
        if not isinstance(value, str) or not _resolves_inside(value, project_dir):
            return False, "arg-outside-project"
    return True, "ok"


def _resolves_inside(value: str, project_dir: str | None) -> bool:
    if project_dir is None:
        return False
    if len(value) >= 2 and value[1] == ":" and value[0].isascii() and value[0].isalpha():
        if value[2:3] not in ("/", "\\"):
            return False
    base = _resolve_parts(_path_parts(project_dir)[1])
    if not base:
        return False
    absolute, parts = _path_parts(value)
    candidate = _resolve_parts(parts if absolute else base + parts)
    if candidate is None:
        return False
    return candidate[: len(base)] == base


def _path_parts(value: str) -> tuple[bool, list[str]]:
    raw = value
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isascii() and raw[0].isalpha():
        raw = raw[:2] + "/" + raw[2:]
    replaced = raw.replace("\\", "/")
    rooted = replaced.startswith("/")
    parts = [part for part in replaced.split("/") if part not in ("", ".")]
    drive = bool(parts) and len(parts[0]) == 2 and parts[0][1] == ":"
    return rooted or drive, parts


def _resolve_parts(parts: list[str]) -> list[str] | None:
    resolved: list[str] = []
    for part in parts:
        if part == "..":
            if not resolved:
                return None
            resolved.pop()
        else:
            resolved.append(part)
    return resolved
