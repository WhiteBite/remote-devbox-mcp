from __future__ import annotations

import json

import pytest

from stitch_devbox import service

_TOKENS = {
    "MCP_BEARER_TOKEN": "b" * 64,
    "MCP_PUBLIC_TOKEN": "c" * 64,
    "INGRESS_TOKEN": "d" * 64,
}

_PORTS_UP = {"ingress": True, "bridge": True, "runner": True}


def _fixture_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    *,
    active_profile: str = "",
    ports: dict[str, bool] | None = None,
    ingress_url: str | None = None,
) -> None:
    rdm = service._rdm()
    lines = ["PUBLIC_URL=https://prompt.example.com"] + [f"{key}={value}" for key, value in _TOKENS.items()]
    if active_profile:
        lines.append(f"ACTIVE_PROFILE={active_profile}")
    env_path = tmp_path / ".env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(rdm.cli, "ENV_FILE", env_path)
    monkeypatch.setattr(rdm.cli, "_runner_port", lambda env_map: 8796)
    monkeypatch.setattr(rdm.cli, "_self_authed_ports", lambda env_map: ())
    monkeypatch.setattr(rdm.ui.api, "build_status", lambda port: {"ports": dict(ports or _PORTS_UP)})
    if ingress_url is not None:
        monkeypatch.setattr(rdm.netprobe, "ingress_url", lambda env_map, compose_file=None: ingress_url)


def _fixture_profile(monkeypatch: pytest.MonkeyPatch, tmp_path, *, mode: str = "standard", toolchain: str = "java21") -> None:
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    (projects / "demo.json").write_text(
        json.dumps({"project_dir": "d:/work/demo", "mode": mode, "toolchain": toolchain}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RDM_PROJECTS_DIR", str(projects))


def _rules_digest(prompt: str) -> list[str]:
    block = prompt[prompt.index("Правила devbox:") :].split("\n\n", 1)[0]
    return [line for line in block.splitlines() if line.startswith("- ")]


def test_agent_prompt_payload_shape(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    result = service.agent_prompt(task="Сделай X")

    assert set(result) == {"prompt", "masked", "ready"}
    assert set(result["ready"]) == {"profile", "bridge", "ingress", "runner"}
    assert isinstance(result["prompt"], str)


def test_agent_prompt_section_order(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt(task="Сделай X")["prompt"]

    marks = [
        "Ты получаешь доступ к моему devbox и выполняешь задачу из последнего раздела.",
        "Bootstrap:",
        "Читать по порядку:",
        "Репозиторий: https://github.com/WhiteBite/remote-devbox-mcp",
        "Правила devbox:",
        "Если в корне проекта есть AGENTS.md",
        "Задача:",
        "Готово = диагностика зелёная",
        "Отчёт в конце:",
    ]
    positions = [prompt.index(mark) for mark in marks]
    assert positions == sorted(positions)


def test_agent_prompt_bootstrap_commands_verbatim(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt()["prompt"]

    for line in (
        "git clone --depth 1 https://github.com/WhiteBite/remote-devbox-mcp devbox",
        "cd devbox/arena && chmod +x mcp     # после каждого снапшота песочницы бит слетает",
        "cat > ~/.remote-devbox-mcp.conf <<EOF",
        "MCP_URL=<INGRESS>/p/8787/mcp",
        "MCP_TOKEN=<BRIDGE_TOKEN>",
        "MCP_TIMEOUT=300",
        "./mcp check        # ожидаешь opencode-mcp-bridge и список из 10 native + 6 control тулов (16)",
    ):
        assert line in prompt
    assert "Сразу скопируй оба конфига" in prompt and "persistent Workspace" in prompt


def test_agent_prompt_read_chain_line(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt()["prompt"]

    assert (
        "Читать по порядку: ARENA.md, skills/remote-devbox/SKILL.md,"
        " arena/AGENT_INSTRUCTIONS.md, arena/SANDBOX_FACTS.md." in prompt
    )


def test_agent_prompt_rules_digest_has_five_bullets(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt()["prompt"]

    assert len(_rules_digest(prompt)) == 5


def test_agent_prompt_project_rules_precedence_line(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt()["prompt"]

    assert (
        "Если в корне проекта есть AGENTS.md или CLAUDE.md - следуй им;"
        " инфраструктурные правила devbox приоритетнее." in prompt
    )


def test_agent_prompt_task_and_acceptance(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt(task="Исправь баг", acceptance="Тесты зелёные")["prompt"]

    assert "Задача:\nИсправь баг" in prompt
    assert "Критерии приёмки:\nТесты зелёные" in prompt


def test_agent_prompt_empty_task_uses_closer(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt()["prompt"]

    assert "Задача:\nЗадачу сформулирую следующим сообщением." in prompt
    assert "Критерии приёмки:" not in prompt


def test_agent_prompt_acceptance_only_with_task(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt(task="Исправь баг", acceptance="Тесты зелёные")["prompt"]

    assert "Задачу сформулирую следующим сообщением." not in prompt


def test_agent_prompt_dod_and_report_lines(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt()["prompt"]

    assert (
        "Готово = диагностика зелёная и команды проверки из задачи прошли;"
        " зависший после 3 попыток шаг - стоп и отчёт о блокере." in prompt
    )
    assert "Отчёт в конце: что сделано, чем проверено, что осталось." in prompt


def test_agent_prompt_masks_by_default(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    result = service.agent_prompt()

    assert result["masked"] is True
    for value in _TOKENS.values():
        assert value not in result["prompt"]
    assert "bbbb...bbbb" in result["prompt"]


def test_agent_prompt_reveal_returns_unmasked_tokens(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    result = service.agent_prompt(reveal=True)

    assert result["masked"] is False
    for value in _TOKENS.values():
        assert value in result["prompt"]


def test_agent_prompt_includes_chat_block_runner_lines(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    result = service.agent_prompt(reveal=True)

    assert "RUNNER_URL=https://prompt.example.com/p/8796/mcp" in result["prompt"]
    assert f"HOST_TOKEN={_TOKENS['MCP_PUBLIC_TOKEN']}" in result["prompt"]


def test_agent_prompt_project_context_line(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path, active_profile="demo")
    _fixture_profile(monkeypatch, tmp_path)

    prompt = service.agent_prompt()["prompt"]

    assert (
        "Контекст проекта: папка d:/work/demo (в контейнере /workspace);"
        " режим прав: standard - мутации уходят на одобрение;"
        " тулчейн: java21;"
        " манифест окружения читай в /workspace/.devbox/AGENTS.md." in prompt
    )


def test_agent_prompt_project_context_readonly_mode(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path, active_profile="demo")
    _fixture_profile(monkeypatch, tmp_path, mode="readonly", toolchain="")

    prompt = service.agent_prompt()["prompt"]

    assert "режим прав: readonly - только чтение и диагностика" in prompt
    assert "тулчейн: -;" in prompt


def test_agent_prompt_without_profile_omits_context(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path)

    prompt = service.agent_prompt()["prompt"]

    assert "Контекст проекта:" not in prompt


def test_agent_prompt_ready_all_up(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path, active_profile="demo")
    _fixture_profile(monkeypatch, tmp_path)

    ready = service.agent_prompt()["ready"]

    assert ready == {"profile": True, "bridge": True, "ingress": True, "runner": True}


def test_agent_prompt_ready_flips_with_ports_status(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path, active_profile="demo", ports={"ingress": True, "bridge": False})
    _fixture_profile(monkeypatch, tmp_path)

    ready = service.agent_prompt()["ready"]

    assert ready == {"profile": True, "bridge": False, "ingress": True, "runner": False}


def test_agent_prompt_ready_ingress_requires_public_url(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path, ingress_url="")

    ready = service.agent_prompt()["ready"]

    assert ready["ingress"] is False


def test_agent_prompt_empty_ingress_marker_replaces_line(monkeypatch, tmp_path):
    _fixture_env(monkeypatch, tmp_path, ingress_url="")

    prompt = service.agent_prompt()["prompt"]

    assert "INGRESS НЕ ПОДНЯТ - включи ingress до передачи промпта" in prompt
    assert "INGRESS=" not in prompt


def test_agent_prompt_handler_registered():
    from stitch_devbox import __main__ as plugin_main

    assert "agent_prompt" in plugin_main._build_server()._handlers


def test_agent_prompt_manifest_is_readonly():
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parents[2] / "plugin.json").read_text(encoding="utf-8")
    )
    commands = {
        command["name"]: command["readonly"] for command in manifest["contributions"]["commands"]
    }
    assert commands["agent_prompt"] is True
