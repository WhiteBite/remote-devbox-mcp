# Часть «дом» — установка на твой компьютер

## 0. Ежедневный запуск (ранбук)

Все команды из папки `home\`. Проект = профиль `..\projects\<имя>.ps1`.

```powershell
cd D:\Sources\WhiteBite\remote-devbox-mcp\home

# применить профиль проекта (рендер .env, host-сервисы, пересоздание toolbox):
.\devbox.ps1 use muffin
docker compose up -d
docker compose ps          # ждём: toolbox healthy, vpn healthy.
                           # Первый старт профиля = установка тулчейнов в
                           # /opt/tools (несколько минут), дальше — кэш
.\host\start-ingress.ps1 # один раз за сессию машины
docker compose logs cloudflared-ingress | Select-String trycloudflare  # -> INGRESS
# MCP_URL    = <INGRESS>/p/8787/mcp   токен MCP_BEARER_TOKEN
# supervisor = <INGRESS>/p/8792/mcp   токен MCP_PUBLIC_TOKEN
# всё прочее на хосте: <INGRESS>/p/<порт>/<путь> (см. §11)

# опционально, только для flutter-web с абсолютными ассетами:
docker compose --profile preview up -d
#   URL: docker compose logs cloudflared-preview | Select-String trycloudflare
```

Тулчейны: спек в профиле (`TOOLCHAIN="java21 flutter:3.44.9"`) entrypoint
идемпотентно ставит в `/opt/tools` (volume rdm-tools): первая установка платная,
повторные старты и смены проекта бесплатны, пересборки образа не нужно.
Git-идентичность агента в контейнере: `GIT_NAME`/`GIT_EMAIL` из профиля.
Постоянный скретч агента между сессиями: `/agent` (volume rdm-agent).

Закрыть доступ наружу: `docker compose stop cloudflared-ingress
cloudflared-preview` (или `down` — весь стек). Host-сервисы профиля:
`.\devbox.ps1 stop-host`; ingress: `.\host\stop-ingress.ps1`.

Смена проекта: `.\devbox.ps1 use <имя>` (туннели выживают, URL не меняется).

Нужны только Docker Desktop и доступ в интернет. Входящие порты на роутере не
требуются: туннель соединяется наружу сам.

## 1. Клонировать репозиторий

```powershell
git clone <URL-репозитория> C:\Users\<ты>\remote-devbox-mcp
cd C:\Users\<ты>\remote-devbox-mcp\home
```

Все команды ниже выполняются из папки `home\`. Её структура:

```
home\
├── docker-compose.yml
├── .env.example
├── cloudflared-config.example.yml
├── SECURITY.md
└── docker\
    └── toolbox.Dockerfile
```

## 2. Сделать .env

```powershell
copy .env.example .env
notepad .env
```

Заполнить:

```
PROJECT_DIR=C:/Users/<ты>/dev/backend     # папка проекта, прямые слеши
MCP_BEARER_TOKEN=<64 hex>
```

Если провайдер рвёт Cloudflare-туннели (DPI) — дополнительно впиши
`VLESS_SUB_URL` (ссылка на VLESS-подписку): туннели пойдут через встроенный
VPN-сайдкар (сервис `vpn`, sing-box в TUN-режиме). Пусто = прямой режим.

Токен сгенерировать так:

```powershell
-join ((1..64) | % { '{0:x}' -f (Get-Random -Max 16) })
```

Тулчейны задаёт профиль проекта (`TOOLCHAIN="java21 flutter:3.44.9"`):
entrypoint идемпотентно ставит их в `/opt/tools` (volume rdm-tools) — первая
установка платная, дальше кэш, пересборки образа не нужно. Всё, чего в спеке
нет, агент доустанавливает сам в `/opt/tools` через bash.

## 3. Собрать и поднять

```powershell
docker compose build
docker compose up -d
docker compose ps
```

Первый старт профиля ставит тулчейны в `/opt/tools` (java ~1 мин, flutter
~3–5 мин с прогревом Dart SDK) — контейнер становится healthy после установки.
Базовый образ — `node:22-bookworm-slim`; если установка падает на внешних
источниках (Adoptium API, github) — проверь их доступность.

## 4. Проверить

```powershell
# сервис жив? (порт наружу не публикуется, смотрим изнутри контейнера)
docker compose exec toolbox curl -fsS http://127.0.0.1:8787/healthz

# публичный URL входа
docker compose logs cloudflared-ingress | Select-String trycloudflare
```

`<INGRESS>/p/8787/mcp` + токен из `.env` — это всё, что нужно агенту для кода;
остальные эндпоинты — тем же ingress (см. §11).

## 5. Сменить проект

Одна команда:

```powershell
.\devbox.ps1 use <имя>     # профиль из ..\projects\<имя>.ps1
```

Профиль задаёт PROJECT_DIR, TOOLCHAIN, GIT_*, PREVIEW_ORIGIN и host-сервисы.
Туннели выживают, URL не меняется; тулчейны берутся из кэша `/opt/tools`,
 недостающие ставятся один раз. Языковой сервер выбирается по расширению
(`jdtls` для `.java`, `dart` для `.dart`) — при наличии тулчейна в спеке.

## 6. Остановить

```powershell
docker compose down          # туннель закроется, доступ извне пропадёт
```

## 7. Preview для агента (виджетбук, запущенное приложение)
Агент проверяет UI своим playwright из своей песочницы: приложение
поднимается на devbox и отдаётся наружу вторым URL туннеля.

Быстрый вариант (без домена, второй trycloudflare-туннель):

```powershell
# 1. Приложение на devbox (агент поднимет его сам через мост, либо ты руками):
docker compose exec toolbox flutter run -d web-server --web-hostname 0.0.0.0 --web-port 8788

# 2. Второй туннель на порт 8788 (по умолчанию выключен):
docker compose --profile preview up -d
docker compose logs cloudflared-preview | Select-String trycloudflare
```

Полученный `https://yyyyy.trycloudflare.com` пришли агенту. Токена на нём
нет — кто знает URL, тот видит приложение; после проверки выключи:
`docker compose stop cloudflared-preview`.

Постоянный вариант (свой домен на Cloudflare): именованный туннель с двумя
hostname — `mcp.* → toolbox:8787` и `preview.* → toolbox:8788`, настройка в
`cloudflared-config.example.yml`.

## 8. Оценка скриншотов агентом (профиль shots)

Агент может визуально оценивать скриншоты/статику проекта, не монтируя ничего
лишнего: любой статический сервер на хосте сразу виден через ingress:

```powershell
python -m http.server 8791 --bind 127.0.0.1 --directory <папка со скриншотами>
# агент: <INGRESS>/p/8791/<путь> + Authorization: Bearer <INGRESS_TOKEN>
```

Контейнеры и туннели под это не поднимаются вовсе (см. §11). URL защищён
токеном; предрелизный UI не публиковать дальше сессии оценки.

## 9. Выдача агенту доступа к проекту

**Смена проекта** — `.\devbox.ps1 use <имя>` (профиль из `..\projects\`).
`opencode.json` проекта автоматически экранируется shadow-монтом (воркер не
поднимает MCP-серверы проекта), тулчейны — спек `TOOLCHAIN` профиля в
`/opt/tools` (volume, кэш между проектами).

**Уровень доступа** задаётся правилами разрешений OpenCode в `.env`
(`OPENCODE_MCP_PERMISSIONS`). По умолчанию мутации требуют разрешения на каждый
джоб; `--auto` у клиента агента — твоё явное доверие агенту внутри workspace.
Read-only сессия (оценки, аудит):

```
OPENCODE_MCP_PERMISSIONS={"edit":"deny","write":"deny","apply_patch":"deny","bash":"deny"}
```

**Страховка**: токен ротируется правкой `.env` + `up -d`; после сессии доступ
наружу закрывается `docker compose stop cloudflared-ingress` (или `down`);
правки агента откатываются git'ом проекта (держи агента в ветке/worktree).
Это сетевой аналог правил `permission` локального OpenCode: граница
workspace + ask/allow на тулы.

## 10. Локальные MCP и host-команды наружу

Два механизма, оба через ingress без отдельных туннелей:

- **Bearer-сервисы профиля** (`$HostServices`, напр. muffin-supervisor):
  сервис слушает Port+1 без авторизации, auth-proxy на Port проверяет
  `MCP_PUBLIC_TOKEN`; агент ходит `/p/<Port>/mcp` со своим токеном.
- **runner-mcp** (`$RunnerCommands`): универсальный host-MCP для команд
  проекта (Windows-native стеки: MidasAI и т.п.). Команды = argv-массивы без
  shell, аргументы агента по схеме (path валидируется), background-процессы
  с pid-файлом, каждый вызов в аудит-логе `%TEMP%\rdm-runner\audit.log`.
  Агент: `MCP_CONF=~/.mcp-runner.conf ./mcp call run_<имя> ...`.

Ingress rout'ит только порты из `$AllowedPorts` профиля + SELF_AUTHED
(bridge, bearer-сервисы, runner-auth-proxy); всё остальное — 403 даже с
верным токеном (fail-closed).

## 11. Ingress: один вход на все эндпоинты

Профили preview/gallery/shots добавляют по контейнеру-туннелю на эндпоинт и
требуют ручного поднятия + передачи нового URL. Ingress убирает и то и другое:
один постоянный туннель (`cloudflared-ingress`, поднимается обычной `up -d`)
ведёт на `ingress-proxy` на хосте, который роутит по пути:

```
<INGRESS>/p/<порт>/<путь>  ->  127.0.0.1:<порт>/<путь>   (префикс срезается)
```

Новый эндпоинт = просто запущенный сервис на loopback-порту (supervisor op,
`python -m http.server`, dev-сервер и т.п.). URL детерминированный, агент
вычисляет его сам, контейнеры не плодятся, никто никого не ждёт.

Примеры (Muffin):
- галерея: `<INGRESS>/p/8765/docs/review/index.html#tab=screens&theme=mpearl`
- виджетбук: `<INGRESS>/p/8080/`
- supervisor MCP: `<INGRESS>/p/8792/mcp` (порт в SELF_AUTHED_PORTS: авторизует
  нижний auth-proxy своим MCP_PUBLIC_TOKEN, ingress-токен не подставляется)

Авторизация: Bearer `INGRESS_TOKEN` на каждый запрос для портов без своей
авторизации. Ограничение: веб-приложения с абсолютными путями ассетов
(flutter web без `--base-href`) под префиксом теряют ассеты — для них либо
сборка с `--base-href /p/<порт>/`, либо старый выделенный туннель-профиль.

Стоп: `home\host\stop-ingress.ps1` (только записанный pid, сверяя cmdline).

## Если что-то не так

| Симптом | Что делать |
|---|---|
| `healthz` не отвечает | `docker compose logs toolbox` — мост сверяет версию Bun (нужна 1.3.14) и коммит upstream, зафиксированный `BRIDGE_COMMIT` в Dockerfile |
| 401 у агента | токен в `.env` и в `~/.remote-devbox-mcp.conf` у агента должны совпадать |
| URL каждый раз новый | это быстрый туннель; для постоянного создай именованный и впиши `TUNNEL_TOKEN` |
| в логах cloudflared частые `Lost connection with the edge`, агент ловит 530/1033 | DPI провайдера рвёт соединения с краем Cloudflare — см. раздел ниже |
| сборка/тест обрываются по времени | подними `JOB_TIMEOUT_SECONDS` (максимум 3600) |

## Если Cloudflare-туннель рвёт DPI провайдера

Симптомы: в `docker compose logs cloudflared` каждые 10–60 с
`Lost connection with the edge` / `connection with edge closed`, снаружи
периодические 530/1033. На российских линиях это известная проблема
([cloudflare/cloudflared#1456](https://github.com/cloudflare/cloudflared/issues/1456)):
вмешательство в соединения с диапазоном края Cloudflare `198.41.128.0/17`.

**Основное решение уже встроено в стек:** сервис `vpn` (sing-box в TUN-режиме)
поднимает VLESS-подписку из `VLESS_SUB_URL`, и туннели cloudflared делят его
сетевое пространство (`network_mode: container:rdm-vpn`). Провайдер видит только
VLESS-трафик, туннель стабилен. Замер на этой же линии: 10/10 healthz → 200,
ноль обрывов за 5 минут — против 10/10 530 без VPN.

Особенности встроенного VPN:
- URL подписки должен отдавать vless-линки с UA sing-box/v2rayN (стандарт).
- Если сервер подписки без VPN недоступен (крутит редирект-петлю) — подписка
  кэшируется в volume `rdm-vpn-cache`: при первом запуске с доступным URL
  она сохраняется и дальше работает из кэша.
- Сервер выбирается TCP-пробингом по списку, каждые 60 с проверяется;
  3 подряд неудачи → ротация на следующий живой сервер.
- Хостовый TUN VPN включать/выключать можно независимо: соединение с VLESS
  просто поедет внутри него (VPN-в-VPN), оба состояния рабочие.
- Если сервис `vpn` пересоздался (пересборка образа, `up -d` после правок) —
  туннели нужно пересоздать следом, они держат жёсткую ссылку на его netns:
  `docker compose up -d --force-recreate cloudflared` (и `cloudflared-preview`,
  если профиль preview поднят).

**План Б — альтернативный туннель** (если подписки нет и VLESS не вариант).
Порты проброшены на `127.0.0.1` (8787 — MCP, 8788 — preview):

- `ssh -R` на свой VPS + reverse-proxy (Caddy/nginx) — самый стабильный,
  SSH DPI не трогает:
  ```bash
  ssh -N -R 19000:localhost:8787 -R 19001:localhost:8788 \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 user@твой-vps
  ```
- localhost.run — без аккаунта, URL сразу: `ssh -R 80:localhost:8787 nokey@localhost.run`
- pinggy.io — без аккаунта: `ssh -p 443 -R0:localhost:8787 a.pinggy.io`
- Tailscale funnel — нужен аккаунт: `tailscale funnel 8787`

Агенту вместо trycloudflare-URL просто отдаётся URL альтернативного туннеля;
токен и порядок работы не меняются.

**Эксперимент 2026-09-11, результат отрицательный.** zapret (winws, desync-профиль
ALT) с расширенными списками (`argotunnel.com`/`trycloudflare.com` в hostlist,
`198.41.128.0/17` в ipset) и cloudflared, запущенным прямо на хосте (чтобы
WinDivert видел трафик): соединения всё равно рвались каждые 40–60 с,
внешние запросы 10/10 → 530. Вмешательство происходит в живую сессию, а desync
обманывает DPI только на старте рукопожатия — этот путь закрыт.

**Не кладите `opencode.json` с MCP-серверами в workspace девбокса.** Бридж
публикует наружу только нативные тулы OpenCode (MCP-серверы проекта фильтруются
и не видны агенту), но OpenCode-воркер пытается их поднять внутри контейнера:
готовность моста деградирует с ~30 с до минут и дольше (замерено на тестовом
`opencode.json` с `server-everything`). MCP-конфиги проекта (как в Muffin) —
для хостового OpenCode/Kiro, не для девбокса.
