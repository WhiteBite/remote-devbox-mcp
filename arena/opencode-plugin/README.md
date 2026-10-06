# OpenCode-плагин devbox (опционально)

Необязательный ускоритель: если ты запускаешь **локальный** OpenCode и хочешь,
чтобы он работал с devbox, плагин избавляет от ручного конфига. Это **пример**,
не покрытый pytest-сьютом репозитория; сверься с версией своего OpenCode.

## Что делает

- `shell.env` — пробрасывает в shell агента переменные, по которым
  `arena/mcp` работает и без `~/.remote-devbox-mcp.conf`.
- `tool.execute.before` — блокирует чтение файлов-секретов (`.env`, `*.pem`,
  `*.key`, `*.p12`).

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

Значения — из hand-off-блока: `MCP_URL=<INGRESS>/p/8787/mcp` +
`MCP_TOKEN=<BRIDGE_TOKEN>`; runner — `<INGRESS>/p/<порт>/mcp` + `<HOST_TOKEN>`.

## Если плагин не подходит

Надёжный путь без плагина: `arena/mcp-config.example.json` (runner и
supervisor — `type: "remote"`, работают сразу) и `arena/mcp-stdio-adapter.py`
для моста. Отдельно: `opencode serve` регистрирует MCP-сервер динамически —
`POST /mcp` с телом `{name, config}`.

## `.well-known/opencode`

OpenCode читает `GET <base>/.well-known/opencode` → `{config?, remote_config?}` —
канал анонса дефолтных MCP-серверов организацией. Bearer-токен в нём размещать
**нельзя**: файл публичный.