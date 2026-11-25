"""Контракт-тест на API `mcp` — пин `mcp>=2.3,<3`.

Инцидент: `MCPServer(transport_security=...)` собирался по API 1.x и падал на
TypeError в рантайме — import-shim маскировал дрейф до запуска. В 2.x
`transport_security` — kwarg `run()` (per-transport), а не конструктора.
Этот тест падает сразу при смене API, а не при первом старте раннера.
"""

from __future__ import annotations

import inspect

from mcp.server.mcpserver import MCPServer


def test_transport_security_lives_in_run_kwargs():
    # run(transport="streamable-http", **kwargs) форвардит kwargs в
    # run_streamable_http_async — там transport_security и живёт
    params = inspect.signature(MCPServer.run_streamable_http_async).parameters
    assert "transport_security" in params


def test_transport_security_not_in_constructor():
    init_params = inspect.signature(MCPServer.__init__).parameters
    assert "transport_security" not in init_params
