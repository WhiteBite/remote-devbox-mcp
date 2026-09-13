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
import hashlib
import os
import re
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
AUDIT_LOG = RUN_DIR / "audit.log"
CHAIN = RUN_DIR / "audit.chain"

# Политика безопасности аргументов агента (mcp-shell-server pattern):
# exec-векторы отклоняются даже внутри разрешённой команды.
DENY_ARG = [
    r"find\b.*-exec", r"\bxargs\b", r"awk\b.*system\s*\(",
    r"tar\b.*--checkpoint-action", r"\bgit\s+-c\b",
    r"Invoke-Expression", r"\biex\b", r"certutil\s+-urlcache",
    r"(curl|wget)\b.*\|\s*(sh|bash|powershell|pwsh)",
]
SECRETISH = re.compile(
    r"(token|key|secret|password|passwd|bearer)\s*[=:]\s*\S+|^[A-Fa-f0-9]{32,}$", re.I)


def _audit(obj) -> None:
    """JSONL-аудит с SHA-256 цепочкой (tamper-evident) и ротацией 10МБ."""
    try:
        if AUDIT_LOG.exists() and AUDIT_LOG.stat().st_size > 10 * 1024 * 1024:
            AUDIT_LOG.rename(RUN_DIR / "audit.log.1")
            CHAIN.unlink(missing_ok=True)
    except OSError:
        pass
    prev = CHAIN.read_text().strip() if CHAIN.exists() else "0" * 64
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": obj, "prev": prev}
    h = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
    entry["hash"] = h
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        CHAIN.write_text(h)
    except OSError:
        pass


def _redact_argv(argv):
    return ["[REDACTED]" if SECRETISH.search(a) else a for a in argv]


def _check_args(values) -> None:
    for v in values:
        s = str(v)
        for pat in DENY_ARG:
            if re.search(pat, s, re.I):
                raise ValueError(f"аргумент отклонён политикой безопасности: {s!r}")


def _child_env(spec):
    """Минимальное окружение ребёнка: системный минимум + env команды."""
    base = {}
    for k in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP",
              "USERPROFILE", "HOME", "JAVA_HOME", "FLUTTER_ROOT"):
        if os.environ.get(k):
            base[k] = os.environ[k]
    base.update(spec.get("env") or {})
    return base

mcp = FastMCP(
    f"devbox-runner-{PROFILE}",
    host="127.0.0.1",
    port=int(os.environ.get("RUNNER_PORT", "8797")),
)


def _norm(d):
    """Ключи из PowerShell ConvertTo-Json приходят с капитализацией профиля."""
    return {(k.lower() if isinstance(k, str) else k): v for k, v in (d or {}).items()}


def _quote_cmd(a: str) -> str:
    """Экранирование аргумента для cmd.exe только когда необходимо:
    простые аргументы передаём как есть (иначе npm получит литеральные кавычки)."""
    if not any(c in a for c in ' &|<>()^%!"'):
        return a
    a = a.replace('"', '""')
    for ch in "&|<>()^%!":
        a = a.replace(ch, "^" + ch)
    return f'"{a}"'


def _resolve(argv: list[str]) -> list[str]:
    """npm/npx на Windows = .cmd-шимы; shutil.which матчит и бесрасширенный
    sh-скрипт (WinError 193), поэтому ищем только явные расширения.
    argv остаётся allowlist-ным; аргументы экранируются для cmd."""
    import shutil
    exe = None
    if os.name == "nt":
        for ext in (".exe", ".cmd", ".bat"):
            exe = shutil.which(argv[0] + ext)
            if exe:
                break
    exe = exe or shutil.which(argv[0]) or argv[0]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", " ".join([exe] + [_quote_cmd(a) for a in argv[1:]])]
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


def _tool_name(name: str) -> str:
    return f"run_{name.replace(':', '_').replace('-', '_')}"


def _make_tool(name: str, spec: dict):
    argnames = list((spec.get("args") or {}).keys())
    timeout = int(spec.get("timeout", DEFAULT_TIMEOUT))

    def run(**kwargs):
        present = {k: v for k, v in kwargs.items() if v is not None}
        _check_args(present.values())
        argv = _build_argv(spec, present)
        _audit({"tool": name, "argv": _redact_argv(argv)})
        argv = _resolve(argv)
        env = _child_env(spec)
        if spec.get("background"):
            pidfile = RUN_DIR / f"{PROFILE}-{name}.pid"
            log = RUN_DIR / f"{PROFILE}-{name}.log"
            with open(log, "ab") as lf:
                proc = subprocess.Popen(
                    argv, cwd=CWD, env=env, stdout=lf, stderr=lf,
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            pidfile.write_text(str(proc.pid))
            return json.dumps({
                "started": True, "pid": proc.pid,
                "port": spec.get("port"), "log": str(log),
            })
        proc = subprocess.run(
            argv, cwd=CWD, env=env, capture_output=True, timeout=timeout,
        )
        _audit({"tool": name, "exit": proc.returncode})
        return json.dumps({
            "exit_code": proc.returncode,
            "stdout": proc.stdout.decode("utf-8", "replace")[-64000:],
            "stderr": proc.stderr.decode("utf-8", "replace")[-32000:],
        })

    run.__name__ = _tool_name(name)
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


def _make_kill_tool(name: str):
    def kill():
        pidfile = RUN_DIR / f"{PROFILE}-{name}.pid"
        if not pidfile.exists():
            return json.dumps({"killed": False, "reason": "no pidfile"})
        pid = int(pidfile.read_text().strip())
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)])
        else:
            try:
                pgid = os.getpgid(pid)
                if pgid != os.getpgid(0):
                    os.killpg(pgid, 9)
                else:
                    os.kill(pid, 9)
            except OSError:
                pass
        try:
            pidfile.unlink()
        except OSError:
            pass
        _audit(f"{name}_kill pid={pid}")
        return json.dumps({"killed": True, "pid": pid})

    kill.__name__ = _tool_name(name) + "_kill"
    kill.__doc__ = f"kill background runner: {name}"
    kill.__signature__ = inspect.Signature(return_annotation=str)
    kill.__annotations__ = {"return": str}
    return kill


@mcp.tool()
def runner_list() -> str:
    """Список объявленных команд раннера (имя, argv, args, background, port)."""
    return json.dumps(CFG.get("commands", []), ensure_ascii=False, indent=2)


for _cmd in CFG.get("commands", []):
    _cmd = _norm(_cmd)
    _cmd["args"] = {k: _norm(v) for k, v in (_cmd.get("args") or {}).items()}
    _fn = _make_tool(_cmd["name"], _cmd)
    mcp.add_tool(_fn, name=_fn.__name__)
    if _cmd.get("background"):
        _kill_fn = _make_kill_tool(_cmd["name"])
        mcp.add_tool(_kill_fn, name=_kill_fn.__name__)

# именованные скрипты профиля ($Scripts) → run_script_<name>
for _sc in CFG.get("scripts", []):
    _sc = _norm(_sc)
    _fn = _make_tool("script:" + _sc["name"], _sc)
    mcp.add_tool(_fn, name=_fn.__name__)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
