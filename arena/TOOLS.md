# TOOLS.md — канонический список тулов моста

Это единственный канонический список инструментов opencode-mcp-bridge.
`tools/list` возвращает 9 native + 2 control тула. Аргументы и поведение —
`arena/AGENT_INSTRUCTIONS.md` §2-§3, схемы — `./mcp schema <имя>`.

| Тул | Kind | Назначение |
|---|---|---|
| `read` | native | чтение файла (`filePath`, `offset` с единицы, `limit`) |
| `write` | native | перезапись файла целиком |
| `edit` | native | точечная замена (`oldString`/`newString`) |
| `apply_patch` | native | патч-блок несколькими файлами за один вызов |
| `glob` | native | поиск имён файлов по шаблону |
| `grep` | native | ripgrep по содержимому |
| `bash` | native | команды на devbox (только `command`) |
| `lsp` | native | языковые операции (`hover`, `findReferences`, …) |
| `todowrite` | native | рабочий список агента |
| `opencode_permission_reply` | control | ответить на запрос разрешения джоба |
| `opencode_job_result` | control | состояние/результат джоба (`wait_seconds` до 50) |

Примечание: `webfetch` — permission-gated; может отсутствовать в `tools/list`
зависимо от сборки моста. По умолчанию уходит в `awaiting_permission`, как
мутации.
