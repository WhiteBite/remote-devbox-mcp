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
cd remote-devbox-mcp/home
copy .env.example .env          # заполнить PROJECT_DIR и токены
python devbox.py use muffin      # применить профиль проекта
docker compose up -d
python -m pytest -q               # проверить окружение (200 тестов)
```

Публичный вход: `docker compose logs cloudflared-ingress | Select-String trycloudflare`.
Дальше агенту отдаётся `INGRESS` + токены — полный цикл в `ARENA.md`.

```
┌─ home/  → ставится на ТВОЙ комп (Windows + Docker Desktop)
│   docker-compose.yml             toolbox + встроенный VPN (VLESS) + туннели cloudflared
│   docker/toolbox.Dockerfile      образ: мост к тулам OpenCode (стеково-нейтральный;
│                                  JDK 21 / Flutter — опциональные build-args)
│   docker/vpn.Dockerfile          sing-box TUN-сайдкар: туннели едут через VLESS,
│                                  DPI провайдера до туннеля не достаёт
│   docker/vpn-entrypoint.sh       подписка → конфиг sing-box, пробинг и ротация серверов
│   .env.example                   → скопировать в .env, заполнить PROJECT_DIR и токен
│   devbox.py / devbox.cmd       ядро (Python): профили, host-сервисы, ingress, doctor
│   devbox.ps1                   тонкий шим к devbox.py
│   rdm/                         core-пакет: envfile, hostos, profiles, proxy, render,
│                                procman, doctor, watchdog, runner
│   host/runner-mcp.py           шим к rdm.runner (host-MCP команд проекта)
│   SECURITY.md                  что запрещено монтировать, про изоляцию честно
│
├─ projects/ → профили проектов (project_dir, тулчейны, host-сервисы), JSON
│   muffin.json, _template.json
│
└─ arena/ → сторона агента (скачивает сам, ничего ставить не нужно)
    mcp_client.py             MCP-клиент на чистом Python (stdlib, 3.10+)
    mcp                       обёртка: ./mcp run --auto edit '{...}'
    mcp.conf.example          → ~/.remote-devbox-mcp.conf (URL + токен)
    AGENT_INSTRUCTIONS.md     правила, по которым агент работает с твоим компом
    SANDBOX_FACTS.md          замеренные факты о песочнице агента
```

## Разделение труда

- **Devbox (твой комп):** код проекта, компиляция, тесты, LSP, запуск
  приложения. Тулчейны (JDK 21, Flutter нужного тега) ставятся в `/opt/tools`
  по спеку `TOOLCHAIN` профиля проекта и кэшируются между проектами; прочее
  агент доустанавливает сам.
- **Песочница агента:** вспомогательные тулы — playwright + chromium,
  codegraph, конвертеры — агент ставит у себя через bash и дёргает локально,
  в devbox-образ они не попадают (`arena/SANDBOX_FACTS.md`).

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
ТЗ задачи лежит в проекте: `/workspace/ARENA_TASK.md` (конвенция).

Читай `arena/AGENT_INSTRUCTIONS.md` — там протокол джобов и разрешений, имена
тулов, коды возврата и обязательства (диагностика после правки, git-дисциплина,
секреты не читать). Быстрый старт:

```bash
git clone --depth 1 https://github.com/WhiteBite/remote-devbox-mcp
cd remote-devbox-mcp
cp arena/mcp.conf.example ~/.remote-devbox-mcp.conf   # вписать URL и токен
cd arena && chmod +x mcp && ./mcp check
```

## Безопасность

Токен даёт доступ к папке проекта через туннель — фактически удалённый shell
внутри контейнера. Подробности и что нельзя монтировать — `home/SECURITY.md`.
Не публикуй URL, а по завершении работы поменяй `MCP_BEARER_TOKEN` в `.env`
и выполни `docker compose up -d`. Остановить всё одной командой:
`docker compose down`.

## Статус / Status

Активная разработка. Host-часть покрыта 217 tests (прогон ~6 s, unit/integration/e2e), CI гоняет
`python -m pytest` и rdk-audit (discoverability 96/100). Профиль проекта — `projects/*.json`;
ядро — `home/rdm/`.

## Лицензия

MIT — см. [LICENSE](LICENSE).

## Для кого это / Who is it for

Для разработчика с Windows-машиной и Docker Desktop, который хочет, чтобы
внешний кодинг-агент (arena.ai, Claude, Codex — любой MCP-клиент) собирал,
тестировал и правил код **на его собственном железе**, а не в одноразовой
облачной песочнице без доступа к реальному проекту.

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
