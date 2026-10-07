import argparse
import importlib.util
import io
import json
import os
import sys
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


def test_save_job_state_writes_atomically(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("RDM_ARENA_STATE_DIR", str(state))
    mod = load_client()
    mod.save_job_state({"job_id": "j-1", "status": "running"}, "bash")
    assert sorted(p.name for p in state.iterdir()) == [".mcp_jobs.json"]
    written = json.loads((state / ".mcp_jobs.json").read_text(encoding="utf-8"))
    assert written["j-1"]["status"] == "running"


def test_save_job_state_keeps_previous_file_on_write_failure(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("RDM_ARENA_STATE_DIR", str(state))
    mod = load_client()
    mod.save_job_state({"job_id": "j-1", "status": "running"}, "bash")

    def boom(obj, f, **kw):
        raise OSError("обрыв посреди записи")

    monkeypatch.setattr(mod.json, "dump", boom)
    with pytest.raises(OSError):
        mod.save_job_state({"job_id": "j-2", "status": "running"}, "bash")
    assert list(mod.load_job_states()) == ["j-1"]
    assert sorted(p.name for p in state.iterdir()) == [".mcp_jobs.json"]


class ScriptedClient:
    def __init__(self, job_payload):
        self.job_payload = job_payload

    def initialize(self):
        return {"protocolVersion": "2025-06-18"}

    def call(self, tool, arguments):
        return {"content": [{"type": "text", "text": json.dumps(self.job_payload)}]}

    def close(self):
        pass


def _patch_run_main(monkeypatch, tmp_path, settle_result):
    state = tmp_path / "state"
    monkeypatch.setenv("RDM_ARENA_STATE_DIR", str(state))
    mod = load_client()
    monkeypatch.setattr(mod, "build_client", lambda args: ScriptedClient(
        {"job_id": settle_result["job_id"], "status": "running"}))
    monkeypatch.setattr(mod, "settle_job", lambda *a, **k: settle_result)
    monkeypatch.setattr(sys, "argv", ["mcp_client", "--url", "http://x/mcp",
                                      "run", "bash", '{"command":"x"}'])
    return mod


def test_run_path_exits_job_failed_when_job_never_settles(monkeypatch, tmp_path):
    mod = _patch_run_main(monkeypatch, tmp_path,
                          {"job_id": "j-hang", "status": "running"})
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == mod.EX_JOB_FAILED


def test_run_path_drops_terminal_job_from_state(monkeypatch, tmp_path):
    mod = _patch_run_main(monkeypatch, tmp_path, {
        "job_id": "j-done", "status": "completed",
        "result": {"metadata": {"exit": 0}}})
    mod.main()
    assert mod.load_job_states() == {}


def test_run_path_keeps_unsettled_job_in_state(monkeypatch, tmp_path):
    mod = _patch_run_main(monkeypatch, tmp_path,
                          {"job_id": "j-hang", "status": "running"})
    with pytest.raises(SystemExit):
        mod.main()
    assert mod.load_job_states()["j-hang"]["status"] == "running"


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        return {"content": [{"type": "text", "text": json.dumps(self.responses.pop(0))}]}


def _awaiting_job(job_id="j-1", perm_id="p-1"):
    return {"job_id": job_id, "status": "awaiting_permission",
            "permission": {"id": perm_id, "permission": "edit",
                           "patterns": ["src/Main.java"]}}


def test_settle_job_without_handler_stops_at_prompt():
    mod = load_client()
    client = SequenceClient([])
    job = mod.settle_job(client, _awaiting_job(), auto=False,
                         max_polls=3, delay=0, progress=False)
    assert job["status"] == "awaiting_permission"
    assert client.calls == []


def test_settle_job_without_handler_auto_replies_once():
    mod = load_client()
    client = SequenceClient([
        {"job_id": "j-1", "status": "completed", "result": {"output": "ok"}},
    ])
    job = mod.settle_job(client, _awaiting_job(), auto=True,
                         max_polls=3, delay=0, progress=False)
    assert job["status"] == "completed"
    assert client.calls == [("opencode_permission_reply",
                             {"job_id": "j-1", "permission_id": "p-1",
                              "reply": "once"})]


def test_settle_job_on_permission_feeds_handler_reply():
    mod = load_client()
    client = SequenceClient([
        {"job_id": "j-1", "status": "completed", "result": {"output": "ok"}},
    ])
    seen = []

    def on_permission(job):
        seen.append(job)
        return "once"

    job = mod.settle_job(client, _awaiting_job(), auto=False,
                         max_polls=3, delay=0, progress=False,
                         on_permission=on_permission)
    assert job["status"] == "completed"
    assert seen == [_awaiting_job()]
    assert client.calls == [("opencode_permission_reply",
                             {"job_id": "j-1", "permission_id": "p-1",
                              "reply": "once"})]


def test_settle_job_on_permission_reject_denies():
    mod = load_client()
    client = SequenceClient([
        {"job_id": "j-1", "status": "cancelled"},
    ])
    job = mod.settle_job(client, _awaiting_job(), auto=False,
                         max_polls=3, delay=0, progress=False,
                         on_permission=lambda job: "reject")
    assert job["status"] == "cancelled"
    assert client.calls == [("opencode_permission_reply",
                             {"job_id": "j-1", "permission_id": "p-1",
                              "reply": "reject"})]


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
    res = t.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert res["result"]["content"][0]["text"] == "ok"
    assert len(calls) == 2
    t.send({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
    assert len(calls) == 3


def test_502_on_tools_call_raises_without_retry(monkeypatch):
    mod = load_client()
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        raise urllib.error.HTTPError(
            "http://devbox/mcp", 502, "Bad Gateway", None,
            io.BytesIO(b"origin died mid-execution"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    t = mod.HttpTransport("http://devbox/mcp", retries=2)
    with pytest.raises(RuntimeError) as err:
        t.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "bash", "arguments": {}}})
    assert "502" in str(err.value)
    assert len(calls) == 1


def test_503_on_tools_call_retries(monkeypatch):
    mod = load_client()
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "http://devbox/mcp", 503, "Service Unavailable", None,
                io.BytesIO(b"connection slot exhausted"))
        return FakeResponse(
            b'{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"ok"}]}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    t = mod.HttpTransport("http://devbox/mcp", retries=2)
    res = t.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "bash", "arguments": {}}})
    assert res["result"]["content"][0]["text"] == "ok"
    assert len(calls) == 2


def test_401_resets_session_via_callback_then_retries(monkeypatch):
    mod = load_client()
    calls = []
    resets = []
    session_on_retry = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                "http://devbox/mcp", 401, "Unauthorized", None,
                io.BytesIO(b"session expired"))
        session_on_retry.append(req.headers.get("Mcp-session-id"))
        return FakeResponse(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda s: None)
    t = mod.HttpTransport("http://devbox/mcp", retries=2)
    t.session_id = "stale"

    def on_reset():
        resets.append(1)
        t.session_id = "fresh"

    t.on_session_reset = on_reset
    res = t.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert res["result"]["tools"] == []
    assert len(resets) == 1
    assert len(calls) == 2
    assert session_on_retry == ["fresh"]


def test_client_wires_on_session_reset_to_initialize():
    mod = load_client()
    t = mod.HttpTransport("http://devbox/mcp")
    client = mod.MCPClient(t)
    assert t.on_session_reset == client.initialize


def test_client_skips_wiring_for_transport_without_callback():
    mod = load_client()
    t = RecordingTransport()
    mod.MCPClient(t)
    assert not hasattr(t, "on_session_reset")


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


class DoctorClient:
    def __init__(self, tools=None, init_error=None, call_error=None):
        self.server_info = {"name": "opencode-toolbox"}
        self._tools = tools if tools is not None else [{"name": "bash"}]
        self._init_error = init_error
        self._call_error = call_error
        self.calls = []

    def initialize(self):
        if self._init_error:
            raise RuntimeError(self._init_error)
        return {}

    def list_tools(self):
        return self._tools

    def call(self, tool, arguments):
        self.calls.append(tool)
        if self._call_error:
            raise RuntimeError(self._call_error)
        return {"content": []}

    def close(self):
        pass


def _patch_doctor_main(monkeypatch, client):
    mod = load_client()
    for var in ("MCP_RUNNER_URL", "MCP_RUNNER_TOKEN", "MCP_CONF"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mod, "build_client", lambda args: client)
    monkeypatch.setattr(sys, "argv", ["mcp_client", "--url", "http://x/mcp", "doctor"])
    return mod


def test_doctor_reports_ok_and_exits_zero(monkeypatch, capsys):
    mod = _patch_doctor_main(monkeypatch, DoctorClient())
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == mod.EX_OK
    out = capsys.readouterr().out
    assert "OK" in out
    assert "FAIL" not in out


def test_doctor_reports_fail_and_exits_config_on_401(monkeypatch, capsys):
    mod = _patch_doctor_main(
        monkeypatch, DoctorClient(init_error="HTTP 401 Unauthorized: bad token"))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == mod.EX_CONFIG
    out = capsys.readouterr().out
    assert "FAIL" in out


def test_doctor_exits_tunnel_on_connection_error(monkeypatch, capsys):
    mod = _patch_doctor_main(
        monkeypatch, DoctorClient(init_error="нет соединения: getaddrinfo failed"))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == mod.EX_TUNNEL
    assert "FAIL" in capsys.readouterr().out


def test_doctor_calls_runner_list_when_conf_is_runner(monkeypatch, capsys):
    client = DoctorClient()
    mod = _patch_doctor_main(monkeypatch, client)
    monkeypatch.setenv("MCP_CONF", str(Path.home() / ".mcp-runner.conf"))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == mod.EX_OK
    assert client.calls == ["runner_list"]
    out = capsys.readouterr().out
    assert out.count("OK") == 2


def test_doctor_probes_second_endpoint_from_runner_env(monkeypatch, capsys):
    primary = DoctorClient()
    runner = DoctorClient()
    mod = _patch_doctor_main(monkeypatch, primary)
    monkeypatch.setenv("MCP_RUNNER_URL", "http://runner/mcp")
    monkeypatch.setenv("MCP_RUNNER_TOKEN", "rt")
    seen = []
    monkeypatch.setattr(mod, "MCPClient",
                        lambda t: seen.append(t) or runner)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == mod.EX_OK
    assert seen[0].url == "http://runner/mcp"
    assert seen[0].token == "rt"
    assert primary.calls == []
    assert runner.calls == ["runner_list"]
    assert "FAIL" not in capsys.readouterr().out


def test_doctor_runner_failure_exits_config_when_primary_ok(monkeypatch, capsys):
    client = DoctorClient(call_error="HTTP 401 Unauthorized: foreign token")
    mod = _patch_doctor_main(monkeypatch, client)
    monkeypatch.setenv("MCP_CONF", str(Path.home() / ".mcp-runner.conf"))
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == mod.EX_CONFIG
    out = capsys.readouterr().out
    assert "OK" in out and "FAIL" in out


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
