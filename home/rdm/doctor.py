"""Chain diagnostics for the devbox host side."""

from __future__ import annotations

import json
import pathlib
import secrets

from rdm import docker, hostos, netprobe, ports, profiles
from rdm.freeze import app_dir

_HOME = app_dir()
_PROJECTS = profiles.PROJECTS_DIR
_DEFAULT_COMPOSE = str(_HOME / "docker-compose.yml")
_INGRESS_PORT = 8799
_LOOPBACK_IPS = frozenset({"127.0.0.1", "::1", "0.0.0.0", "::"})


def _first_entry(path: pathlib.Path) -> tuple[int, float | None, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        fields = line.split("|", 2)
        if fields and fields[0].isdigit():
            try:
                recorded = float(fields[1]) if len(fields) > 1 and fields[1] else None
            except ValueError:
                recorded = None
            marker = fields[2] if len(fields) > 2 else ""
            return (int(fields[0]), recorded, marker)
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


def _port_allowed(profile: profiles.Profile, port: int) -> bool:
    if port in profile.port_deny:
        return False
    return port in profile.allowed_ports or any(lo <= port <= hi for lo, hi in profile.port_ranges)


def _registry_ports(project_dir: str) -> frozenset[int] | None:
    if not project_dir:
        return None
    found: set[int] = set()
    seen = False
    for path in pathlib.Path(project_dir).glob("tools/*/registry.json"):
        seen = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ops = data.get("ops") if isinstance(data, dict) else None
        if not isinstance(ops, dict):
            continue
        found.update(
            spec["port"] for spec in ops.values() if isinstance(spec, dict) and isinstance(spec.get("port"), int)
        )
    return frozenset(found) if seen else None


def _runner_check(profile: profiles.Profile, report: _Report) -> None:
    if not profile.runner_commands:
        return
    port = profile.runner_port or ports.DEFAULT_RUNNER_PORT
    try:
        listening = netprobe.can_connect(port)
    except OSError:
        print(f"[WARN] runner {port}: проба невозможна")
        return
    report.check(f"runner listen {port}", listening, "devbox.py start-host, затем devbox.py doctor")


def _ui_port_check(profile: profiles.Profile, report: _Report) -> None:
    if profile.ui_port is None:
        return
    ui_port = profile.ui_port
    allowed = ui_port in profile.allowed_ports or any(lo <= ui_port <= hi for lo, hi in profile.port_ranges)
    report.check(
        f"ui_port {ui_port}",
        allowed and ui_port not in profile.port_deny,
        f"devbox.py allow {ui_port} --ui",
    )


def _drift_warns(profile: profiles.Profile, registry: frozenset[int]) -> None:
    for port in sorted(registry):
        if port in profile.port_deny:
            continue
        if not _port_allowed(profile, port):
            print(f"[WARN] drift: порт {port} в registry, но не в политике профиля — devbox.py allow {port}")
    for port in profile.allowed_ports:
        if port in profile.port_deny:
            continue
        if any(lo <= port <= hi for lo, hi in profile.port_ranges):
            continue
        if port not in registry:
            print(f"[WARN] drift: порт {port} в allowed_ports без потребителя в registry — перенеси в port_ranges или убери")


def _reverse_scan_warns(profile: profiles.Profile, registry: frozenset[int] | None) -> None:
    if hostos.psutil is None:
        return
    live: set[int] = set()
    try:
        connections = hostos.psutil.net_connections(kind="tcp")
    except (OSError, hostos.psutil.AccessDenied):
        print("[WARN] scan: net_connections недоступен — пропуск")
        return
    for conn in connections:
        if conn.status != hostos.psutil.CONN_LISTEN or not conn.laddr:
            continue
        if conn.laddr.ip in _LOOPBACK_IPS:
            live.add(conn.laddr.port)
    for port in sorted(live):
        if any(lo <= port <= hi for lo, hi in profile.port_ranges) and port in profile.port_deny:
            print(f"[WARN] scan: порт {port} жив, в port_ranges и port_deny — диапазон его не открывает")
        if registry is not None and port in registry and port not in profile.port_deny and not _port_allowed(profile, port):
            print(f"[WARN] scan: порт {port} жив (loopback, registry), но закрыт политикой — devbox.py allow {port}")


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
    profile: profiles.Profile | None = None
    if active and profile_path.exists():
        profile = profiles.load(profile_path)
        problems = [p for p in profiles.validate(profile) if not p.startswith("WARN")]
        report.check("profile validation", not problems, "; ".join(problems))
    report.check("override yml", (_HOME / "docker-compose.override.yml").exists(), "devbox.py use <имя>")
    toolbox = docker.compose("ps", "toolbox", "--format", "{{.Status}}", compose_file=compose_file).stdout
    report.check("toolbox healthy", "healthy" in toolbox, "docker compose up -d toolbox")
    if env_map.get("VLESS_SUB_URL"):
        vpn = docker.compose("ps", "vpn", "--format", "{{.Status}}", compose_file=compose_file).stdout
        report.check("vpn healthy", "healthy" in vpn, "docker compose logs vpn")
    ingress_entry = _first_entry(hostos.tempdir() / "rdm-ingress" / "pids.txt")
    report.check(
        "ingress pid",
        ingress_entry is not None and hostos.owned(*ingress_entry),
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
    if profile is not None:
        _runner_check(profile, report)
        _ui_port_check(profile, report)
        registry = _registry_ports(profile.project_dir)
        if registry is not None:
            _drift_warns(profile, registry)
        _reverse_scan_warns(profile, registry)
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