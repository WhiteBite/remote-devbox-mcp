"""Snapshot of the owner's opencode MCP servers backing exposed_mcps.

build() parses the opencode config layers (global ~/.config/opencode/opencode.json,
<project_dir>/opencode.json, <project_dir>/.opencode/opencode.json — later layers
win per server) at policy-save time and pins every exposed stdio server; verify()
re-parses the layers at spawn time and refuses drift; a missing snapshot with
non-empty exposed_mcps refuses gateway start (fail-closed). Remote entries and
unpinned npx/uvx packages are rejected at save. Env values are captured verbatim
and the snapshot file is written owner-only (0600 / icacls).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rdm.envfile import restrict_permissions

if TYPE_CHECKING:
    from rdm.profiles import Profile


class GatewaySnapshotError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Pin:
    specifier: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ServerSnapshot:
    command: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    pins: Pin


@dataclass(frozen=True, slots=True)
class Snapshot:
    servers: tuple[tuple[str, ServerSnapshot], ...] = ()


_PACKAGE_RUNNERS = frozenset({"npx", "uvx"})
_RUNNER_SUFFIXES = (".exe", ".cmd", ".bat")
_SERVER_FIELDS = frozenset({"command", "args", "env", "pins"})
_PIN_FIELDS = frozenset({"specifier", "sha256"})


def snapshot_path(profile_path: str | os.PathLike[str]) -> Path:
    path = Path(profile_path)
    return path.with_name(f"{path.stem}.mcpsnap.json")


def _layer_paths(profile: Profile, global_config: Path | None) -> tuple[Path, ...]:
    if global_config is None:
        config_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        global_config = config_root / "opencode" / "opencode.json"
    project = Path(profile.project_dir)
    return (global_config, project / "opencode.json", project / ".opencode" / "opencode.json")


def _merged_layers(layers: tuple[Path, ...]) -> dict[str, tuple[object, Path]]:
    merged: dict[str, tuple[object, Path]] = {}
    for layer in layers:
        if not layer.is_file():
            continue
        try:
            data = json.loads(layer.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise GatewaySnapshotError(f"opencode config layer {layer} is not valid JSON: {error}") from error
        if not isinstance(data, dict):
            raise GatewaySnapshotError(f"opencode config layer {layer}: expected a JSON object at top level")
        mcp = data.get("mcp")
        if mcp is None:
            continue
        if not isinstance(mcp, dict):
            raise GatewaySnapshotError(f"opencode config layer {layer}: 'mcp' must be an object")
        for name, entry in mcp.items():
            merged[name] = (entry, layer)
    return merged


def _runner_name(command: str) -> str:
    name = Path(command).name.lower()
    for suffix in _RUNNER_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _package_spec(args: tuple[str, ...], name: str, layer: Path) -> str:
    for arg in args:
        if not arg.startswith("-"):
            return arg
    raise GatewaySnapshotError(f"mcp server '{name}' ({layer}): no package specifier in the command")


def _digest(command: str, args: tuple[str, ...], env: tuple[tuple[str, str], ...]) -> str:
    payload = json.dumps(
        {"args": list(args), "command": command, "env": dict(env)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _server_snapshot(name: str, entry: object, layer: Path) -> ServerSnapshot:
    if not isinstance(entry, dict):
        raise GatewaySnapshotError(f"mcp server '{name}' ({layer}): entry must be an object")
    server_type = entry.get("type")
    if server_type == "remote":
        raise GatewaySnapshotError(
            f"mcp server '{name}' ({layer}) is remote (url-based); remote MCP not supported in P1"
        )
    if server_type != "local":
        raise GatewaySnapshotError(f"mcp server '{name}' ({layer}): 'type' must be \"local\"")
    command = entry.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise GatewaySnapshotError(f"mcp server '{name}' ({layer}): 'command' must be a non-empty array of strings")
    environment = entry.get("environment")
    if environment is None:
        environment = {}
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise GatewaySnapshotError(f"mcp server '{name}' ({layer}): 'environment' must map names to string values")
    command_name = command[0]
    args = tuple(command[1:])
    env = tuple(sorted(environment.items()))
    runner = _runner_name(command_name)
    if runner in _PACKAGE_RUNNERS:
        specifier = _package_spec(args, name, layer)
        body = specifier[1:] if specifier.startswith("@") else specifier
        if "@" not in body:
            raise GatewaySnapshotError(
                f"mcp server '{name}' ({layer}): {runner} package '{specifier}' is unpinned; "
                f"pin the version, e.g. '{specifier}@<version>'"
            )
    else:
        specifier = command_name
    return ServerSnapshot(
        command=command_name,
        args=args,
        env=env,
        pins=Pin(specifier=specifier, sha256=_digest(command_name, args, env)),
    )


def build(profile: Profile, global_config: Path | None = None) -> Snapshot:
    layers = _layer_paths(profile, global_config)
    merged = _merged_layers(layers)
    servers: list[tuple[str, ServerSnapshot]] = []
    for exposed in profile.exposed_mcps:
        name = exposed.name
        found = merged.get(name)
        if found is None or (isinstance(found[0], dict) and found[0].get("enabled") is False):
            raise GatewaySnapshotError(
                f"exposed mcp server '{name}' is missing or disabled in the opencode config layers: "
                + ", ".join(str(layer) for layer in layers)
            )
        entry, layer = found
        servers.append((name, _server_snapshot(name, entry, layer)))
    return Snapshot(servers=tuple(servers))


def _snapshot_json(snapshot: Snapshot) -> dict:
    return {
        "servers": {
            name: {
                "command": server.command,
                "args": list(server.args),
                "env": dict(server.env),
                "pins": {"specifier": server.pins.specifier, "sha256": server.pins.sha256},
            }
            for name, server in snapshot.servers
        }
    }


def save(snapshot: Snapshot, path: str | os.PathLike[str]) -> None:
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(_snapshot_json(snapshot), indent=2, ensure_ascii=False) + "\n")
        restrict_permissions(tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _server_from_json(name: str, entry: object, path: Path) -> ServerSnapshot:
    if not isinstance(entry, dict):
        raise GatewaySnapshotError(f"snapshot {path}: server '{name}' must be an object")
    if set(entry) != _SERVER_FIELDS:
        raise GatewaySnapshotError(
            f"snapshot {path}: server '{name}' fields must be exactly command, args, env, pins"
        )
    command = entry["command"]
    args = entry["args"]
    env = entry["env"]
    pins = entry["pins"]
    if not isinstance(command, str) or not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise GatewaySnapshotError(f"snapshot {path}: server '{name}' has malformed command/args")
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        raise GatewaySnapshotError(f"snapshot {path}: server '{name}' has malformed env")
    if (
        not isinstance(pins, dict)
        or set(pins) != _PIN_FIELDS
        or not isinstance(pins["specifier"], str)
        or not isinstance(pins["sha256"], str)
    ):
        raise GatewaySnapshotError(f"snapshot {path}: server '{name}' has malformed pins")
    env_pairs = tuple(sorted(env.items()))
    if _digest(command, tuple(args), env_pairs) != pins["sha256"]:
        raise GatewaySnapshotError(f"snapshot {path}: server '{name}' fails the integrity check (sha256 mismatch)")
    return ServerSnapshot(
        command=command,
        args=tuple(args),
        env=env_pairs,
        pins=Pin(specifier=pins["specifier"], sha256=pins["sha256"]),
    )


def load(path: str | os.PathLike[str]) -> Snapshot:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise GatewaySnapshotError(f"snapshot {target} not found") from error
    except (OSError, json.JSONDecodeError) as error:
        raise GatewaySnapshotError(f"snapshot {target} is unreadable: {error}") from error
    if not isinstance(data, dict) or set(data) != {"servers"} or not isinstance(data["servers"], dict):
        raise GatewaySnapshotError(f"snapshot {target}: expected exactly a 'servers' object")
    servers = tuple((name, _server_from_json(name, entry, target)) for name, entry in data["servers"].items())
    return Snapshot(servers=servers)


def verify(snapshot: Snapshot, profile: Profile, global_config: Path | None = None) -> None:
    current = build(profile, global_config)
    saved = dict(snapshot.servers)
    for name, server in current.servers:
        pinned = saved.get(name)
        if pinned is None:
            raise GatewaySnapshotError(
                f"mcp server '{name}' is exposed but missing from the policy snapshot; re-save the policy snapshot"
            )
        if pinned != server:
            raise GatewaySnapshotError(
                f"mcp server '{name}' drifted from the policy snapshot (config drift); re-save the policy snapshot"
            )


def save_snapshot(
    profile_path: str | os.PathLike[str], profile: Profile, global_config: Path | None = None
) -> Snapshot:
    snapshot = build(profile, global_config)
    save(snapshot, snapshot_path(profile_path))
    return snapshot


def load_verified(
    profile_path: str | os.PathLike[str], profile: Profile, global_config: Path | None = None
) -> Snapshot:
    path = snapshot_path(profile_path)
    if not path.exists():
        if profile.exposed_mcps:
            raise GatewaySnapshotError(
                f"gateway snapshot {path} is missing while exposed_mcps is non-empty; refusing to start "
                "(re-save the policy to create it)"
            )
        return Snapshot()
    snapshot = load(path)
    verify(snapshot, profile, global_config)
    return snapshot
