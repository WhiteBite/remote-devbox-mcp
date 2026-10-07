# TOOLS.md — канонический список тулов моста

Это единственный канонический список инструментов opencode-mcp-bridge.
`tools/list` возвращает 10 native + 6 control тулов (16). Аргументы и
поведение — `arena/AGENT_INSTRUCTIONS.md` §2-§3, схемы — `./mcp schema <имя>`.

| Тул | Kind | Назначение |
|---|---|---|
| `read` | native | чтение файла (`filePath`, `offset` с единицы, `limit`) |
| `write` | native | перезапись файла целиком |
| `edit` | native | точечная замена (`oldString`/`newString`) |
| `apply_patch` | native | патч-блок несколькими файлами за один вызов |
| `glob` | native | поиск имён файлов по шаблону |
| `grep` | native | ripgrep по содержимому |
| `bash` | native | команды на devbox (только `command`) |
| `webfetch` | native | загрузка URL; permission-gated |
| `todowrite` | native | рабочий список агента |
| `lsp` | native | языковые операции (`hover`, `findReferences`, …) |
| `opencode_native_info` | control | информация о мосте и окружении |
| `opencode_job_list` | control | список джобов |
| `opencode_job_result` | control | состояние/результат джоба (`wait_seconds` до 50) |
| `opencode_job_cancel` | control | отмена джоба |
| `opencode_permissions_pending` | control | ожидающие решения запросы разрешений |
| `opencode_permission_reply` | control | ответить на запрос разрешения джоба |

Примечание: `webfetch` — permission-gated; по умолчанию уходит в
`awaiting_permission`, как мутации.
