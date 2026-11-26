"""Гейт: каждый маршрут, рекламируемый манифестом, реально обслуживается прокси.

Инцидент: handoff рекламировал manifest/gateway, но прокси отдавал 404/403 —
агент получал обещания, которых нет. Правило: для каждого endpoint'а манифеста
либо путь — это router.MANIFEST_PATH, либо порт проходит policy-решение прокси
(`port in self_authed or is_port_allowed`), и никогда не попадает в deny/cockpit.
"""

from __future__ import annotations

from rdm import ports
from rdm.profiles import ExposedMcp, HostService, Profile, RunnerCommand
from rdm.proxy import router
from rdm.render import build_manifest


def _profile(**overrides) -> Profile:
    data = dict(
        host_services=(
            HostService(port=8792, auth="bearer", cmd="run"),
            HostService(port=8090, auth="open", cmd="ui"),
        ),
        runner_commands=(RunnerCommand(name="dev", cmd=("npm", "run", "dev"), port=35210),),
        runner_port=8796,
        allowed_ports=(8765,),
        port_ranges=((3000, 3999),),
        port_deny=(3999,),
        exposed_mcps=(ExposedMcp(name="fs", mode="ro", tool_allow=()),),
    )
    data.update(overrides)
    return Profile(project_dir=".", **data)


def _routed(policy: ports.PortPolicy, port: int) -> bool:
    """Решение прокси из proxy/server.py:_dispatch — зеркало прод-логики."""
    if port == ports.COCKPIT_PORT or port in policy.denied:
        return False
    return port in policy.self_authed or ports.is_port_allowed(policy, port)


def _check(manifest: dict, policy: ports.PortPolicy) -> list[str]:
    bad = []
    for ep in manifest["endpoints"]:
        path = ep["path"].encode()
        if router.is_manifest(path):
            continue
        route = router.parse_route(path)
        if route is None or not _routed(policy, route.port):
            bad.append(f"{ep['name']} {ep['path']}")
    return bad


def test_every_advertised_endpoint_routes():
    profile = _profile()
    policy = ports.compute_port_policy(profile)
    manifest = build_manifest(profile, "testprof", "https://x.example", "ingress")

    assert _check(manifest, policy) == []
    # sanity: манифест реально рекламирует все классы эндпоинтов
    names = {ep["name"] for ep in manifest["endpoints"]}
    assert {"manifest", "bridge", "gateway"} <= names
    assert any(n.startswith("host-") for n in names)
    assert any(n.startswith("allowed-") for n in names)


def test_manifest_path_matches_router_constant():
    manifest = build_manifest(_profile(), "p", "https://x", "ingress")
    advertised = next(ep["path"] for ep in manifest["endpoints"] if ep["name"] == "manifest")
    assert advertised.encode() == router.MANIFEST_PATH


def test_gate_catches_unrouted_port():
    """Саботаж-проверка: эндпоинт на неразрешённый порт обязан быть пойман."""
    profile = _profile()
    policy = ports.compute_port_policy(profile)
    manifest = build_manifest(profile, "p", "https://x", "ingress")
    manifest["endpoints"].append(
        {"name": "ghost", "port": 4999, "auth": "ingress", "path": "/p/4999/"}
    )
    assert _check(manifest, policy) == ["ghost /p/4999/"]


def test_chat_block_mentions_only_existing_tokens():
    """Инцидент: handoff рекламировал GATEWAY_TOKEN при отсутствии шлюза.

    Правило: имя токена в тексте chat_block <=> соответствующая поверхность
    передана (gateway_port / runner|self_authed).
    """
    from rdm.tokens import chat_block

    env = {"MCP_BEARER_TOKEN": "b" * 64, "MCP_PUBLIC_TOKEN": "h" * 64,
           "INGRESS_TOKEN": "i" * 64, "GATEWAY_TOKEN": "g" * 64}
    bare = chat_block(env, "https://x", full=False)
    assert "GATEWAY_TOKEN" not in bare
    assert "HOST_TOKEN" not in bare

    with_gw = chat_block(env, "https://x", full=False, gateway_port=8794)
    assert "GATEWAY_TOKEN" in with_gw
    assert "HOST_TOKEN" not in with_gw

    with_runner = chat_block(env, "https://x", full=False, runner_port=8796)
    assert "HOST_TOKEN" in with_runner
    assert "GATEWAY_TOKEN" not in with_runner


def test_manifest_contains_no_secret_values():
    """Манифест носит имена токенов, не значения — утечка значений = гейт."""
    import json

    manifest = build_manifest(_profile(), "p", "https://x", "ingress")
    blob = json.dumps(manifest)
    for value in ("b" * 64, "i" * 64, "g" * 64):
        assert value not in blob


def test_gateway_not_advertised_when_not_routed():
    """Если gateway-advertised порт не попадает в self_authed — гейт красный.

    Зеркало инцидента: agent получил 403 на /p/8794/mcp при пустой policy.
    """
    profile = _profile(exposed_mcps=())
    policy = ports.compute_port_policy(profile)
    manifest = build_manifest(profile, "p", "https://x", "ingress")
    assert _check(manifest, policy) == []
    # а если бы gateway всё же рекламировался — гейт обязан был бы упасть
    manifest["endpoints"].append(
        {"name": "gateway", "port": ports.DEFAULT_GATEWAY_PORT,
         "auth": "self", "path": f"/p/{ports.DEFAULT_GATEWAY_PORT}/mcp"}
    )
    if ports.DEFAULT_GATEWAY_PORT not in policy.self_authed:
        assert _check(manifest, policy) == [f"gateway /p/{ports.DEFAULT_GATEWAY_PORT}/mcp"]
