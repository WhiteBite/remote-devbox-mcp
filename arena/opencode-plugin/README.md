# OpenCode-плагин devbox (опционально)

Необязательный ускоритель: если ты запускаешь **локальный** OpenCode и хочешь,
чтобы он работал с devbox, плагин избавляет от ручного конфига. Это **пример**
(не часть pytest-сьюта), сверься с версией своего OpenCode. Чистые хелперы
покрыты юнит-тестами на `node:test` — см. «Тесты» ниже.

## Тесты

```bash
node --test arena/opencode-plugin/devbox.test.mjs
```

Без зависимостей; Node 22.6+ (тип-стриппинг `.ts` при импорте; проверено на 24).

## Что делает

- `shell.env` — пробрасывает в shell агента переменные, по которым
  `arena/mcp` работает и без `~/.remote-devbox-mcp.conf`.
- `tool.execute.before` — блокирует чтение файлов-секретов (`.env`, `*.pem`,
  `*.key`, `*.p12`).
- при `DEVBOX_REGISTER_MCP=1` — регистрирует мост как stdio MCP-сервер
  `devbox-bridge` через SDK-метод `client.mcp.add` (это тот же `POST /mcp`
  с телом `{name, config}`, что и у `opencode serve`; метод «Add MCP server
  dynamically» из `@opencode-ai/sdk`, проверен на 1.18.35).

## Установка

Скопируй `devbox.ts` в `.opencode/plugins/` проекта или в
`~/.config/opencode/plugins/` глобально.

## Настройка (переменные окружения, в которых запущен OpenCode)

| Переменная | Куда попадает у агента |
|---|---|
| `DEVBOX_MCP_URL` | `MCP_URL` |
| `DEVBOX_MCP_TOKEN` | `MCP_TOKEN` |
| `DEVBOX_RUNNER_URL` | `MCP_RUNNER_URL` |
| `DEVBOX_RUNNER_TOKEN` | `MCP_RUNNER_TOKEN` |

Поведение:

| Переменная | Эффект |
|---|---|
| `DEVBOX_READONLY=1` | блокирует мутирующие тулы MCP-сервера devbox (`write`/`edit`/`apply_patch`/`bash`/`webfetch`) |
| `DEVBOX_SERVER_PREFIX` | префикс имени MCP-сервера devbox у OpenCode (напр. `devbox_bridge_`); без него `DEVBOX_READONLY` не активен |
| `DEVBOX_REGISTER_MCP=1` | регистрирует `devbox-bridge` через `client.mcp.add` (см. ниже) |
| `DEVBOX_ADAPTER_PATH` | путь к адаптеру (дефолт `arena/mcp-stdio-adapter.py`, относительно каталога проекта OpenCode; если репозиторий не открыт как проект — абсолютный путь) |

Значения — из hand-off-блока: `MCP_URL=<INGRESS>/p/8787/mcp` +
`MCP_TOKEN=<BRIDGE_TOKEN>`; runner — `<INGRESS>/p/<порт>/mcp` + `<HOST_TOKEN>`.

## Регистрация моста (`DEVBOX_REGISTER_MCP=1`)

Плагин при старте вызывает `client.mcp.add` и регистрирует локальный
stdio-сервер:

- имя `devbox-bridge` — как в `arena/mcp-config.example.json`; тулы видны
  как `devbox-bridge_<tool>`, для `DEVBOX_SERVER_PREFIX` подойдёт
  `devbox-bridge_`;
- команда: `python` (Windows) / `python3` + `DEVBOX_ADAPTER_PATH` +
  `--readonly` при `DEVBOX_READONLY=1`, иначе `--trust`;
- `MCP_URL` / `MCP_TOKEN` передаются в окружение сервера;
- регистрация асинхронная: не блокирует старт OpenCode, сервер появляется
  в `GET /mcp` сразу после бутстрапа.

Ограничения:

- регистрация перекрывает одноимённую запись `devbox-bridge` из конфига на
  время сессии; при ротации URL туннеля перезапусти OpenCode;
- если туннель холодный на старте, адаптер завершается без апстрима и сервер
  попадает в статус `failed` — то же поведение, что у декларативного конфига;
- если метод недоступен (старый OpenCode) или запрос упал — плагин пишет
  warning в лог сервера и продолжает работать: env-инъекция активна, мост
  подключается вручную через `arena/mcp-config.example.json`.

## Если плагин не подходит

Надёжный путь без плагина: `arena/mcp-config.example.json` (runner и
supervisor — `type: "remote"`, работают сразу) и `arena/mcp-stdio-adapter.py`
для моста. Отдельно: `opencode serve` регистрирует MCP-сервер динамически —
`POST /mcp` с телом `{name, config}`.

## Скилл и `.well-known/opencode`

Скилл remote-devbox для OpenCode ставится в
`~/.config/opencode/skills/remote-devbox/SKILL.md` (глобально) или
`.opencode/skills/remote-devbox/SKILL.md` (в проекте) — пути и правила
распространения см. в [skills/remote-devbox/SKILL.md](../../skills/remote-devbox/SKILL.md).

OpenCode читает `GET <base>/.well-known/opencode` → `{config?, remote_config?}` —
канал анонса дефолтных MCP-серверов организацией. Bearer-токен в нём размещать
**нельзя**: файл публичный.