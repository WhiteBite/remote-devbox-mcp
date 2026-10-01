from __future__ import annotations

from rdm import tunnels


def test_picks_quick_tunnel_not_api():
    logs = (
        "INF Requesting new quick Tunnel on trycloudflare.com...\n"
        "INF |  Your quick Tunnel has been created! Visit it at "
        "https://brave-lion-abc.trycloudflare.com\n"
        "DBG GET https://api.trycloudflare.com/client\n"
    )
    assert tunnels.from_logs(logs) == "https://brave-lion-abc.trycloudflare.com"


def test_ignores_reserved_hosts_without_quick_tunnel():
    assert tunnels.from_logs("GET https://api.trycloudflare.com/x\n") == ""
    assert tunnels.from_logs("see https://www.trycloudflare.com") == ""


def test_empty_inputs():
    assert tunnels.from_logs("") == ""
    assert tunnels.from_logs(None) == ""