import argparse
import importlib.util
import io
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "arena" / "mcp_client.py"


def load_client():
    spec = importlib.util.spec_from_file_location("mcp_client_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def run_outside_repo_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


class RecordingTransport:
    name = "fake"

    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)
        return {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {"content": [{"type": "text", "text": f"run-{len(self.sent)}"}]},
        }


def test_repeated_mutation_re_executes():
    mod = load_client()
    t = RecordingTransport()
    client = mod.MCPClient(t)
    args = {"command": "./gradlew test"}
    first = client.call("bash", args)
    second = client.call("bash", args)
    assert len(t.sent) == 2
    assert [p["params"] for p in t.sent] == [
        {"name": "bash", "arguments": args},
        {"name": "bash", "arguments": args},
    ]
    assert first["content"][0]["text"] == "run-1"
    assert second["content"][0]["text"] == "run-2"


def test_state_paths_absolute_not_cwd(monkeypatch):
    monkeypatch.delenv("RDM_ARENA_STATE_DIR", raising=False)
    mod = load_client()
    assert os.path.isabs(mod.STATE_DIR)
    assert os.path.isabs(mod.JOBS_FILE)
    cwd = os.path.realpath(os.getcwd())
    assert os.path.realpath(mod.STATE_DIR) != cwd
    assert not os.path.realpath(mod.JOBS_FILE).startswith(cwd + os.sep)


def test_sse_parse_selects_last_result():
    mod = load_client()
    body = "\n".join([
        'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}',
        "",
        'data: {"jsonrpc":"2.0","id":7,"result":{"partial":true}}',
        "",
        'data: {"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"done"}]}}',
        "",
        "data: [DONE]",
    ])
    msg = mod.HttpTransport._parse_sse(body)
    assert msg is not None
    assert msg["id"] == 7
    assert msg["result"]["content"][0]["text"] == "done"


def test_job_state_roundtrip(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("RDM_ARENA_STATE_DIR", str(state))
    mod = load_client()
    assert mod.JOBS_FILE == str(state / ".mcp_jobs.json")
    mod.save_job_state({"job_id": "j-1", "status": "running"}, "bash")
    mod.save_job_state({"job_id": "j-1", "status": "completed"}, "bash")
    loaded = mod.load_job_states()
    assert loaded["j-1"]["status"] == "completed"
    assert loaded["j-1"]["tool"] == "bash"
    mod.save_job_states({"j-2": {"status": "awaiting_permission", "tool": "edit"}})
    assert mod.load_job_states()["j-2"]["tool"] == "edit"
    assert (state / ".mcp_jobs.json").exists()
    assert not (Path.cwd() / "shots").exists()


class FakeResponse:
    def __init__(self, body):
        self.headers = {"Content-Type": "application/json"}
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_retryable_status_retries(monkeypatch):
    mod = load_client()
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "http://devbox/mcp", 502, "Bad Gateway", None,
                io.BytesIO(b"edge unreachable"))
        return FakeResponse(
            b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ok"}]}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    t = mod.HttpTransport("http://devbox/mcp", retries=2)
    res = t.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert res["result"]["content"][0]["text"] == "ok"
    assert len(calls) == 2
    t.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}})
    assert len(calls) == 3


def test_midbody_failure_is_clean_runtime_error(monkeypatch):
    mod = load_client()

    class BrokenResponse(FakeResponse):
        def read(self):
            raise mod.http.client.IncompleteRead(b"")

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: BrokenResponse(b""))
    t = mod.HttpTransport("http://devbox/mcp", retries=2)
    with pytest.raises(RuntimeError) as err:
        t.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}})
    assert "мог выполниться" in str(err.value).lower()


def _retries_args(retries):
    return argparse.Namespace(
        command_stdio=None, url="http://x/mcp", token="t", header=None,
        no_bearer=False, timeout=5.0, retries=retries,
    )


def test_mcp_retries_env_default(monkeypatch):
    mod = load_client()
    monkeypatch.setenv("MCP_RETRIES", "9")
    assert mod.build_client(_retries_args(None)).t.retries == 9


def test_retries_flag_overrides_env(monkeypatch):
    mod = load_client()
    monkeypatch.setenv("MCP_RETRIES", "9")
    assert mod.build_client(_retries_args(3)).t.retries == 3


def test_retries_default_is_six(monkeypatch):
    mod = load_client()
    monkeypatch.delenv("MCP_RETRIES", raising=False)
    assert mod.build_client(_retries_args(None)).t.retries == 6
