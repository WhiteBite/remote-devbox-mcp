"""Единая портовая политика ingress: self-authed, allowed и runner-порты.

Все потребители (cli use/allow, манифест, чат-блок) обязаны сходиться к
одному результату — считать эти множества где-то ещё запрещено.
"""

from __future__ import annotations

import dataclasses
import pathlib

from rdm.profiles import HostService, Profile

BRIDGE_PORT = 8787
DEFAULT_RUNNER_PORT = 8796

_HOME_DIR = pathlib.Path(__file__).resolve().parent.parent


@dataclasses.dataclass(frozen=True, slots=True)
class PortPolicy:
    self_authed: tuple[int, ...]
    allowed: tuple[int, ...]
    runner_port: int | None = None

    @property
    def runner_http_port(self) -> int | None:
        return self.runner_port + 1 if self.runner_port is not None else None


def with_runner_service(profile: Profile) -> Profile:
    if not profile.runner_commands:
        return profile
    port = profile.runner_port or DEFAULT_RUNNER_PORT
    service = HostService(port=port, auth="bearer", cwd=str(_HOME_DIR), cmd="python host\\runner-mcp.py")
    return dataclasses.replace(profile, host_services=(*profile.host_services, service))


def compute_port_policy(profile: Profile) -> PortPolicy:
    runner_port = (profile.runner_port or DEFAULT_RUNNER_PORT) if profile.runner_commands else None
    self_authed = {
        BRIDGE_PORT,
        *(service.port for service in profile.host_services if service.auth == "bearer"),
    }
    if runner_port is not None:
        self_authed.add(runner_port)
    allowed = set(profile.allowed_ports)
    allowed |= {service.port for service in profile.host_services if service.auth != "bearer"}
    allowed |= {command.port for command in profile.runner_commands if command.port}
    return PortPolicy(tuple(sorted(self_authed)), tuple(sorted(allowed)), runner_port)
