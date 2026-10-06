"""Типизированные профили projects/*.json и валидация R1-R33.

Ключи JSON нормализуются регистронезависимо (наследие профилей
PowerShell ConvertTo-Json).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rdm.freeze import app_dir

BRIDGE_PORT = 8787
MAX_PORT_RANGES = 32
PROJECTS_DIR = app_dir().parent / "projects"


def runner_tool_name(name: str) -> str:
    return f"run_{name.replace(':', '_').replace('-', '_')}"


def runner_kill_tool_name(name: str) -> str:
    return f"{runner_tool_name(name)}_kill"


@dataclass(frozen=True, slots=True)
class HostService:
    port: int = 0
    auth: str = ""
    cwd: str = ""
    cmd: str = ""


@dataclass(frozen=True, slots=True)
class ArgSpec:
    type: str = "str"
    position: str = "append"


@dataclass(frozen=True, slots=True)
class RunnerCommand:
    name: str = ""
    cmd: tuple[str, ...] = ()
    description: str = ""
    args: tuple[tuple[str, ArgSpec], ...] = ()
    background: bool = False
    port: int | None = None
    timeout: int | None = None


@dataclass(frozen=True, slots=True)
class SetupCommand:
    cmd: str = ""
    marker: str = ""
    required: bool = False


@dataclass(frozen=True, slots=True)
class Script:
    name: str = ""
    cmd: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True, slots=True)
class Profile:
    project_dir: str = ""
    toolchain: str = ""
    git_name: str = ""
    git_email: str = ""
    preview_origin: str = ""
    mode: str = "standard"
    ui_port: int | None = None
    host_services: tuple[HostService, ...] = ()
    runner_commands: tuple[RunnerCommand, ...] = ()
    runner_port: int | None = None
    allowed_ports: tuple[int, ...] = ()
    port_ranges: tuple[tuple[int, int], ...] = ()
    port_deny: tuple[int, ...] = ()
    deny_mounts: tuple[str, ...] = ()
    setup_cmds: tuple[SetupCommand, ...] = ()
    scripts: tuple[Script, ...] = ()


_TOP_KEYS = {
    "projectdir": "project_dir",
    "toolchain": "toolchain",
    "gitname": "git_name",
    "gitemail": "git_email",
    "previeworigin": "preview_origin",
    "uiport": "ui_port",
    "mode": "mode",
    "hostservices": "host_services",
    "runnercommands": "runner_commands",
    "runnerport": "runner_port",
    "allowedports": "allowed_ports",
    "portranges": "port_ranges",
    "portdeny": "port_deny",
    "denymounts": "deny_mounts",
    "setupcmds": "setup_cmds",
    "scripts": "scripts",
}
_HOST_KEYS = {"port": "port", "auth": "auth", "cwd": "cwd", "cmd": "cmd"}
_RUNNER_KEYS = {
    "name": "name",
    "cmd": "cmd",
    "description": "description",
    "args": "args",
    "background": "background",
    "port": "port",
    "timeout": "timeout",
}
_SETUP_KEYS = {"cmd": "cmd", "marker": "marker", "required": "required"}
_SCRIPT_KEYS = {"name": "name", "cmd": "cmd", "description": "description"}
_ARGSPEC_KEYS = {"type": "type", "position": "position"}

_MOUNT_RE = re.compile(r"[A-Za-z0-9._\-/]+")
_TOOLCHAIN_RE = re.compile(r"[A-Za-z0-9._:\-+ ]*")


def _norm(key: str) -> str:
    """ConvertTo-Json капитализирует ключи PowerShell — сравниваем без регистра и '_'."""
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _fields(raw: object, table: Mapping[str, str], where: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: ожидался объект")
    values: dict[str, object] = {}
    unknown: list[str] = []
    for key, value in raw.items():
        name = table.get(_norm(str(key)))
        if name is None:
            unknown.append(str(key))
        else:
            values[name] = value
    if unknown:
        raise ValueError(f"{where}: неизвестные ключи: {', '.join(sorted(unknown))}")
    return values


def _str(values: Mapping[str, object], key: str, default: str = "") -> str:
    value = values.get(key, default)
    return value if isinstance(value, str) else str(value)


def _int(value: object) -> int:
    return value if isinstance(value, int) else int(str(value))


def _opt_int(values: Mapping[str, object], key: str) -> int | None:
    value = values.get(key)
    return None if value is None else _int(value)


def _seq(values: Mapping[str, object], key: str) -> list[object]:
    value = values.get(key, ())
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key}: ожидался список")
    return list(value)


def _cmd(values: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(str(item) for item in _seq(values, "cmd"))


def _port_range(raw: object) -> tuple[int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError("port_ranges: ожидался [lo, hi]")
    return (_int(raw[0]), _int(raw[1]))


def _host_service(raw: object) -> HostService:
    values = _fields(raw, _HOST_KEYS, "hostservice")
    return HostService(
        port=_int(values.get("port", 0)),
        auth=_str(values, "auth"),
        cwd=_str(values, "cwd"),
        cmd=_str(values, "cmd"),
    )


def _argspec(raw: object) -> ArgSpec:
    values = _fields(raw, _ARGSPEC_KEYS, "argspec")
    return ArgSpec(type=_str(values, "type", "str"), position=_str(values, "position", "append"))


def _args(raw: object) -> tuple[tuple[str, ArgSpec], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError("args: ожидался объект")
    return tuple((str(name), _argspec(spec)) for name, spec in raw.items())


def _runner(raw: object) -> RunnerCommand:
    values = _fields(raw, _RUNNER_KEYS, "runnercommand")
    return RunnerCommand(
        name=_str(values, "name"),
        cmd=_cmd(values),
        description=_str(values, "description"),
        args=_args(values.get("args")),
        background=bool(values.get("background", False)),
        port=_opt_int(values, "port"),
        timeout=_opt_int(values, "timeout"),
    )


def _setup(raw: object) -> SetupCommand:
    values = _fields(raw, _SETUP_KEYS, "setupcmd")
    return SetupCommand(
        cmd=_str(values, "cmd"),
        marker=_str(values, "marker"),
        required=bool(values.get("required", False)),
    )


def _script(raw: object) -> Script:
    values = _fields(raw, _SCRIPT_KEYS, "script")
    return Script(name=_str(values, "name"), cmd=_cmd(values), description=_str(values, "description"))


def _build(values: Mapping[str, object]) -> Profile:
    return Profile(
        project_dir=_str(values, "project_dir"),
        toolchain=_str(values, "toolchain"),
        git_name=_str(values, "git_name"),
        git_email=_str(values, "git_email"),
        preview_origin=_str(values, "preview_origin"),
        mode=_str(values, "mode", "standard"),
        ui_port=_opt_int(values, "ui_port"),
        host_services=tuple(_host_service(item) for item in _seq(values, "host_services")),
        runner_commands=tuple(_runner(item) for item in _seq(values, "runner_commands")),
        runner_port=_opt_int(values, "runner_port"),
        allowed_ports=tuple(_int(item) for item in _seq(values, "allowed_ports")),
        port_ranges=tuple(_port_range(item) for item in _seq(values, "port_ranges")),
        port_deny=tuple(_int(item) for item in _seq(values, "port_deny")),
        deny_mounts=tuple(str(item) for item in _seq(values, "deny_mounts")),
        setup_cmds=tuple(_setup(item) for item in _seq(values, "setup_cmds")),
        scripts=tuple(_script(item) for item in _seq(values, "scripts")),
    )


def load(path: str | os.PathLike[str]) -> Profile:
    values = _fields(json.loads(Path(path).read_text(encoding="utf-8")), _TOP_KEYS, "profile")
    return _build(values)


def validate(profile: Profile) -> list[str]:
    problems: list[str] = []
    if not profile.project_dir:
        problems.append("R1: задай $ProjectDir")
    elif not os.path.exists(profile.project_dir):
        problems.append(f"R2: папка проекта {profile.project_dir} не существует")
    if not _TOOLCHAIN_RE.fullmatch(profile.toolchain):
        problems.append("R22: Toolchain: допустимы только символы [A-Za-z0-9._:+- ]")
    if not profile.git_name:
        problems.append("R4: задай $GitName")
    if not profile.git_email or "@" not in profile.git_email:
        problems.append("R5: задай $GitEmail (с @)")

    ports: list[int] = []
    for i, service in enumerate(profile.host_services, 1):
        if not service.port or service.port < 1 or service.port > 65535:
            problems.append(f"R6: HostService[{i}]: Port 1-65535 обязателен")
        else:
            ports.append(service.port)
        if not service.cmd:
            problems.append(f"R6: HostService[{i}]: Cmd непустой")
        else:
            tokens = shlex.split(service.cmd, posix=False)
            for left, right in zip(tokens, tokens[1:], strict=False):
                joined = f"{left.strip(chr(34))} {right.strip(chr(34))}"
                if (("\\" in left or "/" in left) and ("\\" in right or "/" in right)) and os.path.exists(joined):
                    problems.append(f"WARN R26: HostService[{i}]: путь с пробелом вне кавычек: {joined}")
                    break
        if any(op in service.cmd for op in ("&&", "||", ";", "|")):
            problems.append(
                f"WARN R18: HostService[{i}]: Cmd содержит shell-оператор; host-сервисы запускаются argv-only"
            )
    if len(ports) != len(set(ports)):
        problems.append("R7: дубликат порта в HostServices")

    metachars = "|&;$()<>%!'\""
    names: list[str] = []
    for i, command in enumerate(profile.runner_commands, 1):
        if not command.name:
            problems.append(f"R8: RunnerCommand[{i}]: Name обязателен")
        else:
            names.append(command.name)
        if not command.cmd:
            problems.append(f"R8: RunnerCommand[{i}]: Cmd непустой массив")
        else:
            for element in command.cmd:
                if any(ch in metachars for ch in element):
                    problems.append(f"R9: Cmd элемент '{element}' содержит shell-символ")
        for arg_name, spec in command.args:
            if spec.type not in ("str", "path"):
                problems.append(f"R23: RunnerCommand[{i}] args.{arg_name}: type должен быть str или path")
            if spec.position not in ("append", "template"):
                problems.append(f"R23: RunnerCommand[{i}] args.{arg_name}: position должен быть append или template")
            if command.cmd and command.cmd[0] == "{" + arg_name + "}":
                problems.append(f"R23: RunnerCommand[{i}]: шаблон {{{arg_name}}} в argv[0] — аргумент станет исполняемым")
        if command.timeout is not None and not 1 <= command.timeout <= 86400:
            problems.append(f"R25: RunnerCommand[{i}]: timeout должен быть 1-86400")
    if len(names) != len(set(names)):
        problems.append("R10: дубликат имени в RunnerCommands")
    tool_owners: dict[str, str] = {}
    tool_sources = [(command.name, runner_tool_name(command.name)) for command in profile.runner_commands]
    tool_sources += [(script.name, runner_tool_name(f"script:{script.name}")) for script in profile.scripts]
    for raw_name, tool in tool_sources:
        if tool in tool_owners and tool_owners[tool] != raw_name:
            problems.append(f"R10: имена {tool_owners[tool]} и {raw_name} нормализуются в один tool {tool}")
        else:
            tool_owners[tool] = raw_name

    runner_port = profile.runner_port
    if profile.runner_commands and (not runner_port or runner_port < 1 or runner_port > 65535):
        problems.append("R11: $RunnerPort 1-65535")

    allowed: list[int] = []
    for port in profile.allowed_ports:
        if port < 1 or port > 65535:
            problems.append(f"R12: порт {port} вне 1-65535")
        else:
            allowed.append(port)
    if len(allowed) != len(set(allowed)):
        problems.append("R12: дубликат в AllowedPorts")

    for port in ports:
        if port in allowed:
            problems.append(f"R13: порт {port} и в HostServices, и в AllowedPorts")

    ranges = profile.port_ranges
    if len(ranges) > MAX_PORT_RANGES:
        problems.append(f"R29: port_ranges: не более {MAX_PORT_RANGES} диапазонов")
    protected = {BRIDGE_PORT, BRIDGE_PORT + 1}
    for service in profile.host_services:
        if service.auth == "bearer":
            protected.update((service.port, service.port + 1))
    if runner_port:
        protected.update((runner_port, runner_port + 1))
    ordered: list[tuple[int, int]] = []
    for lo, hi in ranges:
        if not 1 <= lo <= 65535 or not 1 <= hi <= 65535:
            problems.append(f"R27: диапазон {lo}-{hi} вне 1-65535")
            continue
        if lo > hi:
            problems.append(f"R28: диапазон {lo}-{hi}: lo > hi")
            continue
        if lo <= BRIDGE_PORT <= hi:
            problems.append(f"R31: диапазон {lo}-{hi} содержит порт моста {BRIDGE_PORT}")
        for guard in sorted(protected):
            if guard != BRIDGE_PORT and lo <= guard <= hi:
                problems.append(f"R34: диапазон {lo}-{hi} содержит защищённый порт {guard}")
        ordered.append((lo, hi))
    ordered.sort()
    for (lo1, hi1), (lo2, hi2) in zip(ordered, ordered[1:], strict=False):
        if lo2 <= hi1:
            problems.append(f"R30: диапазоны {lo1}-{hi1} и {lo2}-{hi2} пересекаются")
    for deny_port in profile.port_deny:
        if deny_port < 1 or deny_port > 65535:
            problems.append(f"R32: порт {deny_port} в PortDeny вне 1-65535")

    if profile.ui_port is not None:
        in_policy = profile.ui_port in profile.allowed_ports or any(
            lo <= profile.ui_port <= hi for lo, hi in profile.port_ranges
        )
        if not in_policy or profile.ui_port in profile.port_deny:
            problems.append(
                f"R33: ui_port {profile.ui_port} не входит в AllowedPorts и port_ranges или закрыт PortDeny"
            )

    if runner_port:
        if runner_port in allowed:
            problems.append(f"R14: RunnerPort {runner_port} в AllowedPorts")
        if runner_port in ports:
            problems.append(f"R15: RunnerPort {runner_port} в HostServices")
        if runner_port == BRIDGE_PORT or runner_port + 1 == BRIDGE_PORT:
            problems.append(f"R20: RunnerPort {runner_port} конфликтует с портом моста {BRIDGE_PORT}")
        naked = runner_port + 1
        if naked in allowed:
            problems.append(f"R20: порт {naked} (HTTP раннера без авторизации) в AllowedPorts")
        if naked in ports:
            problems.append(f"R20: порт {naked} (HTTP раннера без авторизации) в HostServices")
        for i, command in enumerate(profile.runner_commands, 1):
            if command.port == naked:
                problems.append(f"R20: RunnerCommand[{i}]: port {naked} — HTTP раннера без авторизации")

    for mount in profile.deny_mounts:
        if not _MOUNT_RE.fullmatch(mount):
            problems.append(f"R21: DenyMounts {mount!r}: допустимы только символы [A-Za-z0-9._-/]")
            continue
        if not os.path.exists(os.path.join(profile.project_dir, mount)):
            problems.append(
                f"WARN R16: DenyMounts {profile.project_dir}/{mount} не существует (ничего не денится)"
            )

    for i, setup in enumerate(profile.setup_cmds, 1):
        if not setup.cmd:
            problems.append(f"R17: SetupCmd[{i}]: Cmd обязателен")

    if not profile.preview_origin:
        problems.append("WARN R19: $PreviewOrigin не задан — preview-туннель не поднимется")

    return problems