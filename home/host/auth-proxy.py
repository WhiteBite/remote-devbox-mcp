#!/usr/bin/env python3
"""auth-proxy — TCP-прокси с Bearer-проверкой для локальных MCP-сервисов.

Стоит на loopback перед сервисом без авторизации (streamable-http MCP,
статика и т.п.) и наружу выходит только через туннель. Чужак без токена
получает 401, не доходя до сервиса. Пайпинг сырой TCP: SSE и длинные
стримы проходят как есть.

Env: MCP_PUBLIC_TOKEN (обязателен), PROXY_PORT (8792), TARGET_PORT (8790),
TARGET_HOST (127.0.0.1). Host заголовок переписывается на цель — FastMCP
в SDK проверяет Host (DNS-rebinding protection) и иначе отбивает чужой
публичный hostname с 421.
"""

import datetime
import os
import socket
import sys
import tempfile
import threading

TOKEN = os.environ.get("MCP_PUBLIC_TOKEN")
if not TOKEN:
    sys.exit("MCP_PUBLIC_TOKEN не задан")

LISTEN = ("127.0.0.1", int(os.environ.get("PROXY_PORT", "8792")))
TARGET_HOST = os.environ.get("TARGET_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "8790"))
EXPECTED = f"Bearer {TOKEN}".encode()
MAX_HEADER = 64 * 1024
MAX_BODY = int(os.environ.get("MAX_BODY_MB", "50")) * 1024 * 1024
NEW_HOST = f"Host: {TARGET_HOST}:{TARGET_PORT}".encode()
BODYLESS = frozenset((b"HEAD", b"GET", b"DELETE", b"OPTIONS", b"TRACE", b"CONNECT"))
ACCESS_LOG = os.environ.get(
    "ACCESS_LOG", os.path.join(tempfile.gettempdir(), "rdm-auth", "access.log")
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


def _forward_body(src: socket.socket, dst: socket.socket, n: int) -> None:
    left = n
    while left > 0:
        d = src.recv(min(65536, left))
        if not d:
            break
        dst.sendall(d)
        left -= len(d)


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
    # По-запросная обработка: cloudflared держит keep-alive к origin и
    # переиспользует соединение для разных клиентов — Bearer и Host-rewrite
    # нужны на каждом запросе, а не один раз на коннект.
    up = None
    pipe_thread = None
    pipe_stop = None
    try:
        client.settimeout(60)
        up = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=10)
        up.settimeout(None)
        pipe_stop = threading.Event()
        pipe_thread = threading.Thread(
            target=_pipe_up, args=(up, client, pipe_stop), daemon=True
        )
        pipe_thread.start()
        dbg = os.environ.get("PROXY_DEBUG_LOG")
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
            if dbg:
                with open(dbg, "ab") as f:
                    f.write(b"=== in ===\n" + head + b"\n")
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ", 2)
            if len(parts) < 2:
                return
            headers = {}
            out = []
            for line in lines:
                k, _, v = line.partition(b":")
                key = k.strip().lower()
                if key:
                    headers[key] = v.strip()
                # uvicorn доверяет X-Forwarded-Host (cloudflared его
                # проставляет) — вырезаем, иначе FastMCP отбивает 421
                if key == b"host":
                    out.append(NEW_HOST)
                elif key in (b"x-forwarded-host",):
                    continue
                else:
                    out.append(line)

            _access_log(TARGET_PORT, parts[0], parts[1], "self")

            # MCP spec: валидируем Origin (DNS-rebinding); наши клиенты без Origin
            if b"origin" in headers:
                client.sendall(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return

            if headers.get(b"authorization") != EXPECTED:
                client.sendall(
                    b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return

            if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
                client.sendall(
                    b"HTTP/1.1 501 Not Implemented\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return

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
            # carry уже может содержать начало тела (и даже следующий запрос):
            # досылаем ровно недостающее, остаток уходит в следующий виток
            if cl <= len(carry):
                up.sendall(carry[:cl])
                carry = carry[cl:]
            else:
                need = cl - len(carry)
                up.sendall(carry)
                carry = b""
                _forward_body(client, up, need)

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
    print(f"auth-proxy :{LISTEN[1]} -> {TARGET_HOST}:{TARGET_PORT} (bearer required)", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
