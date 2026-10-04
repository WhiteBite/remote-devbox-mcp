"""Chain diagnostics for the devbox host side."""

from __future__ import annotations

import json
import pathlib
import secrets

from rdm import docker, hostos, netprobe, profiles
from rdm.freeze import app_dir

_HOME = app_dir()
_PROJECTS = profiles.PROJECTS_DIR
_DEFAULT_COMPOSE = str(_HOME / "docker-compose.yml")
_INGRESS_PORT = 8799


def _first_pid(path: pathlib.Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        fields = line.split("|", 2)
        if fields and fields[0].isdigit():
            return int(fields[0])
    return None


class _Report:
    def __init__(self) -> None:
        self.failed = False

    def check(self, name: str, ok: bool, hint: str = "") -> None:
        if ok:
            print(f"[PASS] {name}")
        else:
            self.failed = True
            print(f"[FAIL] {name} — {hint}")


def run(env_map: dict[str, str], compose_file: str | None = None, prober=None) -> int:
    compose_file = compose_file or _DEFAULT_COMPOSE
    probe = prober or netprobe.probe_http
    report = _Report()
    report.check("docker engine", docker.run("info").returncode == 0, "Docker Desktop не запущен")
    report.check("psutil", hostos.psutil is not None, "python -m pip install psutil")
    keys = ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN", "PROJECT_DIR")
    report.check(".env keys", all(env_map.get(key) for key in keys), "заполни токены и PROJECT_DIR в home/.env")
    report.check(
        "token lengths >=24",
        all(len(env_map.get(key, "")) >= 24 for key in ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN")),
        "токен короче 24 символов",
    )
    active = env_map.get("ACTIVE_PROFILE", "")
    profile_path = _PROJECTS / f"{active}.json"
    report.check("profile exists", bool(active) and profile_path.exists(), "devbox.py use <имя>")
    if active and profile_path.exists():
        problems = [p for p in profiles.validate(profiles.load(profile_path)) if not p.startswith("WARN")]
        report.check("profile validation", not problems, "; ".join(problems))
    report.check("override yml", (_HOME / "docker-compose.override.yml").exists(), "devbox.py use <имя>")
    toolbox = docker.compose("ps", "toolbox", "--format", "{{.Status}}", compose_file=compose_file).stdout
    report.check("toolbox healthy", "healthy" in toolbox, "docker compose up -d toolbox")
    if env_map.get("VLESS_SUB_URL"):
        vpn = docker.compose("ps", "vpn", "--format", "{{.Status}}", compose_file=compose_file).stdout
        report.check("vpn healthy", "healthy" in vpn, "docker compose logs vpn")
    ingress_pid = _first_pid(hostos.tempdir() / "rdm-ingress" / "pids.txt")
    report.check(
        "ingress pid",
        ingress_pid is not None and hostos.owned(ingress_pid, None, "rdm.proxy"),
        "devbox.py ingress start",
    )
    report.check("ingress listen 8799", netprobe.can_connect(_INGRESS_PORT), "перезапусти devbox.py ingress start")
    url = netprobe.ingress_url(env_map)
    report.check("ingress url", bool(url), "docker compose logs cloudflared-ingress")
    if url:
        report.check(
            "bridge via ingress 200",
            probe(f"{url}/p/{profiles.BRIDGE_PORT}/healthz", env_map.get("MCP_BEARER_TOKEN")) == 200,
            "docker compose logs toolbox",
        )
        report.check(
            "ingress auth 401",
            probe(f"{url}/p/{profiles.BRIDGE_PORT}/mcp", "wrong-" + secrets.token_hex(8)) == 401,
            "проверь MCP_BEARER_TOKEN/INGRESS_TOKEN",
        )
        report.check(
            "allowlist 403",
            probe(f"{url}/p/1/", env_map.get("INGRESS_TOKEN")) == 403,
            "проверь ALLOWED_PORTS",
        )
    pids_path = hostos.tempdir() / "rdm-host" / f"{active}-pids.txt"
    dead = 0
    try:
        for line in pids_path.read_text(encoding="utf-8").splitlines():
            fields = line.split("|", 2)
            if not fields or not fields[0].isdigit():
                continue
            marker = fields[2] if len(fields) > 2 else ""
            recorded = float(fields[1]) if len(fields) > 1 and fields[1] else None
            if not hostos.owned(int(fields[0]), recorded, marker):
                dead += 1
    except FileNotFoundError:
        pass
    report.check("host services alive", dead == 0, "devbox.py use <имя> перезапустит")
    gitleaks = hostos.tempdir() / "rdm-host" / f"gitleaks-{active}.json"
    if gitleaks.exists():
        try:
            findings = json.loads(gitleaks.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            findings = []
        if findings:
            print(f"[WARN] gitleaks: найдено секретов {len(findings)} — проверь DenyMounts")
        else:
            print("[PASS] gitleaks clean")
    else:
        print("[SKIP] gitleaks отчёт ещё не готов")
    return 1 if report.failed else 0