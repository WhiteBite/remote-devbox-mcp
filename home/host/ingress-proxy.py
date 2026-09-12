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

import os
import re
import socket
import sys
import threading

TOKEN = os.environ.get("INGRESS_TOKEN")
if not TOKEN:
    sys.exit("INGRESS_TOKEN не задан")

LISTEN = ("127.0.0.1", int(os.environ.get("PROXY_PORT", "8799")))
SELF_AUTHED = {int(p) for p in os.environ.get("SELF_AUTHED_PORTS", "8792").split(",") if p}
EXPECTED = f"Bearer {TOKEN}".encode()
MAX_HEADER = 64 * 1024
ROUTE = re.compile(rb"^/p/(\d{1,5})(/.*)?$")


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
    up = None
    try:
        client.settimeout(60)
        up = None
        cur_port = None
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
            m = ROUTE.match(parts[1])
            if not m:
                client.sendall(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            port = int(m.group(1))
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
            if port not in SELF_AUTHED and headers.get(b"authorization") != EXPECTED:
                client.sendall(
                    b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            if cur_port != port:
                # cloudflared переиспользует одно соединение под запросы к
                # разным портам: апстрим переподключаем при смене цели
                if up is not None:
                    try:
                        up.close()
                    except OSError:
                        pass
                up = socket.create_connection(("127.0.0.1", port), timeout=10)
                up.settimeout(None)
                threading.Thread(target=_pipe_up, args=(up, client), daemon=True).start()
                cur_port = port
            up.sendall(b"\r\n".join(out) + b"\r\n\r\n")
            cl = int(headers.get(b"content-length") or 0)
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
    print(f"ingress-proxy :{LISTEN[1]} -> /p/<port>/* (bearer; self-authed: {sorted(SELF_AUTHED)})", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
