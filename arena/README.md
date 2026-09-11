# Часть «арена» — сторона агента

**Пользователю здесь ничего устанавливать не нужно.** Агент сам скачивает эту
папку из репозитория (`git clone --depth 1` или как умеет) и работает ею как
тонким клиентом. Требования в песочнице агента: `python3` 3.10+, `bash`.

Здесь нет ни LLM, ни второго агента: это тонкий клиент, которым агент вызывает
инструменты контейнера на машине пользователя. Весь «мозг» — на стороне агента,
весь «исполнитель» — на стороне пользователя.

## Файлы

| Файл | Назначение |
|---|---|
| `mcp_client.py` | MCP-клиент: streamable HTTP и stdio, только стандартная библиотека Python |
| `mcp` | обёртка: берёт URL и токен из `~/.remote-devbox-mcp.conf` |
| `mcp.conf.example` | шаблон конфига |
| `AGENT_INSTRUCTIONS.md` | правила работы агента с машиной пользователя (тулы, джобы, ограничения) |

## Конфиг

Агент создаёт `~/.remote-devbox-mcp.conf` сам, получив от пользователя две
строки (URL + токен):

```
MCP_URL=https://xxxxx.trycloudflare.com/mcp
MCP_TOKEN=<тот же, что MCP_BEARER_TOKEN в .env>
MCP_TIMEOUT=300
```

## Чем агент пользуется

```bash
./mcp check                                  # рукопожатие + список тулов
./mcp list
./mcp schema edit                            # схема аргументов
./mcp run read '{"filePath":"src/Main.java","offset":1,"limit":80}'
./mcp run --auto edit '{"filePath":"...","oldString":"...","newString":"..."}'
./mcp run --auto bash '{"command":"./gradlew test"}'
./mcp reply <job_id> <permission_id> once    # разрешить конкретный запрос
./mcp job <job_id>                           # дозапросить результат
```

Правки файлов, shell-команды и патчи на той стороне по умолчанию требуют
разрешения (джоб в статусе `awaiting_permission`), поэтому `run --auto` сам
отвечает `once` и дожидается результата. Долгие сборки покрывает серверное
ожидание моста: клиент передаёт `wait_seconds` в `opencode_job_result`, один
опрос ≈ 45 с, дефолтные 60 опросов ≈ 45 минут. Ненулевой код команды
(`exit 3`) клиент пробрасывает как ошибку — упавший тест не выглядит успехом.

## Соответствие протоколу

Клиент написан по контракту моста `nmt3325/notioncode` (opencode-toolbox):
форма джоба `JobView` (статусы включают `cancelling`), разрешение в поле
`permission: {id, permission, patterns}`, управляющие тулы
`opencode_permission_reply` / `opencode_job_result`, код возврата bash в
`result.metadata.exit`, путь обрезанного вывода в `metadata.outputPath`.
