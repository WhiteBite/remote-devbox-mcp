import importlib.util
import json
import re
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


def _skill_exit_code_offenders(text, mod):
    marker = "## Exit codes"
    start = text.find(marker)
    if start == -1:
        return ["skills/remote-devbox/SKILL.md: '## Exit codes' section is missing"]
    rest = text[start + len(marker):]
    end = rest.find("\n## ")
    section = (rest if end == -1 else rest[:end]).replace("`", "")
    offenders = []
    for code, word in (
        (mod.EX_OK, "ok"),
        (mod.EX_CONFIG, "config"),
        (mod.EX_TUNNEL, "tunnel"),
        (mod.EX_PERMISSION, "permission"),
        (mod.EX_JOB_FAILED, "job error"),
    ):
        if f"{code} {word}" not in section:
            offenders.append(f"skills/remote-devbox/SKILL.md exit codes: missing '{code} {word}'")
    if "never finished" not in section:
        offenders.append("skills/remote-devbox/SKILL.md exit codes: missing 'never finished'")
    return offenders


def _tools_md_names(text):
    names = []
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 3:
            continue
        match = re.search(r"`([^`]+)`", cells[1])
        if match:
            names.append(match.group(1).strip())
    return list(dict.fromkeys(names))


def _tool_presence_offenders(tools_text, docs):
    names = _tools_md_names(tools_text)
    if not names:
        return ["arena/TOOLS.md: no table rows with a backticked tool name in the first column"]
    offenders = []
    for name in names:
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        for doc_name, doc_text in docs.items():
            if not pattern.search(doc_text):
                offenders.append(f"{doc_name}: tool '{name}' from arena/TOOLS.md is not documented")
    return offenders


def _conf_url_offenders(text):
    offenders = []
    if "/p/8787/mcp" not in text:
        offenders.append("arena/mcp.conf.example: '/p/8787/mcp' bridge path is not documented")
    url_lines = [line for line in text.splitlines() if line.startswith("MCP_URL=")]
    if not url_lines:
        offenders.append("arena/mcp.conf.example: MCP_URL= line is missing")
    for line in url_lines:
        if "/p/8787/mcp" not in line:
            offenders.append(f"arena/mcp.conf.example: MCP_URL at tunnel root, not /p/8787/mcp: {line}")
    return offenders


def _exec_bit_offenders(text):
    offenders = []
    for line in text.splitlines():
        if "./mcp check" in line and "чинит" in line:
            offenders.append(f"false './mcp check' self-heal claim: {line.strip()}")
    if "bash mcp" not in text and "chmod +x mcp" not in text:
        offenders.append("arena/AGENT_INSTRUCTIONS.md: no 'bash mcp' / 'chmod +x mcp' exec-bit workaround")
    return offenders


def _webfetch_offenders(files):
    offenders = []
    for name, text in files.items():
        if text is None:
            offenders.append(f"{name}: file is missing")
            continue
        for line in text.splitlines():
            if "webfetch" not in line:
                continue
            if "permission" not in line.lower() and "разрешени" not in line.lower():
                offenders.append(f"{name}: webfetch not marked permission-gated: {line.strip()}")
    return offenders


def _matrix_offenders(rel, text, heading_term):
    idx = text.find(heading_term)
    if idx == -1:
        return [f"{rel}: capability matrix heading '{heading_term}' is missing"]
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n## ", idx)
    section = text[start:] if end == -1 else text[start:end]
    offenders = []
    for term in ("adapter", "--trust", "standard", "awaiting_permission"):
        if term not in section:
            offenders.append(f"{rel}: capability matrix is missing '{term}'")
    return offenders


def test_skill_md_exit_codes_match_client_constants():
    mod = _client_module()
    text = (REPO / "skills" / "remote-devbox" / "SKILL.md").read_text(encoding="utf-8")
    assert _skill_exit_code_offenders(text, mod) == []


def test_tools_md_canonical_tools_documented_in_agent_docs():
    tools_path = REPO / "arena" / "TOOLS.md"
    assert tools_path.exists(), "arena/TOOLS.md is missing: canonical tool list unavailable"
    docs = {
        rel: (REPO / rel).read_text(encoding="utf-8")
        for rel in ("ARENA.md", "skills/remote-devbox/SKILL.md", "arena/AGENT_INSTRUCTIONS.md")
    }
    assert _tool_presence_offenders(tools_path.read_text(encoding="utf-8"), docs) == []


def test_conf_example_url_uses_bridge_path():
    text = (REPO / "arena" / "mcp.conf.example").read_text(encoding="utf-8")
    assert _conf_url_offenders(text) == []


def test_agent_instructions_no_false_exec_bit_self_heal():
    text = (REPO / "arena" / "AGENT_INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert _exec_bit_offenders(text) == []


def test_webfetch_marked_permission_gated():
    files = {}
    for rel in ("ARENA.md", "arena/AGENT_INSTRUCTIONS.md", "arena/TOOLS.md"):
        path = REPO / rel
        files[rel] = path.read_text(encoding="utf-8") if path.exists() else None
    assert _webfetch_offenders(files) == []


def test_capability_matrix_documented_in_skill_and_arena():
    skill = (REPO / "skills" / "remote-devbox" / "SKILL.md").read_text(encoding="utf-8")
    arena = (REPO / "ARENA.md").read_text(encoding="utf-8")
    assert _matrix_offenders("skills/remote-devbox/SKILL.md", skill, "Client capability matrix") == []
    assert _matrix_offenders("ARENA.md", arena, "Матрица возможностей") == []


def test_readme_has_no_unqualified_any_mcp_client_claim():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "любой MCP-клиент" not in text


def test_mcp_config_example_parses_with_three_servers():
    data = json.loads((REPO / "arena" / "mcp-config.example.json").read_text(encoding="utf-8"))
    assert data["$schema"] == "https://opencode.ai/config.json"
    servers = data["mcp"]
    for name in ("devbox-runner", "devbox-supervisor", "devbox-bridge"):
        assert name in servers
    for name in ("devbox-runner", "devbox-supervisor"):
        assert servers[name]["type"] == "remote"
        assert servers[name]["headers"]["Authorization"] == "Bearer {env:MCP_PUBLIC_TOKEN}"
    bridge = servers["devbox-bridge"]
    assert bridge["type"] == "local"
    assert "--trust" in bridge["command"]
    assert "MCP_URL" in bridge["environment"]
    assert "MCP_TOKEN" in bridge["environment"]


def test_sabotage_mutated_docs_are_flagged():
    mod = _client_module()

    swapped = (
        "## Exit codes\n\n`0` ok · `2` tunnel · `3` config · `4` permission · `5` job error, never finished\n"
    )
    offenders = _skill_exit_code_offenders(swapped, mod)
    assert any("2 config" in o for o in offenders)
    assert any("3 tunnel" in o for o in offenders)

    offenders = _tool_presence_offenders("| `read` |\n| `phantom_tool` |\n", {"ARENA.md": "uses read"})
    assert any("phantom_tool" in o for o in offenders)

    offenders = _conf_url_offenders("MCP_URL=https://xxxxx.trycloudflare.com/mcp\n")
    assert any("trycloudflare.com/mcp" in o for o in offenders)

    offenders = _exec_bit_offenders("chmod +x mcp вручную.\n`./mcp check` сам чинит exec-бит.\n")
    assert any("чинит" in o for o in offenders)

    offenders = _webfetch_offenders({"arena/TOOLS.md": "| `webfetch` | fetch a URL |"})
    assert any("webfetch" in o for o in offenders)

    offenders = _matrix_offenders(
        "ARENA.md",
        "## Матрица возможностей\n\nклиент и адаптер без флагов\n",
        "Матрица возможностей",
    )
    assert any("--trust" in o for o in offenders)
    assert any("awaiting_permission" in o for o in offenders)
