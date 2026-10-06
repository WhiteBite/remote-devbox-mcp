# AGENTS.md

> External users driving the devbox with a coding agent: read `ARENA.md` instead.
> This file is for developing this repository.

> Operational instructions for coding agents (Codex, Cursor, OpenCode, Claude Code).
> Content below is hand-verified against the repository; commands are real.

Project: WhiteBite/remote-devbox-mcp — turns your own Windows+Docker machine into a remote devbox that an external coding agent drives through an outbound Cloudflare tunnel and a containerized opencode-mcp-bridge.

## Commands

```bash
# install dev dependencies (host runtime + pytest, ruff, pyinstaller)
python -m pip install -r requirements-dev.txt

# run the test suite (pytest, tests/ with unit/integration/e2e dirs)
python -m pytest

# lint (ruff config lives in pyproject.toml; there is no compiled build step)
python -m ruff check home arena tests

# local frozen build (PyInstaller exe + compose/docker/projects zip into dist/)
python scripts/build_exe.py
```

Host orchestration (Windows): `python home/devbox.py start <profile> [--preview]`
(or `start.cmd` from the repo root) brings the stack up and prints the agent
hand-off block. Optional tray: `python -m pip install -r requirements-tray.txt`,
then `tray.cmd`.

## Repository map

| Path | Purpose |
| --- | --- |
| `home/` | host side: `devbox.py` + `rdm/` core package, `tray.py`, compose stack, `host/runner-mcp.py` |
| `home/README.md` | detailed install/run runbook (Windows), ingress and named-tunnel setup |
| `home/rdm/` | core: cli, profiles, render, envfile, ports, procman, hostos, docker, netprobe, freeze, tokens, tunnels, doctor, watchdog, `proxy/` (ingress), `runner/` (host-MCP) |
| `arena/` | agent-side stdlib MCP client (`mcp_client.py`, `mcp`), `AGENT_INSTRUCTIONS.md`, `TOOLS.md` (canonical tool list), `SANDBOX_FACTS.md`, `README.md` |
| `skills/remote-devbox/` | agent skill describing how to drive the devbox |
| `projects/` | per-project JSON profiles (`muffin.json`, `midasai.json`, `_template.json`) |
| `tests/` | pytest suite (unit / integration / e2e) |
| `scripts/` | `build_exe.py` + frozen entrypoints (`entry_devbox.py`, `entry_tray.py`) |
| `docs/`, `llms.txt`, `llms-full.txt` | discoverability artifacts, generated from `.discoverability/project.yml` |
| `ARENA.md` | full onboarding for the external agent (bootstrap, rules, hand-off template) |
| `start.cmd` / `tray.cmd` | repo-root launchers that delegate into `home/` |
| `.discoverability/project.yml` | source of truth for repo metadata |

## Do / Don't

- **Do** run `python -m pytest` before committing.
- **Do** keep `.discoverability/project.yml`, `README.md`, `llms.txt` and `llms-full.txt` in sync (the llms files are generated from the yml; see their headers).
- **Do** keep the exit-code line in `ARENA.md` in sync with the constants in `arena/mcp_client.py` — `tests/unit/test_docs_sync.py` gates it.
- **Don't** edit `.py`, `.ps1`, compose or Dockerfile files as part of doc-only work.
- **Don't** bump versions, create tags, publish, force-push or delete files without explicit human confirmation.
- **Don't** rewrite unrelated files while fixing a specific finding.

## Порт-политика и онбординг проекта

- Доступ агента к сервисам проекта задаётся политикой, а не перечислением портов:
  `allowed_ports` (явные) плюс `port_ranges` (диапазоны-потолки) минус `port_deny`.
  Ingress пускает порт из диапазона автоматически (token-gated), вне диапазона — 403.
  Так сервис/микросервис, поднятый агентом на любом порту диапазона, виден без правок профиля.
- Граница ответственности: долгоживущие серверы принадлежат supervisor-MCP проекта
  (`server_ensure`/`server_start`, напр. muffin-supervisor); host-runner (`runner_commands`) —
  для разовых host-команд (docker infra, staging ssh). Один сервер не поднимать обоими путями.
- Web под префиксом `/p/<port>/`: ingress переписывает `<base href>` для length-framed
  HTML-ответов, поэтому Flutter web (уважающий `<base>`) грузит ассеты без `--base-href`;
  chunked/gzip HTML и приложения с корне-абсолютными путями (Vite/React) — нет.
- Дрейф «профиль ↔ реальность» ловит `devbox.py doctor`: сверяет порты профиля с
  `tools/*/registry.json` проекта и печатает точный `devbox.py allow <port>`; реверс-скан
  показывает живые loopback-порты.
- Новый проект: скопировать `projects/_template.json`, задать `project_dir`, `toolchain`,
  `host_services`/`runner_commands` и диапазоны портов; `devbox.py use <имя>` валидирует
  профиль (R1–R33) fail-fast.

## Discoverability (RDK)

- On-demand only: run these when the user explicitly asks; never proactively.
- `npx repo-aeo audit` — Discoverability Score 0-100 and findings; read-only.
- The `rdk-audit` workflow fails pull requests below `vars.RDK_MIN_SCORE`.
