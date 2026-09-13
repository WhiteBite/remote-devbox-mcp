#!/usr/bin/env python3
"""ingress-proxy — одна публичная точка входа на все loopback-эндпоинты хоста.

Маршрутизация по пути: /p/<port>/<rest> -> 127.0.0.1:<port>/<rest>
(префикс срезается). Новый эндпоинт не требует ни конфига, ни рестарта:
достаточно поднять сервис на loopback-порту — URL сразу детерминированный.

Авторизация: Bearer INGRESS_TOKEN на каждый запрос. Порты из
SELF_AUTHED_PORTS (напр. 8792 с собственным auth-proxy) роутер не
авторизует сам — заголовок уходит транзитом, решение принимает нижний слой.

Публичный hostname переписывается в Host цели, X-Forwarded-Host вырезается
(FastMCP/uvicorn иначе отбивают 421). Тело форвардится carry-буфером:
keep-alive соединения cloudflared переиспользуются под разные запросы.

Env: INGRESS_TOKEN (обязателен), PROXY_PORT (8793), SELF_AUTHED_PORTS ("8792").
"""

import datetime
import os
import re
import socket
import sys
import tempfile
import threading

TOKEN = os.environ.get("INGRESS_TOKEN")
if not TOKEN:
    sys.exit("INGRESS_TOKEN не задан")

LISTEN = ("127.0.0.1", int(os.environ.get("PROXY_PORT", "8799")))
SELF_AUTHED = {int(p) for p in os.environ.get("SELF_AUTHED_PORTS", "8792").split(",") if p}
# fail-closed: порты, routable через ingress с ingress-токеном.
# Пусто = все non-self-authed порты запрещены (403).
ALLOWED = {int(p) for p in os.environ.get("ALLOWED_PORTS", "").split(",") if p}
MAX_BODY = int(os.environ.get("MAX_BODY_MB", "50")) * 1024 * 1024
EXPECTED = f"Bearer {TOKEN}".encode()
MAX_HEADER = 64 * 1024
ROUTE = re.compile(rb"^/p/(\d{1,5})(/.*)?$")
MANIFEST_PATH = b"/p/9000/manifest.json"
BODYLESS = frozenset((b"HEAD", b"GET", b"DELETE", b"OPTIONS", b"TRACE", b"CONNECT"))
ACCESS_LOG = os.environ.get(
    "ACCESS_LOG", os.path.join(tempfile.gettempdir(), "rdm-ingress", "access.log")
)


def _access_log(port, method, path, auth_mode):
    try:
        d = os.path.dirname(ACCESS_LOG)
        os.makedirs(d, exist_ok=True)
        if os.path.exists(ACCESS_LOG) and os.path.getsize(ACCESS_LOG) > 10 * 1024 * 1024:
            os.rename(ACCESS_LOG, ACCESS_LOG + ".1")
        with open(ACCESS_LOG, "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.datetime.now().isoformat()} {port} "
                f"{method} {path[:120]} {auth_mode}\n"
            )
    except OSError:
        pass


def _forward_body(src: socket.socket, dst: socket.socket, n: int) -> bool:
    """False = заявленное тело больше MAX_BODY — вызывающий шлёт 413."""
    if n > MAX_BODY:
        return False
    left = n
    while left > 0:
        d = src.recv(min(65536, left))
        if not d:
            break
        dst.sendall(d)
        left -= len(d)
    return True


def _pipe_up(up: socket.socket, client: socket.socket, stop: threading.Event) -> None:
    try:
        while True:
            d = up.recv(65536)
            if not d:
                break
            if stop.is_set():
                break
            client.sendall(d)
    except OSError:
        pass


def _pipe_down(client: socket.socket, up: socket.socket) -> None:
    try:
        while True:
            d = client.recv(65536)
            if not d:
                break
            up.sendall(d)
    except OSError:
        pass


def _stop_pipe(pipe_thread, pipe_stop, close_up, up):
    if pipe_thread is not None:
        if pipe_stop is not None:
            pipe_stop.set()
        if close_up and up is not None:
            try:
                up.close()
            except OSError:
                pass
        pipe_thread.join(2)
    elif close_up and up is not None:
        try:
            up.close()
        except OSError:
            pass


def handle(client: socket.socket) -> None:
    up = None
    pipe_thread = None
    pipe_stop = None
    cur_port = None
    try:
        client.settimeout(60)
        carry = b""
        while True:
            while b"\r\n\r\n" not in carry:
                chunk = client.recv(65536)
                if not chunk:
                    return
                carry += chunk
                if len(carry) > MAX_HEADER:
                    return
            head, _, carry = carry.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ", 2)
            if len(parts) < 2:
                return

            if parts[1] == MANIFEST_PATH:
                _access_log(9000, parts[0], parts[1], "none")
                manifest_env = os.environ.get("RDM_MANIFEST_PATH")
                if not manifest_env or not os.path.isfile(manifest_env):
                    client.sendall(
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return
                auth_ok = False
                if any(l.strip().lower().startswith(b"origin:") for l in lines[1:]):
                    client.sendall(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return
                for line in lines[1:]:
                    k, _, v = line.partition(b":")
                    if k.strip().lower() == b"authorization" and v.strip() == EXPECTED:
                        auth_ok = True
                        break
                if not auth_ok:
                    client.sendall(
                        b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return
                with open(manifest_env, "rb") as f:
                    body = f.read()
                client.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
                return

            m = ROUTE.match(parts[1])
            if not m:
                client.sendall(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            port = int(m.group(1))
            if not 1 <= port <= 65535:
                client.sendall(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            rest = m.group(2) or b"/"

            headers = {}
            out = [parts[0] + b" " + rest + b" " + parts[2]]
            for line in lines[1:]:
                k, _, v = line.partition(b":")
                key = k.strip().lower()
                if key:
                    headers[key] = v.strip()
                if key == b"host":
                    out.append(f"Host: 127.0.0.1:{port}".encode())
                elif key == b"x-forwarded-host":
                    continue
                else:
                    out.append(line)

            auth_mode = "self" if port in SELF_AUTHED else "ingress"
            _access_log(port, parts[0], parts[1], auth_mode)

            # MCP spec: сервер валидирует Origin (DNS-rebinding); наши клиенты без Origin
            if b"origin" in headers:
                client.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return

            if port not in SELF_AUTHED:
                if headers.get(b"authorization") != EXPECTED:
                    client.sendall(
                        b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return
                if port not in ALLOWED:
                    client.sendall(
                        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return

            if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
                client.sendall(
                    b"HTTP/1.1 501 Not Implemented\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return

            if cur_port != port:
                _stop_pipe(pipe_thread, pipe_stop, True, up)
                pipe_thread = None
                pipe_stop = None
                up = socket.create_connection(("127.0.0.1", port), timeout=10)
                up.settimeout(None)
                pipe_stop = threading.Event()
                pipe_thread = threading.Thread(
                    target=_pipe_up, args=(up, client, pipe_stop), daemon=True
                )
                pipe_thread.start()
                cur_port = port

            cl = int(headers.get(b"content-length") or 0)
            if parts[0] in BODYLESS:
                cl = 0
            if cl > MAX_BODY:
                client.sendall(
                    b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return

            if headers.get(b"expect", b"").lower() == b"100-continue":
                if cl <= MAX_BODY:
                    client.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                out = [
                    line for line in out
                    if line.split(b":", 1)[0].strip().lower() != b"expect"
                ]

            up.sendall(b"\r\n".join(out) + b"\r\n\r\n")
            if cl <= len(carry):
                up.sendall(carry[:cl])
                carry = carry[cl:]
            else:
                need = cl - len(carry)
                up.sendall(carry)
                carry = b""
                if not _forward_body(client, up, need):
                    client.sendall(
                        b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    return

            if (
                headers.get(b"upgrade", b"").lower() == b"websocket"
                and b"upgrade" in headers.get(b"connection", b"").lower()
            ):
                _stop_pipe(pipe_thread, pipe_stop, False, up)
                pipe_stop = threading.Event()
                pipe_thread = threading.Thread(
                    target=_pipe_up, args=(up, client, pipe_stop), daemon=True
                )
                pipe_thread.start()
                _pipe_down(client, up)
                return
    except (OSError, ValueError):
        pass
    finally:
        try:
            client.close()
        except OSError:
            pass
        if up is not None:
            try:
                up.close()
            except OSError:
                pass


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(64)
    print(f"ingress-proxy :{LISTEN[1]} -> /p/<port>/* (bearer; self-authed: {sorted(SELF_AUTHED)})", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
