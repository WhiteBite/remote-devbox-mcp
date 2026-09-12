#!/usr/bin/env python3
"""runner-mcp — host-MCP для команд проекта, объявленных в профиле devbox.

Конфиг: JSON-путь в env RUNNER_CONFIG (рендерит home/devbox.ps1 use <имя>
из поля $RunnerCommands профиля). Каждая команда = argv-массив; аргументы
агента подставляются ТОЛЬКО как элементы argv (никакого shell=True),
type=path валидируется на выход за cwd. Background-команды детачатся,
pid пишется в %TEMP%\\rdm-runner\\<profile>-<name>.pid. Каждый вызов
логируется в %TEMP%\\rdm-runner\\audit.log.

Наружу отдаётся через auth-proxy (bearer) + ingress /p/<port>/...;
в SELF_AUTHED_PORTS добавляется порт auth-proxy, не самого раннера.
"""

import json
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

CFG_PATH = os.environ.get("RUNNER_CONFIG")
if not CFG_PATH:
    sys.exit("RUNNER_CONFIG не задан")
CFG = json.loads(Path(CFG_PATH).read_text(encoding="utf-8"))
CWD = Path(CFG.get("cwd", os.getcwd()))
PROFILE = CFG.get("profile", "default")
RUN_DIR = Path(os.environ.get("TEMP", "/tmp")) / "rdm-runner"
RUN_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_TIMEOUT = int(CFG.get("timeout", 900))

mcp = FastMCP(
    f"devbox-runner-{PROFILE}",
    host="127.0.0.1",
    port=int(os.environ.get("RUNNER_PORT", "8797")),
)


def _audit(line: str) -> None:
    with open(RUN_DIR / "audit.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")


def _safe_path(value: str) -> str:
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"path-аргумент должен быть относительным без '..': {value!r}")
    return str(p)


def _build_argv(spec: dict, kwargs: dict) -> list[str]:
    argv = [str(x) for x in spec["cmd"]]
    for name, arg in (spec.get("args") or {}).items():
        if name not in kwargs:
            continue
        val = kwargs[name]
        if arg.get("type") == "path":
            val = _safe_path(str(val))
        pos = arg.get("position", "append")
        if pos == "append":
            argv.append(str(val))
        else:
            argv = [str(val) if e == "{" + name + "}" else e for e in argv]
    return argv


def _make_tool(name: str, spec: dict):
    argnames = list((spec.get("args") or {}).keys())

    def run(**kwargs):
        present = {k: v for k, v in kwargs.items() if v is not None}
        argv = _build_argv(spec, present)
        _audit(f"{name} argv={argv}")
        if spec.get("background"):
            pidfile = RUN_DIR / f"{PROFILE}-{name}.pid"
            log = RUN_DIR / f"{PROFILE}-{name}.log"
            with open(log, "ab") as lf:
                proc = subprocess.Popen(
                    argv, cwd=CWD, stdout=lf, stderr=lf,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            pidfile.write_text(str(proc.pid))
            return json.dumps({
                "started": True, "pid": proc.pid,
                "port": spec.get("port"), "log": str(log),
            })
        proc = subprocess.run(
            argv, cwd=CWD, capture_output=True, timeout=DEFAULT_TIMEOUT,
        )
        _audit(f"{name} exit={proc.returncode}")
        return json.dumps({
            "exit_code": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", "replace")[-8000:],
            "stderr": proc.stderr.decode("utf-8", "replace")[-4000:],
        })

    run.__name__ = f"run_{name.replace(':', '_').replace('-', '_')}"
    run.__doc__ = (spec.get("description") or f"runner: {name}") + (
        f"\nargs: {argnames}" if argnames else "\nargs: нет")
    # явная сигнатура: FastMCP строит схему аргументов из signature
    params = [
        inspect.Parameter(a, inspect.Parameter.KEYWORD_ONLY,
                          default=None, annotation=str | None)
        for a in argnames
    ]
    run.__signature__ = inspect.Signature(parameters=params, return_annotation=str)
    run.__annotations__ = {a: str | None for a in argnames} | {"return": str}
    return run


@mcp.tool()
def runner_list() -> str:
    """Список объявленных команд раннера (имя, argv, args, background, port)."""
    return json.dumps(CFG.get("commands", []), ensure_ascii=False, indent=2)


for _cmd in CFG.get("commands", []):
    _fn = _make_tool(_cmd["name"], _cmd)
    mcp.add_tool(_fn, name=_fn.__name__)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
