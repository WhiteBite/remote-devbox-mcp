"""Live SSE idle gate for the MCP gateway: real processes, 5-minute idle hold.

Run: python scripts/live_sse_idle_gate.py  (exit 0 = gate passed)

Isolation: the gateway child spawned by procman.start_gateway inherits only
_CHILD_ENV_KEYS, so the temp environment rides on USERPROFILE/TEMP (allowlisted)
plus a copied home/ tree whose .env points at the temp profile.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOLD_SECONDS = 300.0
HEARTBEAT_WINDOW = 20.0
MID_HOLD_AT = 150.0
MIN_HEARTBEATS = 15

ECHO_FAKE = r'''
import json, os, sys
pidfile = sys.argv[1]
with open(pidfile, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
tools = [{"name": "echo", "inputSchema": {"type": "object"}}]
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    m = msg.get("method")
    mid = msg.get("id")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18",
              "capabilities": {"tools": {}}, "serverInfo": {"name": "echo-fake", "version": "0"}}})
    elif m == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}})
    elif m == "tools/call":
        args = msg["params"].get("arguments") or {}
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
            {"type": "text", "text": json.dumps(args)}]}})
'''


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _free_port_pair() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port + 1))
            except OSError:
                continue
        return port


def _post(port: int, token: str, body: dict, headers: dict) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("POST", "/mcp", json.dumps(body), headers)
        response = conn.getresponse()
        data = response.read()
        return response.status, {name.lower(): value for name, value in response.getheaders()}, data
    finally:
        conn.close()


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return f"<no {path.name}>"


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="rdm-sse-gate-"))
    os.environ["USERPROFILE"] = str(tmp_root)
    os.environ["TEMP"] = str(tmp_root)
    os.environ["TMP"] = str(tmp_root)
    tempfile.tempdir = str(tmp_root)

    started_at = _stamp()
    report: dict[str, object] = {"tmp_root": str(tmp_root)}
    gateway_pid: int | None = None
    curl: subprocess.Popen | None = None
    procman = None
    try:
        home_copy = tmp_root / "home"
        shutil.copytree(
            REPO_ROOT / "home", home_copy, ignore=shutil.ignore_patterns("__pycache__", ".env")
        )
        (home_copy / ".env").write_text("ACTIVE_PROFILE=echo-fake\n", encoding="utf-8")

        project = tmp_root / "project"
        project.mkdir()
        pidfile = tmp_root / "echo-fake.pid"
        server_entry = {"type": "local", "command": [sys.executable, "-c", ECHO_FAKE, str(pidfile)]}
        global_config = tmp_root / ".config" / "opencode" / "opencode.json"
        global_config.parent.mkdir(parents=True)
        global_config.write_text(json.dumps({"mcp": {"echo-fake": server_entry}}), encoding="utf-8")
        (project / "opencode.json").write_text(
            json.dumps({"mcp": {"echo-fake": server_entry}}), encoding="utf-8"
        )

        profiles_dir = tmp_root / ".devbox" / "projects"
        profiles_dir.mkdir(parents=True)
        gateway_port = _free_port_pair()
        profile_path = profiles_dir / "echo-fake.json"
        profile_path.write_text(
            json.dumps(
                {
                    "project_dir": str(project),
                    "exposed_mcps": [
                        {"name": "echo-fake", "mode": "readonly", "tool_allow": ["echo"]}
                    ],
                    "gateway_port": gateway_port,
                }
            ),
            encoding="utf-8",
        )

        sys.path.insert(0, str(home_copy))
        from rdm import hostos, profiles
        from rdm import procman as procman_mod

        procman = procman_mod
        os.environ["RDM_PROJECTS_DIR"] = str(profiles_dir)
        from rdm.gateway.snapshot import save_snapshot

        profile = profiles.load(profile_path)
        save_snapshot(profile_path, profile, global_config=global_config)

        token = "sse-gate-" + os.urandom(16).hex()
        gateway_pid = procman.start_gateway(profile, token, home_copy)
        report["gateway_pid"] = gateway_pid

        healthy = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not healthy:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", gateway_port, timeout=3)
                conn.request("GET", "/healthz", headers={"Authorization": f"Bearer {token}"})
                response = conn.getresponse()
                response.read()
                conn.close()
                healthy = response.status == 200
            except OSError:
                time.sleep(0.3)
        if not healthy:
            print(f"FAIL: gateway never answered /healthz on {gateway_port}")
            print(f"--- gateway.err ---\n{_tail(tmp_root / 'rdm-gateway' / 'gateway.err')}")
            return 1

        status, headers, data = _post(
            gateway_port,
            token,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "sse-gate", "version": "0"},
                },
            },
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        session_id = headers.get("mcp-session-id")
        if status != 200 or not session_id:
            print(f"FAIL: initialize -> {status} {data[:200]!r}")
            print(f"--- gateway.err ---\n{_tail(tmp_root / 'rdm-gateway' / 'gateway.err')}")
            return 1
        report["session_id"] = session_id

        echo_pid = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and echo_pid is None:
            if pidfile.exists():
                echo_pid = int(pidfile.read_text(encoding="utf-8").strip())
            else:
                time.sleep(0.2)
        if echo_pid is None:
            print("FAIL: echo-fake child never started (no pidfile)")
            print(f"--- gateway.err ---\n{_tail(tmp_root / 'rdm-gateway' / 'gateway.err')}")
            return 1
        report["echo_fake_pid"] = echo_pid

        hold_started = time.monotonic()
        stderr_path = tmp_root / "curl.err"
        stderr_handle = open(stderr_path, "wb")
        curl = subprocess.Popen(
            [
                "curl",
                "-N",
                "-sS",
                "--max-time",
                str(int(HOLD_SECONDS)),
                "-H",
                f"Authorization: Bearer {token}",
                "-H",
                f"Mcp-Session-Id: {session_id}",
                f"http://127.0.0.1:{gateway_port}/mcp",
            ],
            stdout=subprocess.PIPE,
            stderr=stderr_handle,
        )
        stderr_handle.close()
        lines: list[tuple[float, str]] = []

        def reader() -> None:
            assert curl.stdout is not None
            for raw in curl.stdout:
                lines.append((time.monotonic(), raw.decode("utf-8", "replace").rstrip("\r\n")))

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        tools_latency_ms: int | None = None
        tools_ok = False
        while curl.poll() is None and time.monotonic() - hold_started < HOLD_SECONDS + 20:
            if tools_latency_ms is None and time.monotonic() - hold_started >= MID_HOLD_AT:
                began = time.monotonic()
                status, _, data = _post(
                    gateway_port,
                    token,
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "Mcp-Session-Id": session_id,
                    },
                )
                tools_latency_ms = int((time.monotonic() - began) * 1000)
                tools_ok = status == 200 and any(
                    tool.get("name") == "echo-fake__echo"
                    for tool in json.loads(data).get("result", {}).get("tools", [])
                )
            time.sleep(0.5)

        if curl.poll() is None:
            curl.kill()
        curl_exit = curl.wait(timeout=30)
        hold_ended = time.monotonic()
        reader_thread.join(timeout=5)
        ended_at = _stamp()

        heartbeats = [stamp for stamp, line in lines if "heartbeat" in line]
        gaps = [right - left for left, right in zip(heartbeats, heartbeats[1:], strict=False)]
        first_heartbeat_delay = heartbeats[0] - hold_started if heartbeats else None
        max_gap = max(gaps) if gaps else None

        pids_file = tmp_root / "rdm-gateway" / "pids.txt"
        recorded = []
        if pids_file.exists():
            for line in pids_file.read_text(encoding="utf-8").splitlines():
                fields = line.split("|", 2)
                if fields and fields[0].isdigit():
                    recorded.append(int(fields[0]))
        procman.stop_gateway()
        watched = sorted(set(recorded + [echo_pid]))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(
            hostos.create_time(pid) is not None for pid in watched
        ):
            time.sleep(0.2)
        orphans = [pid for pid in watched if hostos.create_time(pid) is not None]

        report.update(
            {
                "started_at": started_at,
                "ended_at": ended_at,
                "hold_seconds": round(hold_ended - hold_started, 1),
                "curl_exit": curl_exit,
                "heartbeat_count": len(heartbeats),
                "first_heartbeat_delay_s": (
                    round(first_heartbeat_delay, 1) if first_heartbeat_delay is not None else None
                ),
                "max_heartbeat_gap_s": round(max_gap, 1) if max_gap is not None else None,
                "mid_hold_tools_list_latency_ms": tools_latency_ms,
                "mid_hold_tools_list_ok": tools_ok,
                "watched_pids": watched,
                "orphan_pids": orphans,
            }
        )

        checks = {
            "curl_exit_28_stream_stayed_open": curl_exit == 28,
            "heartbeat_count_ge_15": len(heartbeats) >= MIN_HEARTBEATS,
            "first_heartbeat_within_20s": (
                first_heartbeat_delay is not None and first_heartbeat_delay <= HEARTBEAT_WINDOW
            ),
            "max_heartbeat_gap_within_20s": max_gap is not None and max_gap <= HEARTBEAT_WINDOW,
            "mid_hold_tools_list_ok": tools_ok,
            "no_orphan_pids": not orphans,
        }
        report["checks"] = checks

        print(json.dumps(report, indent=2))
        if all(checks.values()):
            shutil.rmtree(tmp_root, ignore_errors=True)
            print("LIVE SSE IDLE GATE: PASS")
            return 0
        print("LIVE SSE IDLE GATE: FAIL")
        print(f"--- gateway.err ---\n{_tail(tmp_root / 'rdm-gateway' / 'gateway.err')}")
        print(f"--- proxy.err ---\n{_tail(tmp_root / 'rdm-gateway' / 'proxy.err')}")
        print(f"--- curl.err ---\n{_tail(stderr_path)}")
        return 1
    finally:
        if procman is not None:
            procman.stop_gateway()
        if curl is not None and curl.poll() is None:
            curl.kill()


if __name__ == "__main__":
    sys.exit(main())
