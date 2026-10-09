"""GC for /opt/tools: reclaim toolchain installs whose refcount dropped to 0.

toolchain.sh keeps registry.json ({entries: {spec: {refcount, path, ...}}})
in sync with /opt/tools/refs/*.list on every toolbox start. This module is
the consumer promised at docker/toolchain.sh:39: it deletes unreferenced
installs and drops their entries; safe to re-run.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
from collections.abc import Callable

from rdm import docker

VOLUME = "rdm-tools"
MOUNT = "/opt/tools"
_SAFE_PATH = re.compile(r"/opt/tools/[A-Za-z0-9._/-]+")


def unreferenced(registry: dict) -> list[str]:
    entries = registry.get("entries") or {}
    return sorted(spec for spec, entry in entries.items() if entry.get("refcount") == 0)


def _targets(registry: dict) -> dict[str, str]:
    entries = registry.get("entries") or {}
    targets: dict[str, str] = {}
    for spec in unreferenced(registry):
        raw = str(entries[spec].get("path") or "")
        if not _SAFE_PATH.fullmatch(raw) or ".." in pathlib.PurePosixPath(raw).parts:
            continue
        targets[spec] = raw
    return targets


def clean_volume(root: pathlib.Path) -> list[str]:
    registry_file = root / "registry.json"
    try:
        registry = json.loads(registry_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    entries = registry.get("entries") or {}
    targets = _targets(registry)
    removed: list[str] = []
    for spec, raw in targets.items():
        shutil.rmtree(root / raw[len(MOUNT) + 1:], ignore_errors=True)
        removed.append(spec)
    if removed:
        for spec in removed:
            del entries[spec]
        registry_file.write_text(json.dumps({"entries": entries}, indent=2), encoding="utf-8")
    return removed


def clean_tools(
    volume: str = VOLUME,
    runner: Callable[..., subprocess.CompletedProcess[str]] = docker.run,
) -> int:
    mount = f"{volume}:{MOUNT}"
    read = runner("run", "--rm", "-v", mount, "alpine", "cat", f"{MOUNT}/registry.json")
    if read.returncode != 0:
        print(f"{MOUNT}/registry.json не найден; чистить нечего")
        return 0
    try:
        registry = json.loads(read.stdout)
    except ValueError:
        print("registry.json повреждён; пропущено", file=sys.stderr)
        return 1
    targets = _targets(registry)
    if not targets:
        print("unreferenced тулчейнов нет")
        return 0
    rm = runner("run", "--rm", "-v", mount, "alpine", "rm", "-rf", "--", *targets.values())
    if rm.returncode != 0:
        print(f"rm не удался (rc={rm.returncode}): {rm.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    entries = registry.get("entries") or {}
    for spec in targets:
        del entries[spec]
    write = runner(
        "run", "--rm", "-i", "-v", mount, "alpine", "sh", "-c", f"cat > {MOUNT}/registry.json",
        input=json.dumps({"entries": entries}, indent=2),
    )
    if write.returncode != 0:
        print(f"registry.json не перезаписан (rc={write.returncode})", file=sys.stderr)
        return 1
    for spec in sorted(targets):
        print(f"удалён {spec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(clean_tools())
