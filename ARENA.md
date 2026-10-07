# ARENA.md — входная точка для стороннего агента (arena.ai и подобные)

Это полная инструкция по подключению и работе. Живых значений (URL, токены)
в репозитории нет: пользователь передаёт их в сообщении вместе со ссылкой на
этот файл. Всё остальное агент берёт отсюда и из файлов, на которые есть ссылки.

Машиночитаемый агент-скилл: `skills/remote-devbox/SKILL.md`.
Канонический список тулов: `arena/TOOLS.md`.

Цепочка входных точек (читать по порядку): `ARENA.md` →
`skills/remote-devbox/SKILL.md` → `arena/AGENT_INSTRUCTIONS.md`.

## 0. К чему ты подключаешься

- **devbox на машине пользователя**: контейнер toolbox с проектом (`/workspace`)
   и тулчейнами; внутри opencode-mcp-bridge — 10 native тулов
   (read/write/edit/apply_patch/glob/grep/bash/todowrite/lsp и
   permission-gated webfetch) и 6 control (opencode_native_info,
   opencode_job_list, opencode_job_result, opencode_job_cancel,
   opencode_permissions_pending, opencode_permission_reply) с джоб-протоколом;
   канонический список — `arena/TOOLS.md`.
- **манифест окружения**: `devbox use` пишет `AGENTS.md` (профиль, тулчейны,
   режим, порты, команды раннера) в `/workspace/.devbox/AGENTS.md` — открой его
   тулом `read` моста; копия в `/agent/AGENTS.md` лежит вне корня моста, для неё
   нужен `bash cat`. Каталог `.devbox/` генерируется — не коммить его.
- **ingress**: единственный публичный вход `<INGRESS>`; любой эндпоинт хоста =
  путь `<INGRESS>/p/<порт>/<путь>`. Код проекта: `/p/8787/mcp`.
- **твоя песочница**: изолирована; файлов проекта в ней нет — всё через мост.

## 1. Bootstrap

```bash
git clone --depth 1 https://github.com/WhiteBite/remote-devbox-mcp devbox
cd devbox/arena && chmod +x mcp     # после каждого снапшота песочницы бит слетает
```

В сообщении пользователя вместе со ссылкой на этот файл должны быть:
`INGRESS`, `BRIDGE_TOKEN`, при необходимости `HOST_TOKEN` и список доп.
эндпоинтов. Чего-то нет — спроси, не выдумывай.

```bash
cat > ~/.remote-devbox-mcp.conf <<EOF
MCP_URL=<INGRESS>/p/8787/mcp
MCP_TOKEN=<BRIDGE_TOKEN>
MCP_TIMEOUT=300
EOF
./mcp check        # ожидаешь opencode-mcp-bridge и список из 10 native + 6 control тулов (16)
```

Конфиги сразу скопируй в свой **Workspace** (песочница сбрасывается, Workspace —
нет; восстановление на старте сессии — см. скилл «Persistence»).

Персистентные 530/1033: подожди 2–3 минуты (watchdog ротирует VLESS-сервер),
повтори `check` один раз; снова персистентно — остановись и сообщи пользователю.

## 2. Правила до любой работы

- `arena/AGENT_INSTRUCTIONS.md` — джоб-протокол, разрешения (`--auto`),
  exit-коды в `result.metadata.exit` (completed ≠ успех), таймауты вызовов
  ≥ 300 с для всего, что ждёт permission или долгий джоб, скретч `/agent`,
  мутации только внутри `/workspace`, секреты и `.env` не читать.
- `arena/SANDBOX_FACTS.md` — факты о твоей песочнице: root, apt, что не
  персистентно между ходами, таймауты bash, серверы только через start_process.

## 3. ТЗ

Задача приходит **от пользователя в чате** (файла-ТЗ нет). Читай её до любого
плана; не выдумывай задачу сам. Ожидай: цель, scope, критерии приёмки,
процедуру проверки, что не трогать.

## 4. Рабочий цикл

1. Карта/план из ТЗ → показать пользователю, дождаться согласия.
2. Правки пакетами: только файловые тулы моста (edit/apply_patch);
   никаких `cat > file` через bash.
3. Верификация пакета: analyze/test через bash моста; рестарт dev-серверов
   через host-MCP, если ТЗ его указывает; скриншоты до/после своим playwright
   (порты без своей авторизации требуют заголовок
   `Authorization: Bearer <INGRESS_TOKEN>`).
   Для UI: открой приложение своим playwright по `<INGRESS>/p/<порт>/` с
   `extraHTTPHeaders={"Authorization": "Bearer <INGRESS_TOKEN>"}`, либо по
   `PREVIEW`-URL (root, без токена) — детали и оговорки в
   `skills/remote-devbox/SKILL.md`.
4. Отчёт: git diff через bash моста, результаты тестов, скриншоты через
   present_file, отступления от ТЗ — явно.

## 5. Дополнительные эндпоинты

Шаблон: `<INGRESS>/p/<порт>/<путь>`. Набор портов проекта даёт пользователь
в сообщении или ТЗ; ingress rout'ит только порты из allowlist профиля
(остальные — 403). Порты со своим bearer-токеном (напр. супервайзер `:8792`):
конфиг отдельный (`MCP_CONF=~/.mcp-supervisor.conf`), вызовы через `call`,
а не `run` — там нет джоб-протокола.

Проекты с host-сборкой (напр. MidasAI: node_modules/.venv принадлежат хост-ОС):
сборки/тесты/dev-серверы идут через **runner** — третий конфиг
`~/.mcp-runner.conf`: `MCP_URL=<RUNNER_URL>` (строка из блока) и
`MCP_TOKEN=<HOST_TOKEN>` — токены моста и раннера НЕ взаимозаменяемы
(401 на эндпоинте = отправлен чужой токен); `runner_list` покажет
объявленные команды, вызов `run_<имя>` с аргументами строго по схеме
(path-аргументы относительные, без `..`). Bash моста в таких проектах —
только git и read-only; `pnpm install`/`pip install` в контейнере запрещены
(испортят хост-артефакты другой ОС).

Коды возврата клиента: 0 успех, 2 конфиг, 3 туннель, 4 разрешение, 5 джоб-ошибка (в т.ч. джоб, не завершившийся за отведённые опросы) — используй для ветвления в скриптах.

## 6. Матрица возможностей клиентов

| Клиент | Чтение | Мутации |
|---|---|---|
| arena-клиент `./mcp` | да | да — `--auto` сам отвечает на запрос разрешения |
| stdio-адаптер `arena/mcp-stdio-adapter.py` | да | да с `--trust` (авто-одобрение); `--readonly` отказывает |
| обычный MCP-клиент напрямую в `/p/8787/mcp` | да | только в режиме профиля `full` (авто-разрешение); в `standard` джоб висит в `awaiting_permission`; в `readonly` мутации отвергаются |
| обычный MCP-клиент в runner / supervisor | да | да — обычный MCP без джоб-протокола |

Мост отвечает на каждый вызов джобом (JobView), поэтому клиент, не умеющий
отвечать на разрешения, увидит зависший джоб, а не результат. Готовый конфиг
для OpenCode/Claude-класса — `arena/mcp-config.example.json`.

## Приложение. Шаблон передачи задачи (для пользователя)

```
Репозиторий: https://github.com/WhiteBite/remote-devbox-mcp
Скилл + инструкция: https://github.com/WhiteBite/remote-devbox-mcp/blob/main/skills/remote-devbox/SKILL.md , https://github.com/WhiteBite/remote-devbox-mcp/blob/main/ARENA.md
INGRESS=https://<актуальный>.trycloudflare.com   # или PUBLIC_URL при именованном туннеле
BRIDGE_TOKEN=<MCP_BEARER_TOKEN из home/.env>  # /p/8787/mcp — код: read/edit/write/bash
INGRESS_TOKEN=<INGRESS_TOKEN из home/.env>    # UI и прочие порты (Authorization: Bearer)
UI=<INGRESS>/p/<порт>/                        # UI приложения для playwright (Bearer INGRESS_TOKEN)
PREVIEW=<preview-URL>                         # если поднят preview-туннель
RUNNER_URL=<INGRESS>/p/<порт раннера>/mcp     # MCP_URL для ~/.mcp-runner.conf (только если есть runner)
HOST_TOKEN=<MCP_PUBLIC_TOKEN из home/.env>    # MCP_TOKEN для RUNNER_URL
```

Реестр эндпоинтов: GET <INGRESS>/p/9000/manifest.json с заголовком Authorization: Bearer <INGRESS_TOKEN> — машиночитаемый список эндпоинтов, портов и команд раннера; не перечисляй порты вручную.

Актуальный INGRESS: `docker compose logs cloudflared-ingress | Select-String trycloudflare`
из папки `home\` на машине пользователя.
