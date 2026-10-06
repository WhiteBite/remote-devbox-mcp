#!/usr/bin/env python3
"""runner-mcp — host-MCP для команд проекта, объявленных в профиле devbox.

Конфиг: JSON-путь в env RUNNER_CONFIG (рендерит home/devbox.py use <имя>
из поля $RunnerCommands профиля). Каждая команда = argv-массив; аргументы
агента подставляются ТОЛЬКО как элементы argv (shell=False); для .cmd/.bat
шимов Windows (npm/npx) запуск идёт через `cmd.exe /c` с отдельной валидацией
аргументов на cmd-опасные символы (% ! " перевод строки), .exe запускаются
напрямую. type=path валидируется на выход за cwd. Background-команды детачатся,
pid-файл хранит `pid|create_time|marker`; kill-тул сверяет владение процессом
(cmdline-маркер или create_time) и не трогает чужой/переиспользованный pid.
Каждый вызов логируется в %TEMP%\\rdm-runner\\audit.log.

Наружу отдаётся через auth-proxy (bearer) + ingress /p/<port>/...;
в SELF_AUTHED_PORTS добавляется порт auth-proxy, не самого раннера.
"""

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from rdm import hostos
from rdm.profiles import runner_kill_tool_name, runner_tool_name
from rdm.runner.audit import _audit
from rdm.runner.policy import _check_args, _check_cmd_shim, _redact_argv


@dataclass(frozen=True)
class _Ctx:
    cwd: Path
    run_dir: Path
    profile: str
    default_timeout: int
    bg: dict[int, subprocess.Popen] = field(default_factory=dict)


def _prune_bg(ctx: _Ctx) -> None:
    for stale, child in list(ctx.bg.items()):
        if child.poll() is not None:
            del ctx.bg[stale]


def _norm(d):
    """Ключи из PowerShell ConvertTo-Json приходят с капитализацией профиля."""
    return {(k.lower() if isinstance(k, str) else k): v for k, v in (d or {}).items()}


def _shim_cmdline(exe: str, args: list[str]) -> str:
    """Сырая командная строка для cmd-шимов: list2cmdline экранирует наши
    кавычки бэкслешами (\"), и аргумент приходит в .cmd с литеральными
    кавычками; поэтому шимовый путь собираем строкой и квотим сами. `"`/`%`/`!`
    в аргументах уже отклонены _check_cmd_shim, внутри кавычек метасимволы
    cmd литеральны. Внешняя пара кавычек обязательна: иначе cmd /c срезает
    первую и последнюю кавычки строки и ломает путь шима."""
    parts = [f'"{exe}"']
    for a in args:
        parts.append(f'"{a}"' if any(c in a for c in " \t&|<>()^") else a)
    return f'cmd /c "{" ".join(parts)}"'


def _tail(stream, limit: int) -> str:
    size = stream.seek(0, os.SEEK_END)
    stream.seek(max(0, size - limit))
    return stream.read().decode("utf-8", "replace")


def _resolve(argv: list[str], cwd: Path) -> list[str] | str:
    """npm/npx на Windows = .cmd-шимы; shutil.which матчит и бесрасширенный
    sh-скрипт (WinError 193), поэтому ищем только явные расширения.
    Относительный exe Windows резолвит от cwd процесса (папка home), а не от
    cwd команды — поэтому сначала делаем его абсолютным от cwd проекта.
    argv остаётся allowlist-ным; .cmd-путь собирается в сырую строку
    (_shim_cmdline), .exe идёт списком."""
    first = argv[0]
    if not os.path.isabs(first):
        candidate = cwd / first
        if candidate.exists():
            first = str(candidate)
    exe = None
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat"):
            exe = shutil.which(first + ext)
            if exe:
                break
    exe = exe or shutil.which(first) or first
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return _shim_cmdline(exe, argv[1:])
    return [exe] + argv[1:]


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


def _child_env(spec: dict) -> dict[str, str]:
    """Минимальное окружение ребёнка: системный минимум + env команды."""
    base = {}
    for k in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP",
              "USERPROFILE", "HOME", "JAVA_HOME", "FLUTTER_ROOT"):
        if os.environ.get(k):
            base[k] = os.environ[k]
    base.update(spec.get("env") or {})
    return base


def _tool_name(name: str) -> str:
    return runner_tool_name(name)


def _make_tool(name: str, spec: dict, ctx: _Ctx):
    argnames = list((spec.get("args") or {}).keys())
    timeout = int(spec.get("timeout") or ctx.default_timeout)

    def run(**kwargs):
        present = {k: v for k, v in kwargs.items() if v is not None}
        _check_args(present.values())
        argv = _build_argv(spec, present)
        _audit({"tool": name, "argv": _redact_argv(argv)}, ctx.run_dir)
        resolved = _resolve(argv, ctx.cwd)
        if isinstance(resolved, str):
            _check_cmd_shim(argv[1:])
        argv = resolved
        env = _child_env(spec)
        if spec.get("background"):
            pidfile = ctx.run_dir / f"{ctx.profile}-{name}.pid"
            log = ctx.run_dir / f"{ctx.profile}-{name}.log"
            with open(log, "ab") as lf:
                proc = subprocess.Popen(
                    argv, cwd=ctx.cwd, env=env, stdout=lf, stderr=lf,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    start_new_session=os.name != "nt",
                )
            pidfile.write_text(
                f"{proc.pid}|{hostos.create_time(proc.pid) or ''}|{' '.join(argv)}",
                encoding="utf-8",
            )
            _prune_bg(ctx)
            ctx.bg[proc.pid] = proc
            return json.dumps({
                "started": True, "pid": proc.pid,
                "port": spec.get("port"), "log": str(log),
            })
        with tempfile.TemporaryFile() as out_file, tempfile.TemporaryFile() as err_file:
            proc = subprocess.Popen(
                argv, cwd=ctx.cwd, env=env, stdout=out_file, stderr=err_file,
                start_new_session=os.name != "nt",
            )
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                hostos.kill_tree(proc.pid)
                proc.wait()
            stdout = _tail(out_file, 64000)
            stderr = _tail(err_file, 32000)
        if timed_out:
            _audit({"tool": name, "exit": "timeout"}, ctx.run_dir)
            return json.dumps({
                "exit_code": 124,
                "stdout": stdout,
                "stderr": (stderr + f"\n[timeout] дерево процессов убито по таймауту {timeout}s").lstrip("\n"),
            })
        _audit({"tool": name, "exit": proc.returncode}, ctx.run_dir)
        return json.dumps({
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        })

    run.__name__ = _tool_name(name)
    run.__doc__ = (spec.get("description") or f"runner: {name}") + (
        f"\nargs: {argnames}" if argnames else "\nargs: нет")
    # FastMCP строит схему аргументов из signature, а не из **kwargs
    params = [
        inspect.Parameter(a, inspect.Parameter.KEYWORD_ONLY,
                          default=None, annotation=str | None)
        for a in argnames
    ]
    run.__signature__ = inspect.Signature(parameters=params, return_annotation=str)
    run.__annotations__ = {a: str | None for a in argnames} | {"return": str}
    return run


def _make_kill_tool(name: str, ctx: _Ctx):
    def kill():
        pidfile = ctx.run_dir / f"{ctx.profile}-{name}.pid"
        try:
            text = pidfile.read_text(encoding="utf-8")
        except FileNotFoundError:
            return json.dumps({"killed": False, "reason": "no pidfile"})
        fields = text.strip().split("|", 2)
        if len(fields) != 3 or not fields[0].isdigit():
            pidfile.unlink(missing_ok=True)
            return json.dumps({"killed": False, "reason": "pidfile повреждён (нет pid|create_time|marker)"})
        pid = int(fields[0])
        try:
            recorded = float(fields[1]) if fields[1] else None
        except ValueError:
            recorded = None
        if not hostos.owned(pid, recorded, fields[2]):
            pidfile.unlink(missing_ok=True)
            return json.dumps({"killed": False, "pid": pid, "reason": "процесс мёртв или pid переиспользован"})
        hostos.kill_tree(pid)
        proc = ctx.bg.get(pid)
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        _prune_bg(ctx)
        pidfile.unlink(missing_ok=True)
        _audit(f"{name}_kill pid={pid}", ctx.run_dir)
        return json.dumps({"killed": True, "pid": pid})

    kill.__name__ = runner_kill_tool_name(name)
    kill.__doc__ = f"kill background runner: {name}"
    kill.__signature__ = inspect.Signature(return_annotation=str)
    kill.__annotations__ = {"return": str}
    return kill


def main() -> None:
    cfg_path = os.environ.get("RUNNER_CONFIG")
    if not cfg_path:
        sys.exit("RUNNER_CONFIG не задан")
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    ctx = _Ctx(
        cwd=Path(cfg.get("cwd", os.getcwd())),
        run_dir=Path(os.environ.get("TEMP", "/tmp")) / "rdm-runner",
        profile=cfg.get("profile", "default"),
        default_timeout=int(cfg.get("timeout", 900)),
    )
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    mcp = FastMCP(
        f"devbox-runner-{ctx.profile}",
        host="127.0.0.1",
        port=int(os.environ.get("MCP_HTTP_PORT", "8797")),
    )

    @mcp.tool()
    def runner_list() -> str:
        """Список объявленных команд раннера (имя, argv, args, background, port)."""
        return json.dumps(cfg.get("commands", []), ensure_ascii=False, indent=2)

    for _cmd in cfg.get("commands", []):
        _cmd = _norm(_cmd)
        _cmd["args"] = {k: _norm(v) for k, v in (_cmd.get("args") or {}).items()}
        _fn = _make_tool(_cmd["name"], _cmd, ctx)
        mcp.add_tool(_fn, name=_fn.__name__)
        if _cmd.get("background"):
            _kill_fn = _make_kill_tool(_cmd["name"], ctx)
            mcp.add_tool(_kill_fn, name=_kill_fn.__name__)

    for _sc in cfg.get("scripts", []):
        _sc = _norm(_sc)
        _fn = _make_tool("script:" + _sc["name"], _sc, ctx)
        mcp.add_tool(_fn, name=_fn.__name__)
        if _sc.get("background"):
            _kill_fn = _make_kill_tool("script:" + _sc["name"], ctx)
            mcp.add_tool(_kill_fn, name=_kill_fn.__name__)

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
