import subprocess

from rdm import netprobe

RELAY_LOG = "your url is: https://devbox-abcd1234.loca.lt\n"


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], 0, stdout, "")


def test_relay_mode_prefers_tunnel_url(monkeypatch):
    monkeypatch.setattr(netprobe.docker, "compose", lambda *a, **k: _completed(RELAY_LOG))

    url = netprobe.ingress_url({"INGRESS_MODE": "relay", "PUBLIC_URL": "https://devbox.example.test"})

    assert url == "https://devbox-abcd1234.loca.lt"


def test_relay_mode_never_falls_back_to_public_url(monkeypatch):
    monkeypatch.setattr(netprobe.docker, "compose", lambda *a, **k: _completed(""))
    monkeypatch.setattr(netprobe.docker, "run", lambda *a, **k: _completed(""))

    url = netprobe.ingress_url({"INGRESS_MODE": "relay", "PUBLIC_URL": "https://devbox.example.test"})

    assert url == ""


def test_relay_mode_falls_back_to_container_logs(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("compose unavailable")

    monkeypatch.setattr(netprobe.docker, "compose", boom)
    monkeypatch.setattr(netprobe.docker, "run", lambda *a, **k: _completed(RELAY_LOG))

    url = netprobe.ingress_url({"INGRESS_MODE": "relay"})

    assert url == "https://devbox-abcd1234.loca.lt"


def test_manual_mode_uses_public_url():
    url = netprobe.ingress_url({"INGRESS_MODE": "manual", "PUBLIC_URL": "https://my-vps.example.test"})

    assert url == "https://my-vps.example.test"


def test_manual_mode_returns_empty_without_public_url():
    url = netprobe.ingress_url({"INGRESS_MODE": "manual"})

    assert url == ""


def test_relay_mode_falls_back_to_env_relay_url(monkeypatch):
    monkeypatch.setattr(netprobe.docker, "compose", lambda *a, **k: _completed(""))
    monkeypatch.setattr(netprobe.docker, "run", lambda *a, **k: _completed(""))

    url = netprobe.ingress_url({"INGRESS_MODE": "relay", "RELAY_URL": "https://devbox-abcd1234.loca.lt"})

    assert url == "https://devbox-abcd1234.loca.lt"
