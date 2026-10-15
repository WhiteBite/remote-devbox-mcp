from __future__ import annotations

from .tokens import _REPO_URL, chat_block

_MODE_HUMAN = {
    "readonly": "readonly - только чтение и диагностика",
    "standard": "standard - мутации уходят на одобрение",
    "full": "full - полный доступ",
}

_RULES = (
    "- Сборки и тесты только на devbox через `bash`, не в своей песочнице.",
    "- После каждой правки - диагностика: `lsp` или компиляция/тесты через `bash`.",
    "- Переименования через `lsp`, а не текстовой заменой.",
    "- Секреты не читать и не выводить: `.env`, ключи, `application-prod.*`.",
    "- Мутации только внутри `/workspace`, только файловыми тулами (`edit`/`write`/`apply_patch`),"
    " без `cat > file` через `bash`.",
)

_BOOTSTRAP = (
    "```bash",
    f"git clone --depth 1 {_REPO_URL} devbox",
    "cd devbox/arena && chmod +x mcp     # после каждого снапшота песочницы бит слетает",
    "cat > ~/.remote-devbox-mcp.conf <<EOF",
    "MCP_URL=<INGRESS>/p/8787/mcp",
    "MCP_TOKEN=<BRIDGE_TOKEN>",
    "MCP_TIMEOUT=300",
    "EOF",
    "./mcp check        # ожидаешь opencode-mcp-bridge и список из 10 native + 6 control тулов (16)",
    "```",
    "Сразу скопируй оба конфига (~/.remote-devbox-mcp.conf и ~/.mcp-runner.conf)"
    " в persistent Workspace - песочница сбрасывается, Workspace нет.",
)


def compose_prompt(
    env_map: dict[str, str],
    ingress_url: str,
    preview_url: str,
    runner_port: int | None,
    self_authed_ports: tuple[int, ...],
    profile: dict[str, str] | None,
    task: str,
    acceptance: str,
    full: bool,
) -> str:
    sections = [
        "Ты получаешь доступ к моему devbox и выполняешь задачу из последнего раздела.",
        "Bootstrap:\n" + "\n".join(_BOOTSTRAP),
        "Читать по порядку: ARENA.md, skills/remote-devbox/SKILL.md,"
        " arena/AGENT_INSTRUCTIONS.md, arena/SANDBOX_FACTS.md.",
        chat_block(
            env_map,
            ingress_url,
            full,
            preview_url=preview_url,
            runner_port=runner_port,
            self_authed_ports=self_authed_ports,
        ),
    ]
    if profile is not None:
        mode = profile.get("mode", "")
        toolchain = profile.get("toolchain", "") or "-"
        sections.append(
            f"Контекст проекта: папка {profile.get('project_dir', '')} (в контейнере /workspace);"
            f" режим прав: {_MODE_HUMAN.get(mode, mode)};"
            f" тулчейн: {toolchain};"
            " манифест окружения читай в /workspace/.devbox/AGENTS.md."
        )
    sections.append("Правила devbox:\n" + "\n".join(_RULES))
    sections.append(
        "Если в корне проекта есть AGENTS.md или CLAUDE.md - следуй им;"
        " инфраструктурные правила devbox приоритетнее."
    )
    task_text = task.strip()
    task_lines = ["Задача:", task_text if task_text else "Задачу сформулирую следующим сообщением."]
    acceptance_text = acceptance.strip()
    if acceptance_text:
        task_lines += ["Критерии приёмки:", acceptance_text]
    sections.append("\n".join(task_lines))
    sections.append(
        "Готово = диагностика зелёная и команды проверки из задачи прошли;"
        " зависший после 3 попыток шаг - стоп и отчёт о блокере.\n"
        "Отчёт в конце: что сделано, чем проверено, что осталось."
    )
    return "\n\n".join(sections)
