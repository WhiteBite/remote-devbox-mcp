#!/usr/bin/env python3
"""
mcp_client.py — минимальный MCP-клиент без внешних зависимостей (Python 3.10+).

Зачем: агенту (мне) нужно самому вызывать инструменты удалённой машины.
Платформенной поддержки внешних MCP может не быть, но это и не обязательно:
протокол MCP — это JSON-RPC поверх транспорта, а транспорт доступен мне
через bash. Этот файл реализует клиент целиком, чтобы от «у меня есть URL
и токен» до «инструмент вызван» не было посредников и лишних агентов.

Поддерживается:
  * streamable HTTP  (POST /mcp, ответ JSON или SSE)
  * stdio            (локальный сервер как подпроцесс)
  * протокол джобов и разрешений моста opencode-toolbox (run/job/reply)

Примеры:
  python3 mcp_client.py --url https://x.trycloudflare.com/mcp --token T check
  python3 mcp_client.py --url ... --token T list
  python3 mcp_client.py --url ... --token T schema edit
  python3 mcp_client.py --url ... --token T run read '{"filePath":"src/Main.java"}'
  python3 mcp_client.py --url ... --token T run --auto bash '{"command":"./gradlew test"}'

Переменные окружения вместо флагов: MCP_URL, MCP_TOKEN, MCP_HEADER
(имя заголовка авторизации, по умолчанию Authorization; значение получает
префикс Bearer, если MCP_BEARER=1).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Описания тулов содержат юникод; Windows-консоли (cp1251 и т.п.) его не пользуют.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PROTOCOL_VERSION = "2025-06-18"


# ──────────────────────────────────────────────────────────────────────
# Транспорты
# ──────────────────────────────────────────────────────────────────────

class HttpTransport:
    """Streamable HTTP: один POST на запрос, ответ — JSON либо SSE-поток."""

    name = "http"

    # Ошибки края Cloudflare «ориджин недоступен»: запрос НЕ доставлен мосту,
    # повтор безопасен (в отличие от таймаута чтения — тот не ретраим).
    RETRYABLE_STATUS = frozenset({502, 520, 521, 523, 524, 530})

    def __init__(self, url: str, token: str | None = None,
                 header: str = "Authorization", bearer: bool = True,
                 timeout: float = 120.0, retries: int = 4):
        self.url = url
        self.token = token
        self.header = header
        self.bearer = bearer
        self.timeout = timeout
        self.retries = retries
        self.session_id: str | None = None

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.token:
            h[self.header] = f"Bearer {self.token}" if self.bearer else self.token
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def send(self, payload: dict) -> dict | None:
        data = json.dumps(payload).encode()
        for attempt in range(self.retries + 1):
            if attempt:
                pause = min(2 ** attempt, 12)
                print(f"[retry {attempt}/{self.retries}] через {pause} с",
                      file=sys.stderr)
                time.sleep(pause)
            req = urllib.request.Request(self.url, data=data,
                                         headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        self.session_id = sid
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    body = resp.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:600]
                if e.code in self.RETRYABLE_STATUS and attempt < self.retries:
                    print(f"[!] HTTP {e.code}: туннель временно недоступен",
                          file=sys.stderr)
                    continue
                raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail}") from None
            except urllib.error.URLError as e:
                if attempt < self.retries:
                    print(f"[!] нет соединения: {e.reason}", file=sys.stderr)
                    continue
                raise RuntimeError(f"нет соединения: {e.reason}") from None

            if not body.strip():
                return None                       # это было уведомление
            if "text/event-stream" in ctype:
                return self._parse_sse(body)
            return json.loads(body)
        raise RuntimeError("исчерпаны повторы")   # недостижимо: цикл либо return, либо raise

    @staticmethod
    def _parse_sse(body: str) -> dict | None:
        result = None
        for line in body.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    msg = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if "result" in msg or "error" in msg:
                    result = msg
        return result


class StdioTransport:
    """Локальный MCP-сервер как подпроцесс: JSON-строка на запрос."""

    name = "stdio"

    def __init__(self, command: list[str], cwd: str | None = None):
        self.proc = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        self._id = 0

    def send(self, payload: dict) -> dict | None:
        if "id" not in payload:               # уведомление
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
            return None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("сервер закрыл поток")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("id") == payload.get("id"):
                return msg

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# Клиент
# ──────────────────────────────────────────────────────────────────────

class MCPClient:
    def __init__(self, transport):
        self.t = transport
        self._id = 0
        self.server_info: dict = {}
        self.capabilities: dict = {}

    def _next(self) -> int:
        self._id += 1
        return self._id

    def initialize(self) -> dict:
        res = self.t.send({
            "jsonrpc": "2.0", "id": self._next(), "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"roots": {"listChanged": False},
                                 "sampling": {}},
                "clientInfo": {"name": "arena-agent-client", "version": "1.0.0"},
            },
        })
        if not res or "error" in res:
            raise RuntimeError(f"initialize не удался: {res}")
        self.server_info = res["result"].get("serverInfo", {})
        self.capabilities = res["result"].get("capabilities", {})
        self.t.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res["result"]

    def list_tools(self) -> list[dict]:
        res = self.t.send({"jsonrpc": "2.0", "id": self._next(),
                           "method": "tools/list", "params": {}})
        return (res or {}).get("result", {}).get("tools", [])

    def call(self, name: str, arguments: dict) -> dict:
        res = self.t.send({"jsonrpc": "2.0", "id": self._next(),
                           "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
        if not res:
            raise RuntimeError("пустой ответ")
        if "error" in res:
            raise RuntimeError(f"ошибка протокола: {res['error']}")
        return res["result"]

    def close(self):
        if hasattr(self.t, "close"):
            self.t.close()


# ──────────────────────────────────────────────────────────────────────
# Вывод
# ──────────────────────────────────────────────────────────────────────

def flatten(result: dict) -> str:
    """MCP возвращает content-блоки; склеиваем в один текст."""
    parts = []
    for block in result.get("content", []) or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "resource":
            parts.append(json.dumps(block.get("resource", {}), ensure_ascii=False))
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    if result.get("isError"):
        parts.insert(0, "[ИНСТРУМЕНТ ВЕРНУЛ ОШИБКУ]")
    return "\n".join(parts)


def parse_job(result: dict) -> dict | None:
    """Мост opencode-toolbox возвращает не текст, а структурированный джоб.

    Вытаскиваем JSON из text-блока: job_id + status (+ permission / result).
    Возвращаем None, если ответ не похож на джоб (обычный сервер тулов).
    """
    for block in result.get("content", []) or []:
        if block.get("type") != "text":
            continue
        try:
            data = json.loads(block.get("text", ""))
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and "job_id" in data:
            return data
    return None


def permission_id(perm: dict) -> str | None:
    """Мост (JobView) кладёт разрешение в {id, permission, patterns, metadata}."""
    return perm.get("id") or perm.get("permission_id")


def job_brief(job: dict) -> str:
    status = job.get("status")
    line = f"job {job.get('job_id')} [{status}]"
    perm = job.get("permission") or {}
    if perm:
        ptype = perm.get("permission") or perm.get("type") or ""
        detail = ", ".join(perm.get("patterns") or []) or perm.get("detail", "")
        line += f"\n  разрешение: {permission_id(perm)} ({ptype}) → {detail}"
    res = job.get("result") or {}
    if res:
        exit_code = (res.get("metadata") or {}).get("exit")
        out = (res.get("output") or "").strip()
        line += f"\n  exit={exit_code}\n{out}"
    return line


def settle_job(client, job: dict, auto: bool,
               max_polls: int = 60, delay: float = 1.0,
               wait_seconds: int = 45) -> dict:
    """Довести джоб до конца.

    Мост на write/edit/apply_patch/bash отвечает awaiting_permission:
    нужен opencode_permission_reply, затем opencode_job_result.
    Без auto — останавливаемся на запросе и печатаем, чем ответить.

    opencode_job_result поддерживает серверное ожидание wait_seconds (≤50),
    поэтому один опрос покрывает до 45 с сборки: 60 опросов ≈ 45 минут.
    Счётчик итераций общий для всех статусов — цикл ограничен даже если
    ответ разрешения не принят и статус не меняется.
    """
    polls = 0
    while job.get("status") in ("awaiting_permission", "running", "cancelling"):
        polls += 1
        if polls > max_polls:
            print(f"[!] джоб не завершился за {max_polls} опросов", file=sys.stderr)
            return job
        if job.get("status") == "awaiting_permission":
            perm = (job.get("permission") or {})
            if not auto:
                print("ТРЕБУЕТСЯ РАЗРЕШЕНИЕ. Ответь:")
                print(f"  ./mcp reply {job['job_id']} {permission_id(perm)} once")
                return job
            job = parse_job(client.call("opencode_permission_reply", {
                "job_id": job["job_id"],
                "permission_id": permission_id(perm),
                "reply": "once"})) or job
            continue
        time.sleep(delay)
        job = parse_job(client.call("opencode_job_result", {
            "job_id": job["job_id"],
            "wait_seconds": wait_seconds})) or job
    return job


def print_tools(tools: list[dict]):
    if not tools:
        print("инструментов не найдено")
        return
    width = max(len(t["name"]) for t in tools)
    for t in tools:
        desc = (t.get("description") or "").strip().split("\n")[0]
        if len(desc) > 90:
            desc = desc[:87] + "..."
        print(f"  {t['name']:<{width}}  {desc}")
    print(f"\nвсего: {len(tools)}")


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def build_client(args) -> MCPClient:
    if args.command_stdio:
        t = StdioTransport(args.command_stdio, cwd=args.cwd)
    else:
        url = args.url or os.environ.get("MCP_URL")
        if not url:
            sys.exit("укажи --url или MCP_URL")
        token = args.token if args.token is not None else os.environ.get("MCP_TOKEN")
        header = args.header or os.environ.get("MCP_HEADER", "Authorization")
        bearer = not args.no_bearer
        t = HttpTransport(url, token, header, bearer, args.timeout, args.retries)
    return MCPClient(t)


def main():
    p = argparse.ArgumentParser(
        description="Минимальный MCP-клиент (streamable HTTP / stdio).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="URL MCP-эндпоинта, напр. https://x.trycloudflare.com/mcp")
    p.add_argument("--token", help="токен авторизации")
    p.add_argument("--header", help="имя заголовка авторизации (по умолчанию Authorization)")
    p.add_argument("--no-bearer", action="store_true", help="не добавлять префикс 'Bearer '")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--retries", type=int, default=4,
                   help="повторы при ошибках края туннеля (502/520/521/523/524/530) и обрыве соединения")
    p.add_argument("--stdio", dest="command_stdio", nargs="+",
                   help="вместо HTTP: команда локального MCP-сервера")
    p.add_argument("--cwd", help="рабочая директория для stdio-сервера")

    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("check", help="health + initialize + счётчик тулов")
    sub.add_parser("init", help="только рукопожатие")
    sub.add_parser("list", help="список инструментов")
    s = sub.add_parser("call", help="вызвать инструмент")
    s.add_argument("tool")
    s.add_argument("args", nargs="?", default="{}", help="JSON-аргументы")
    s.add_argument("--json", action="store_true", help="сырой JSON-ответ")
    s = sub.add_parser("schema", help="схема аргументов инструмента")
    s.add_argument("tool")

    # --- протокол джобов opencode-toolbox ---
    s = sub.add_parser("run", help="вызвать тул и довести джоб до конца")
    s.add_argument("tool")
    s.add_argument("args", nargs="?", default="{}", help="JSON-аргументы")
    s.add_argument("--auto", action="store_true",
                   help="самому отвечать 'once' на запрос разрешения")
    s.add_argument("--polls", type=int, default=60, help="сколько раз опрашивать джоб")
    s.add_argument("--delay", type=float, default=1.0, help="пауза между опросами, сек")
    s.add_argument("--wait-seconds", type=int, default=45,
                   help="серверное ожидание в opencode_job_result (максимум 50)")
    s = sub.add_parser("job", help="состояние/результат джоба")
    s.add_argument("job_id")
    s = sub.add_parser("reply", help="ответить на запрос разрешения")
    s.add_argument("job_id")
    s.add_argument("permission_id")
    s.add_argument("reply", nargs="?", default="once", choices=["once", "reject"])

    args = p.parse_args()
    client = build_client(args)
    try:
        handshake = client.initialize()

        if args.action in ("init", "check"):
            print(json.dumps({"serverInfo": client.server_info,
                              "capabilities": client.capabilities,
                              "protocolVersion": handshake.get("protocolVersion")},
                             ensure_ascii=False, indent=2))

        if args.action in ("list", "check"):
            tools = client.list_tools()
            print()
            print_tools(tools)

        if args.action == "call":
            try:
                arguments = json.loads(args.args)
            except json.JSONDecodeError as e:
                sys.exit(f"аргументы не JSON: {e}")
            result = client.call(args.tool, arguments)
            print(json.dumps(result, ensure_ascii=False, indent=2)
                  if args.json else flatten(result))

        if args.action == "schema":
            for t in client.list_tools():
                if t["name"] == args.tool:
                    print(json.dumps(t.get("inputSchema", {}), ensure_ascii=False, indent=2))
                    break
            else:
                sys.exit(f"инструмент {args.tool!r} не найден")

        if args.action == "run":
            try:
                arguments = json.loads(args.args)
            except json.JSONDecodeError as e:
                sys.exit(f"аргументы не JSON: {e}")
            raw = client.call(args.tool, arguments)
            job = parse_job(raw)
            if job is None:            # сервер без протокола джобов — обычный вызов
                print(flatten(raw))
            else:
                job = settle_job(client, job, args.auto, args.polls, args.delay,
                                 args.wait_seconds)
                print(job_brief(job))
                # Мост считает «completed» даже при ненулевом коде команды
                # (result.metadata.exit). Непройденный тест не должен выглядеть
                # как успех, поэтому падаем и в этом случае.
                exit_code = ((job.get("result") or {}).get("metadata") or {}).get("exit")
                if job.get("status") == "failed" or exit_code not in (None, 0):
                    sys.exit(1)

        if args.action == "job":
            job = parse_job(client.call("opencode_job_result", {"job_id": args.job_id}))
            print(job_brief(job) if job else "нет такого джоба (или сервер без джобов)")

        if args.action == "reply":
            job = parse_job(client.call("opencode_permission_reply", {
                "job_id": args.job_id, "permission_id": args.permission_id,
                "reply": args.reply}))
            print(job_brief(job) if job else "ответ не принят")
    except RuntimeError as e:
        sys.exit(f"ОШИБКА: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
