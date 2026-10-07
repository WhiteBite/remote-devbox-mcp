#!/usr/bin/env python3
"""mcp-stdio-adapter.py — мост devbox как обычный MCP-сервер по stdio.

Stock MCP-клиент (OpenCode/Codex/Claude) запускает скрипт и говорит с ним
JSON-RPC 2.0 построчно: запрос — JSON-строка в stdin, ответ — JSON-строка в
stdout, логи — только в stderr. Джобы и разрешения не переизобретаются: их
доводит mcp_client.settle_job (auto = --trust).

Контракт:
  * initialize → protocolVersion/capabilities/serverInfo; capabilities клиента
    запоминаются в CLIENT_CAPABILITIES.
  * tools/list → прокси апстрима, каждому тулу добавляется поле "mutating"
    (true для write/edit/apply_patch/bash/webfetch); если апстрим недоступен —
    минимальный список из TOOLS.md (10 native + 6 control).
  * tools/call → ответ-джоб доводится до конца и возвращается конкретным
    результатом {"content": [{"type": "text", ...}], "isError": ...};
    не-джоб → mc.flatten(raw). Мутация без --trust: если клиент заявил
    capability elicitation, адаптер спрашивает его запросом
    elicitation/create (accept → "once", decline/cancel → "reject",
    отказ → isError «мутация отклонена»); без elicitation джоб в
    awaiting_permission → isError с подсказкой включить trust.
  * --readonly отказывает write/edit/apply_patch/bash/webfetch
    isError-результатом, не вызывая апстрим.
  * неизвестный метод → JSON-RPC -32601.

  python3 arena/mcp-stdio-adapter.py --url https://x.trycloudflare.com/p/8787/mcp \
      --token <T> --trust
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_client as mc

SERVER_NAME = "devbox-bridge-adapter"
SERVER_VERSION = "1.0.0"
MUTATING_TOOLS = frozenset({"write", "edit", "apply_patch", "bash", "webfetch"})

CLIENT_CAPABILITIES: dict = {}

FALLBACK_TOOLS = [
    {"name": name, "description": desc, "inputSchema": {"type": "object"}}
    for name, desc in (
        ("read", "чтение файла"),
        ("write", "перезапись файла целиком"),
        ("edit", "точечная замена (oldString/newString)"),
        ("apply_patch", "патч-блок по нескольким файлам"),
        ("glob", "поиск имён файлов по шаблону"),
        ("grep", "ripgrep по содержимому"),
        ("bash", "команды на devbox (command)"),
        ("webfetch", "загрузка URL (permission-gated)"),
        ("todowrite", "рабочий список агента"),
        ("lsp", "языковые операции (hover, findReferences, …)"),
        ("opencode_native_info", "информация о мосте и окружении"),
        ("opencode_job_list", "список джобов"),
        ("opencode_job_result", "состояние/результат джоба (wait_seconds до 50)"),
        ("opencode_job_cancel", "отмена джоба"),
        ("opencode_permissions_pending", "ожидающие разрешения запросы"),
        ("opencode_permission_reply", "ответить на запрос разрешения джоба"),
    )
]


def _ok(request: dict, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}


def _err(request: dict, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request.get("id"),
            "error": {"code": code, "message": message}}


def _tool_result(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _annotate(tools: list[dict]) -> list[dict]:
    return [{**tool, "mutating": tool.get("name") in MUTATING_TOOLS} for tool in tools]


class ElicitationError(RuntimeError):
    """Клиент не завершил elicitation (обрыв stdin или JSON-RPC-ошибка)."""


class ElicitationBroker:
    """Ожидающие ответы на исходящие запросы адаптера (elicitation/create)."""

    def __init__(self):
        self._open: set[str] = set()
        self._responses: dict[str, dict] = {}
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        rid = f"elicit-{self._counter}"
        self._open.add(rid)
        return rid

    def route(self, msg) -> bool:
        if not isinstance(msg, dict) or "method" in msg:
            return False
        if msg.get("id") not in self._open:
            return False
        self._open.discard(msg["id"])
        self._responses[msg["id"]] = msg
        return True

    def response(self, rid: str) -> dict | None:
        return self._responses.pop(rid, None)


class _ElicitPermission:
    """on_permission для settle_job: вопрос клиенту через elicitation/create."""

    def __init__(self, tool: str, elicit):
        self.tool = tool
        self.elicit = elicit
        self.denied = False

    def __call__(self, job: dict) -> str:
        perm = job.get("permission") or {}
        patterns = ", ".join(perm.get("patterns") or [])
        message = f"devbox: разрешить мутацию {self.tool}"
        if patterns:
            message += f" ({patterns})"
        result = self.elicit(message, {"type": "object", "properties": {}})
        action = (result or {}).get("action")
        self.denied = action != "accept"
        return "once" if action == "accept" else "reject"


@contextlib.contextmanager
def _stdout_guard():
    # settle_job печатает запрос разрешения в stdout; в JSON-RPC-канале это фатально
    saved, sys.stdout = sys.stdout, sys.stderr
    try:
        yield
    finally:
        sys.stdout = saved


def _job_result(job: dict) -> dict:
    status = job.get("status")
    exit_code = ((job.get("result") or {}).get("metadata") or {}).get("exit")
    output = ((job.get("result") or {}).get("output") or "").strip()
    is_error = status != "completed" or exit_code not in (None, 0)
    if status == "awaiting_permission":
        text = (f"джоб {job.get('job_id')} ждёт разрешения; клиент не поддерживает "
                f"elicitation, перезапусти адаптер с --trust")
    else:
        text = output or mc.job_brief(job)
    return _tool_result(text, is_error=is_error)


def _call_tool(client, params: dict, *, trust: bool, readonly: bool,
               elicit=None) -> dict:
    name = params.get("name")
    if readonly and name in MUTATING_TOOLS:
        return _tool_result(f"тул {name!r} мутирующий; адаптер запущен с --readonly",
                            is_error=True)
    handler = None
    if elicit is not None and not trust and "elicitation" in CLIENT_CAPABILITIES:
        handler = _ElicitPermission(name, elicit)
    try:
        raw = client.call(name, params.get("arguments") or {})
        job = mc.parse_job(raw)
        if job is None:
            return _tool_result(mc.flatten(raw), is_error=bool(raw.get("isError")))
        if handler is None and not trust:
            with _stdout_guard():
                job = mc.settle_job(client, job, auto=trust)
        else:
            job = mc.settle_job(client, job, auto=trust, on_permission=handler)
    except ElicitationError as e:
        return _tool_result(f"elicitation не удался: {e}", is_error=True)
    except RuntimeError as e:
        return _tool_result(f"ошибка апстрима: {e}", is_error=True)
    if handler is not None and handler.denied:
        return _tool_result(f"мутация {name!r} отклонена клиентом (elicitation)",
                            is_error=True)
    return _job_result(job)


def handle(request: dict, client, *, trust: bool, readonly: bool,
           elicit=None) -> dict | None:
    """Диспетчер JSON-RPC 2.0: не трогает stdin/stdout; client — с call()/list_tools().

    elicit(message, requested_schema) — round-trip elicitation/create к клиенту
    (брокер из serve); возвращает {action: ...} или кидает ElicitationError.
    """
    if "id" not in request:
        return None
    method = request.get("method")
    if method == "initialize":
        capabilities = dict((request.get("params") or {}).get("capabilities") or {})
        CLIENT_CAPABILITIES.clear()
        CLIENT_CAPABILITIES.update(capabilities)
        if "elicitation" in capabilities:
            print("[adapter] клиент поддерживает elicitation: мутации без --trust "
                  "будут запрашиваться", file=sys.stderr)
        return _ok(request, {
            "protocolVersion": mc.PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "tools/list":
        try:
            tools = client.list_tools()
        except RuntimeError as e:
            print(f"[adapter] апстрим недоступен, минимальный список: {e}",
                  file=sys.stderr)
            tools = []
        return _ok(request, {"tools": _annotate(tools or FALLBACK_TOOLS)})
    if method == "tools/call":
        return _ok(request, _call_tool(client, request.get("params") or {},
                                       trust=trust, readonly=readonly, elicit=elicit))
    return _err(request, -32601, f"метод не поддерживается: {method!r}")


def _handle_line(line: str, broker: ElicitationBroker, dispatch) -> None:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        print(json.dumps(_err({"id": None}, -32700, f"ошибка разбора JSON: {e}")),
              flush=True)
        return
    if not broker.route(msg):
        dispatch(msg)


def serve(client, *, trust: bool, readonly: bool) -> None:
    broker = ElicitationBroker()

    def dispatch(msg: dict) -> None:
        response = handle(msg, client, trust=trust, readonly=readonly, elicit=elicit)
        if response is not None:
            print(json.dumps(response), flush=True)

    def elicit(message: str, requested_schema: dict) -> dict:
        rid = broker.next_id()
        print(json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": "elicitation/create",
            "params": {"message": message, "requestedSchema": requested_schema},
        }), flush=True)
        while True:
            response = broker.response(rid)
            if response is not None:
                break
            line = sys.stdin.readline()
            if not line:
                raise ElicitationError("клиент закрыл поток во время elicitation")
            line = line.strip()
            if line:
                _handle_line(line, broker, dispatch)
        if "error" in response:
            err = response["error"]
            raise ElicitationError(
                f"клиент ответил ошибкой: {err.get('code')} {err.get('message')}")
        return response.get("result") or {}

    for line in iter(sys.stdin.readline, ""):
        line = line.strip()
        if line:
            _handle_line(line, broker, dispatch)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Мост devbox как MCP-сервер по stdio (JSON-RPC 2.0).")
    parser.add_argument("--url", help="URL MCP-моста (или переменная MCP_URL)")
    parser.add_argument("--token", help="токен авторизации (или переменная MCP_TOKEN)")
    parser.add_argument("--trust", action="store_true",
                        help="авто-одобрение разрешений (аналог --auto у mcp_client)")
    parser.add_argument("--readonly", action="store_true",
                        help="отказывать write/edit/apply_patch/bash/webfetch с isError")
    args = parser.parse_args(argv)
    url = args.url or os.environ.get("MCP_URL")
    token = args.token if args.token is not None else os.environ.get("MCP_TOKEN")
    if not url:
        print("укажи --url или MCP_URL", file=sys.stderr)
        return mc.EX_CONFIG
    client = mc.MCPClient(mc.HttpTransport(url, token))
    try:
        client.initialize()
        serve(client, trust=args.trust, readonly=args.readonly)
    except RuntimeError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return mc.EX_GENERIC
    finally:
        client.close()
    return mc.EX_OK


if __name__ == "__main__":
    sys.exit(main())
