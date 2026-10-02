import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _client_module():
    spec = importlib.util.spec_from_file_location("mcp_client_docs", REPO / "arena" / "mcp_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_arena_md_exit_codes_match_client_constants():
    mod = _client_module()
    line = next(
        line for line in (REPO / "ARENA.md").read_text(encoding="utf-8").splitlines()
        if "Коды возврата клиента" in line
    )
    pairs = (
        (mod.EX_OK, "успех"),
        (mod.EX_CONFIG, "конфиг"),
        (mod.EX_TUNNEL, "туннель"),
        (mod.EX_PERMISSION, "разрешение"),
        (mod.EX_JOB_FAILED, "джоб-ошибка"),
    )
    for code, word in pairs:
        assert f"{code} {word}" in line
    assert "не завершившийся" in line


def test_agent_instructions_document_job_failed_code():
    text = (REPO / "arena" / "AGENT_INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert "кодом 5" in text
    assert "не завершился" in text
