import re

from rdm import tokens
from rdm.envfile import EnvFile

HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
TOKEN_KEYS = ("MCP_BEARER_TOKEN", "MCP_PUBLIC_TOKEN", "INGRESS_TOKEN")


def test_generate_token_is_64_hex():
    a = tokens.generate_token()
    b = tokens.generate_token()
    assert HEX64.match(a)
    assert a != b


def test_mask_short_token():
    assert tokens.mask("abc123") == "..."
    assert tokens.mask("abcdefgh") == "abcd...efgh"
    assert tokens.mask("abcdefg") == "..."


def test_rotate_tokens_preserves_env_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# header comment\n"
        "PROJECT_DIR=C:/dev/app\n"
        "MCP_BEARER_TOKEN=old_bearer_token_value_000000\n"
        "\n"
        "MCP_PUBLIC_TOKEN=old_public_token_value_00000\n"
        "# tail comment\n"
        "INGRESS_TOKEN=old_ingress_token_value_0000\n",
        encoding="utf-8",
    )
    env = EnvFile.load(path)
    new = tokens.rotate_tokens(env)
    env.write(path)
    text = path.read_text(encoding="utf-8")
    assert "# header comment" in text
    assert "# tail comment" in text
    assert "PROJECT_DIR=C:/dev/app" in text
    for key in TOKEN_KEYS:
        assert HEX64.match(new[key])
        assert f"{key}={new[key]}" in text
        assert "old_" not in text.split(f"{key}=")[1].splitlines()[0]


def test_chat_block_masks_when_not_full():
    env_map = {
        "MCP_BEARER_TOKEN": "a" * 32,
        "MCP_PUBLIC_TOKEN": "b" * 32,
        "INGRESS_TOKEN": "c" * 32,
    }
    block = tokens.chat_block(env_map, "https://x.trycloudflare.com", full=False)
    lines = block.splitlines()
    assert lines[0] == "Репозиторий: https://github.com/WhiteBite/remote-devbox-mcp"
    assert lines[1] == (
        "Загрузи скилл: "
        "https://github.com/WhiteBite/remote-devbox-mcp/blob/main/skills/remote-devbox/SKILL.md"
    )
    assert lines[2] == (
        "Работай по инструкции: "
        "https://github.com/WhiteBite/remote-devbox-mcp/blob/main/ARENA.md"
    )
    assert lines[3] == "INGRESS=https://x.trycloudflare.com"
    assert lines[4] == "BRIDGE_TOKEN=aaaa..." + "a" * 4
    assert lines[5] == "HOST_TOKEN=bbbb..." + "b" * 4
    assert lines[6] == "INGRESS_TOKEN=cccc..." + "c" * 4
    assert lines[-1] == "ТЗ: в чате (или /workspace/ARENA_TASK.md, если есть)"
    assert "a" * 32 not in block


def test_chat_block_full_values():
    bearer = tokens.generate_token()
    public = tokens.generate_token()
    ingress = tokens.generate_token()
    env_map = {
        "MCP_BEARER_TOKEN": bearer,
        "MCP_PUBLIC_TOKEN": public,
        "INGRESS_TOKEN": ingress,
    }
    block = tokens.chat_block(env_map, "https://y.example.com", full=True, preview_url="https://p.example.com")
    assert f"BRIDGE_TOKEN={bearer}" in block
    assert f"HOST_TOKEN={public}" in block
    assert f"INGRESS_TOKEN={ingress}" in block
    assert "PREVIEW=https://p.example.com" in block
    assert "..." not in block


def test_chat_block_marks_active_profile():
    block = tokens.chat_block(
        {"ACTIVE_PROFILE": "midasai"}, "https://x", full=True, profiles=["muffin", "midasai"]
    )
    assert "midasai (активный)" in block
    assert "muffin" in block


def test_chat_block_includes_ui_url():
    block = tokens.chat_block({"UI_PORT": "8080"}, "https://ing", full=True)
    assert "UI=https://ing/p/8080/" in block
