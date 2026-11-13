"""Registry of editable devbox environment keys.

Single source of truth for the cockpit settings panel and the plugin RPC
commands: which keys exist, which are secret, which are managed elsewhere,
and what a change requires to be restarted.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from . import tokens

_RELAY_SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_URL_RE = re.compile(r"^https?://\S+$")

_INGRESS_MODES = ("", "cloudflare", "relay")
_EGRESS_CONTAINERS = ("rdm-egress", "rdm-vpn")
_JOB_TIMEOUT_MIN = 60
_JOB_TIMEOUT_MAX = 3600


def _enum(values: tuple[str, ...]) -> Callable[[str], str | None]:
    def validate(value: str) -> str | None:
        if value in values:
            return None
        return "expected one of: " + ", ".join(repr(item) for item in values)

    return validate


def _optional_subdomain(value: str) -> str | None:
    if value == "":
        return None
    if _RELAY_SUBDOMAIN_RE.match(value):
        return None
    return "expected a lowercase dns label or an empty value"


def _optional_url(value: str) -> str | None:
    if value == "":
        return None
    if _URL_RE.match(value):
        return None
    return "expected an http(s) url or an empty value"


def _job_timeout(value: str) -> str | None:
    try:
        number = int(value)
    except ValueError:
        return "expected an integer"
    if not _JOB_TIMEOUT_MIN <= number <= _JOB_TIMEOUT_MAX:
        return f"expected an integer between {_JOB_TIMEOUT_MIN} and {_JOB_TIMEOUT_MAX}"
    return None


class EnvKey:
    def __init__(
        self,
        key: str,
        section: str,
        secret: bool = False,
        readonly: bool = False,
        restart: str = "",
        validate: Callable[[str], str | None] | None = None,
    ) -> None:
        self.key = key
        self.section = section
        self.secret = secret
        self.readonly = readonly
        self.restart = restart
        self.validate = validate


_KEYS: tuple[EnvKey, ...] = (
    EnvKey("INGRESS_MODE", "ingress", restart="ingress", validate=_enum(_INGRESS_MODES)),
    EnvKey("RELAY_SUBDOMAIN", "ingress", restart="ingress", validate=_optional_subdomain),
    EnvKey("TUNNEL_TOKEN", "ingress", secret=True, restart="ingress"),
    EnvKey("PUBLIC_URL", "ingress", restart="ingress", validate=_optional_url),
    EnvKey("EGRESS_CONTAINER", "egress", restart="ingress", validate=_enum(_EGRESS_CONTAINERS)),
    EnvKey("VLESS_SUB_URL", "egress", secret=True, restart="ingress", validate=_optional_url),
    EnvKey("JOB_TIMEOUT_SECONDS", "runtime", restart="stack", validate=_job_timeout),
    EnvKey("OPENCODE_MCP_PERMISSIONS", "managed", readonly=True),
    EnvKey("TUNNEL_TAIL", "managed", readonly=True),
    EnvKey("MCP_BEARER_TOKEN", "managed", secret=True, readonly=True),
    EnvKey("MCP_PUBLIC_TOKEN", "managed", secret=True, readonly=True),
    EnvKey("INGRESS_TOKEN", "managed", secret=True, readonly=True),
    EnvKey("GATEWAY_TOKEN", "managed", secret=True, readonly=True),
)

_BY_KEY = {item.key: item for item in _KEYS}


def is_secret(key: str) -> bool:
    item = _BY_KEY.get(key)
    return item is not None and item.secret


def entries(env_map: dict[str, str]) -> list[dict[str, Any]]:
    """Editable key list with secrets masked."""
    rows: list[dict[str, Any]] = []
    for item in _KEYS:
        raw = env_map.get(item.key, "")
        rows.append(
            {
                "key": item.key,
                "value": tokens.mask(raw) if item.secret else raw,
                "secret": item.secret,
                "readonly": item.readonly,
                "section": item.section,
            }
        )
    return rows


def apply_patch(
    env_map: dict[str, str], patch: dict[str, Any]
) -> tuple[dict[str, str], list[str], list[str]]:
    """Validate a patch against the registry.

    Returns the new env map, the validation errors and the restart targets
    required by the accepted changes. The map is left untouched when any
    value is rejected.
    """
    errors: list[str] = []
    accepted: dict[str, str] = {}
    for key, value in patch.items():
        item = _BY_KEY.get(key)
        if item is None:
            errors.append(f"{key}: unknown key")
            continue
        if item.readonly:
            errors.append(f"{key}: managed key, rotate it with issue_tokens")
            continue
        text = value if isinstance(value, str) else str(value)
        problem = item.validate(text) if item.validate is not None else None
        if problem is not None:
            errors.append(f"{key}: {problem}")
            continue
        accepted[key] = text
    if errors:
        return dict(env_map), errors, []
    new_map = dict(env_map)
    restart: list[str] = []
    for key, text in accepted.items():
        if new_map.get(key, "") == text:
            continue
        new_map[key] = text
        target = _BY_KEY[key].restart
        if target and target not in restart:
            restart.append(target)
    return new_map, [], restart
