from rdm import tunnels

RELAY_LOG = """your url is: https://whole-clocks-judge.loca.lt
"""

QUICK_LOG = """2026-11-13T10:00:00Z INF |  https://some-name-here.trycloudflare.com  |"""


def test_relay_url_extracted() -> None:
    assert tunnels.from_relay_logs(RELAY_LOG) == "https://whole-clocks-judge.loca.lt"


def test_relay_ignores_cloudflare_urls() -> None:
    assert tunnels.from_relay_logs(QUICK_LOG) == ""


def test_relay_empty_log() -> None:
    assert tunnels.from_relay_logs("") == ""