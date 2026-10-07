import re
from pathlib import Path

from rdm.ui import auth

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_AUTH_SOURCE = Path(__file__).resolve().parents[2] / "home" / "rdm" / "ui" / "auth.py"
_AGENT_TOKEN_LITERALS = ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN", "_TOKEN_KEYS")


def test_ui_state_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_UI_STATE_DIR", str(tmp_path))
    assert auth.ui_state_dir() == tmp_path


def test_secret_is_64_hex_and_persists(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_UI_STATE_DIR", str(tmp_path))
    first = auth.load_or_create_secret()
    assert HEX64.match(first)
    assert auth.load_or_create_secret() == first
    assert (tmp_path / "secret").read_text(encoding="utf-8").strip() == first


def test_secret_regenerates_over_corrupt_file(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_UI_STATE_DIR", str(tmp_path))
    (tmp_path / "secret").write_text("not-a-secret", encoding="utf-8")
    assert HEX64.match(auth.load_or_create_secret())


def test_bootstrap_consumes_once(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_UI_STATE_DIR", str(tmp_path))
    token = auth.write_bootstrap()
    assert auth.consume_bootstrap(token)
    assert not auth.consume_bootstrap(token)
    assert not (tmp_path / "bootstrap.json").exists()


def test_bootstrap_rejects_foreign_token(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_UI_STATE_DIR", str(tmp_path))
    token = auth.write_bootstrap()
    assert not auth.consume_bootstrap("foreign")
    assert not auth.consume_bootstrap(token)


def test_bootstrap_expires(monkeypatch, tmp_path):
    monkeypatch.setenv("RDM_UI_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "_BOOTSTRAP_TTL_SECONDS", -1)
    assert not auth.consume_bootstrap(auth.write_bootstrap())


def test_host_origin_ok_accepts_loopback():
    assert auth.host_origin_ok({"Host": "127.0.0.1:8090"}, 8090)
    assert auth.host_origin_ok({"Host": "localhost:8090"}, 8090)
    assert auth.host_origin_ok({"Host": "127.0.0.1:8090", "Origin": "http://127.0.0.1:8090"}, 8090)
    assert auth.host_origin_ok({"Host": "localhost:8090", "Origin": "http://localhost:8090"}, 8090)


def test_host_origin_ok_rejects_foreign_host():
    assert not auth.host_origin_ok({"Host": "evil.example.com:8090"}, 8090)
    assert not auth.host_origin_ok({"Host": "127.0.0.1:9999"}, 8090)
    assert not auth.host_origin_ok({}, 8090)


def test_host_origin_ok_rejects_foreign_origin():
    assert not auth.host_origin_ok({"Host": "127.0.0.1:8090", "Origin": "https://evil.example.com"}, 8090)
    assert not auth.host_origin_ok({"Host": "127.0.0.1:8090", "Origin": "http://127.0.0.1:9999"}, 8090)


def test_cookie_header_exact():
    assert auth.cookie_header("s3") == "Set-Cookie: rdm_ui=s3; HttpOnly; SameSite=Strict; Path=/"


def test_session_store_rejects_unknown_token():
    store = auth.SessionStore()
    assert not store.verify_session("unknown")
    minted = store.mint_session()
    assert store.verify_session(minted)
    assert not store.verify_session(minted + "x")


def test_minted_sessions_are_unique():
    store = auth.SessionStore()
    assert store.mint_session() != store.mint_session()


def test_auth_source_references_no_agent_tokens():
    source = _AUTH_SOURCE.read_text(encoding="utf-8")
    for literal in _AGENT_TOKEN_LITERALS:
        assert literal not in source
