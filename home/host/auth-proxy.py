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

import os
import socket
import sys
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


def _forward_body(src: socket.socket, dst: socket.socket, n: int) -> None:
    left = n
    while left > 0:
        d = src.recv(min(65536, left))
        if not d:
            break
        dst.sendall(d)
        left -= len(d)


def _pipe_up(up: socket.socket, client: socket.socket) -> None:
    try:
        while True:
            d = up.recv(65536)
            if not d:
                break
            client.sendall(d)
    except OSError:
        pass


def handle(client: socket.socket) -> None:
    # По-запросная обработка: cloudflared держит keep-alive к origin и
    # переиспользует соединение для разных клиентов — Bearer и Host-rewrite
    # нужны на каждом запросе, а не один раз на коннект.
    up = None
    try:
        client.settimeout(60)
        up = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=10)
        up.settimeout(None)
        threading.Thread(target=_pipe_up, args=(up, client), daemon=True).start()
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
            if headers.get(b"authorization") != EXPECTED:
                client.sendall(
                    b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            up.sendall(b"\r\n".join(out) + b"\r\n\r\n")
            cl = int(headers.get(b"content-length") or 0)
            if cl > MAX_BODY:
                client.sendall(
                    b"HTTP/1.1 413 Payload Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
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
