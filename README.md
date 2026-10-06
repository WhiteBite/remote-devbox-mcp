# remote-devbox-mcp — твой комп как удалённый devbox для арена-агента

Агент (например, arena.ai в агентском режиме) не имеет своего железа для
сборок. Этот репозиторий соединяет его с **твоим** компом: у тебя крутится
контейнер с инструментами (без LLM), агент подключается к нему через исходящий
Cloudflare-туннель и правит код, собирает и тестирует у тебя.

Никаких сторонних агентов: на твоём компе — только исполнитель, на стороне
агента — только тонкий клиент, который его дёргает.

## Быстрый старт / Quickstart

```bash
git clone https://github.com/WhiteBite/remote-devbox-mcp
cd remote-devbox-mcp
python -m pip install -r requirements.txt   # host-зависимости: psutil, mcp
cd home
copy .env.example .env           # заполнить PROJECT_DIR и токены
python devbox.py start muffin     # профиль + стек + ingress + блок для агента
```

Публичный вход: `docker compose logs cloudflared-ingress | Select-String trycloudflare`.
Дальше агенту отдаётся `INGRESS` + токены — полный цикл в `ARENA.md`.
Проверка окружения (pytest/ruff) — в разделе «Разработка».

```
┌─ home/  → ставится на ТВОЙ комп (Windows + Docker Desktop)
│   docker-compose.yml             toolbox + встроенный VPN (VLESS) + туннели cloudflared
│   docker/toolbox.Dockerfile      образ: мост к 9 нативным тулам OpenCode (стеково-нейтральный)
│   docker/toolchain.sh            идемпотентная установка TOOLCHAIN в /opt/tools (volume)
│   docker/toolchains/            java21.sh, flutter.sh — опциональные тулчейны
│   docker/vpn.Dockerfile          sing-box TUN-сайдкар: туннели едут через VLESS,
│   docker/vpn-entrypoint.sh       подписка → конфиг sing-box, пробинг и ротация серверов
│   docker/gitleaks.toml           конфиг фонового скана секретов проекта
│   .env.example                   → скопировать в .env, заполнить PROJECT_DIR и токен
│   devbox.py / devbox.cmd / devbox.ps1   ядро (Python): профили, host-сервисы, ingress, doctor
│   tray.py                        трей-пульт (requirements-tray.txt), запуск через tray.cmd
│   rdm/                           core-пакет: cli, profiles, render, envfile, ports, procman,
│                                  hostos, docker, netprobe, freeze, tokens, tunnels, doctor,
│                                  watchdog, proxy/ (ingress), runner/ (host-MCP)
│   host/runner-mcp.py             шим к rdm.runner (host-MCP команд проекта)
│   README.md                      подробный ранбук по установке и эксплуатации
│   SECURITY.md                    что запрещено монтировать, про изоляцию честно
│
├─ projects/ → профили проектов (project_dir, тулчейны, host-сервисы), JSON
│   muffin.json, midasai.json, _template.json
│
├─ arena/ → сторона агента (скачивает сам, ничего ставить не нужно)
│   mcp_client.py             MCP-клиент на чистом Python (stdlib, 3.10+)
│   mcp                       обёртка: ./mcp run --auto edit '{...}'
│   mcp.conf.example          → ~/.remote-devbox-mcp.conf (URL + токен)
│   mcp-stdio-adapter.py      мост как обычный MCP-сервер по stdio (--trust / --readonly)
│   mcp-config.example.json   готовый MCP-конфиг для OpenCode/Claude-класса
│   opencode-plugin/          опциональный OpenCode-плагин (пример)
│   AGENT_INSTRUCTIONS.md     правила, по которым агент работает с твоим компом
│   TOOLS.md                  канонический список тулов моста (9 native + 2 control)
│   SANDBOX_FACTS.md          замеренные факты о песочнице агента
│
├─ skills/remote-devbox/SKILL.md   агент-скилл (arena.ai и подобные MCP-клиенты)
├─ scripts/                   build_exe.py + entrypoints frozen-сборки
├─ .github/workflows/         tests, build (exe по тегу v*), rdk-audit
├─ docs/, llms.txt            AEO/LLM-артефакты, генерируются из .discoverability/
├─ start.cmd / tray.cmd       лончеры из корня репо (делегируют в home/)
└─ ARENA.md                   полная инструкция для стороннего агента
```

## Разделение труда

- **Devbox (твой комп):** код проекта, компиляция, тесты, LSP, запуск
  приложения. Тулчейны (JDK 21, Flutter нужного тега) ставятся в `/opt/tools`
  по спеку `TOOLCHAIN` профиля проекта и кэшируются между проектами; прочее
  агент доустанавливает сам.
- **Песочница агента:** вспомогательные тулы — playwright + chromium,
  codegraph, конвертеры — агент ставит у себя через bash и дёргает локально,
  в devbox-образ они не попадают (`arena/SANDBOX_FACTS.md`).

## Установка без Python (готовый exe)

Сборки — в [Releases](https://github.com/WhiteBite/remote-devbox-mcp/releases):
`devbox-windows-x64.zip` / `devbox-linux-x64.zip`. Распакуй, `home\.env.example`
скопируй в `home\.env` и заполни — дальше как в quickstart, только вместо
`python devbox.py` — `home\devbox.exe` (в Windows есть и `devbox-tray.exe` —
трей-пульт без консоли). Артефакт повторяет layout репозитория (`home/` +
`projects/`), поэтому compose-проект и volumes общие с исходниковым запуском.
Локальная сборка: `python scripts/build_exe.py` (pyinstaller — в
requirements-dev.txt); CI собирает то же самое по тегу `v*`. Из исходников те же
действия доступны из корня репо: `start.cmd` (то же, что `home\devbox.cmd start`)
и `tray.cmd`.

## Порядок действий

1. **Ты:** клонируй репозиторий, перейди в `home/`, сделай `.env` из
   `.env.example` (токены) и выполни `.\devbox.cmd use <имя>` +
   `docker compose up -d` (профиль проекта: `..\projects\<имя>.json`).
2. **Ты:** возьми публичный URL входа —
   `docker compose logs cloudflared-ingress | Select-String trycloudflare`.
3. **Ты:** пришли агенту INGRESS-URL и токены и ссылку на репозиторий.
4. **Агент:** скачает папку `arena/`, запишет конфиги (`/p/8787/mcp` для кода,
   `/p/<порт>/mcp` для host-MCP), выполнит `./mcp check` и покажет ответ твоего
   контейнера. Дальше правит код уже у тебя.

## Агенту

Точка входа — [ARENA.md](ARENA.md): полная инструкция по подключению и работе
(bootstrap, правила, рабочий цикл, шаблон сообщения-передачи для пользователя).
Задачу агент получает в чате.
Готовый агент-скилл: [skills/remote-devbox/SKILL.md](skills/remote-devbox/SKILL.md).

Читай `arena/AGENT_INSTRUCTIONS.md` — там протокол джобов и разрешений, имена
тулов, коды возврата и обязательства (диагностика после правки, git-дисциплина,
секреты не читать). Быстрый старт:

```bash
git clone --depth 1 https://github.com/WhiteBite/remote-devbox-mcp
cd remote-devbox-mcp
cp arena/mcp.conf.example ~/.remote-devbox-mcp.conf   # вписать URL и токен
cd arena && chmod +x mcp && ./mcp check
```

Клиентам OpenCode/Claude-класса — готовый конфиг
[arena/mcp-config.example.json](arena/mcp-config.example.json): runner и
supervisor как `remote`-серверы (`Authorization: Bearer {env:MCP_PUBLIC_TOKEN}`),
мост — через stdio-адаптер `arena/mcp-stdio-adapter.py` с `--trust`
(одобряет мутации; `--readonly` — отказывает). Соответствие имён токенов:
`MCP_TOKEN` конфига арены = `MCP_BEARER_TOKEN` из `home/.env` (мост),
`HOST_TOKEN` = `MCP_PUBLIC_TOKEN` (runner и host-сервисы со своей авторизацией),
`INGRESS_TOKEN` закрывает весь HTTP `/p/<порт>`, включая `/p/9000/manifest.json`.
Stdio-only клиенты (Codex-класс):
`npx mcp-remote <url> --header "Authorization: Bearer <token>"`.

## Безопасность

Токен даёт доступ к папке проекта через туннель — фактически удалённый shell
внутри контейнера. Подробности и что нельзя монтировать — `home/SECURITY.md`.
Не публикуй URL, а по завершении работы поменяй `MCP_BEARER_TOKEN` в `.env`
и выполни `docker compose up -d`. Остановить всё одной командой:
`docker compose down`.

## Разработка / Development

```bash
python -m pip install -r requirements-dev.txt   # host deps + pytest, ruff, pyinstaller
python -m pytest                                 # tests/unit, tests/integration, tests/e2e
python -m ruff check home arena tests            # линт (конфиг в pyproject.toml)
python scripts/build_exe.py                      # локальная frozen-сборка в dist/
```

`tests/unit/test_docs_sync.py` сверяет exit-коды в `ARENA.md` с константами
`arena/mcp_client.py` — правка одной стороны без другой валит тест.

## Статус / Status

Активная разработка. Host-часть покрыта pytest-сьютом (unit/integration/e2e), CI гоняет
`python -m pytest` и rdk-audit (discoverability 96/100). Профиль проекта — `projects/*.json`;
ядро — `home/rdm/`.

## Лицензия

MIT — см. [LICENSE](LICENSE). JSON-LD-описание репозитория — [docs/jsonld.jsonld](docs/jsonld.jsonld).

## Для кого это / Who is it for

Для разработчика с Windows-машиной и Docker Desktop, который хочет, чтобы
внешний кодинг-агент (arena.ai, Claude, Codex и другие MCP-клиенты) собирал,
тестировал и правил код **на его собственном железе**, а не в одноразовой
облачной песочнице без доступа к реальному проекту. Класс клиента имеет
значение: чтение работает у всех, мутации — через arena-клиент (`--auto`),
stdio-адаптер (`--trust`) или режим профиля `full`; матрица возможностей —
`ARENA.md`.

## Сценарии / Use cases

- Агент билдит и гоняет тесты большого проекта (Java/Gradle, Flutter/web) на
  твоём компе: у него CPU, RAM и кэш сборки, которых нет ни у какой песочницы.
- Работа с приватным кодом, который нельзя выгружать в облако: код не
  покидает твою машину, наружу идут только запросы инструментов через туннель.
- Preview запущенного приложения для визуальной проверки агентом: второй
  туннель отдаёт dev-сервер (`docker compose --profile preview up -d`).
- Host-MCP для Windows-native стеков: команды профиля (`$RunnerCommands`)
  выполняются на хосте без shell, аргументы валидируются, каждый вызов —
  в аудит-логе.
- Смена проекта одной командой (`.\devbox.cmd use <имя>`): профиль задаёт
  папку, тулчейны, права; туннель и URL при этом не меняются.
- Read-only аудит и оценки: режим `readonly` запрещает мутации, агент только
  читает и запускает диагностику.

## Почему так, а не иначе / Why choose this

- Никакой сторонней исполнительной стороны: на твоём компе только контейнер-
  исполнитель, на стороне агента — тонкий Python-клиент из `arena/` (stdlib,
  ставить ничего не нужно).
- Outbound-only: входящие порты на роутере не открываются; если DPI провайдера
  рвёт Cloudflare-туннель, встроенный VLESS-сайдкар (sing-box) это обходит.
- Профили проектов вместо ручных конфигов: `TOOLCHAIN`, `$Mode`
  (`readonly | standard | full`), `$AllowedPorts`, `$DenyMounts` — fail-closed
  механика, а не обещания (см. `home/SECURITY.md`).
- Тулчейны кэшируются между проектами в volume `/opt/tools`: первая установка
  платная, пересборки образа не нужно.

## Примеры

Поднять devbox под проект `muffin` (из папки `home\`, PowerShell):

```powershell
copy .env.example .env      # заполнить PROJECT_DIR и токены
.\devbox.cmd use muffin     # профиль ..\projects\muffin.json
docker compose up -d
docker compose logs cloudflared-ingress | Select-String trycloudflare
```

Проверить доступность и поработать с кодом (сторона агента, bash):

```bash
cp arena/mcp.conf.example ~/.remote-devbox-mcp.conf   # вписать MCP_URL и MCP_TOKEN
cd arena && ./mcp check
./mcp run read '{"filePath":"src/Main.java","offset":1,"limit":80}'
./mcp run --auto bash '{"command":"./gradlew test"}'
```
