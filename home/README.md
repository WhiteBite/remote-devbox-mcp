# Часть «дом» — установка на твой компьютер

## 0. Ежедневный запуск (ранбук)

Все команды из папки `home\`. `.env` уже настроен (PROJECT_DIR, токены).

```powershell
cd D:\Sources\WhiteBite\remote-devbox-mcp\home

# база: toolbox + VPN-сайдкар + MCP-туннель
docker compose up -d
docker compose ps                       # ждём: toolbox healthy, vpn healthy
docker compose logs cloudflared | Select-String trycloudflare   # -> hostname
# MCP_URL = https://<hostname>/mcp, токен = MCP_BEARER_TOKEN из .env

# опционально, по задаче:
docker compose --profile preview up -d   # preview запущенного приложения
#   (внутри toolbox поднять приложение, напр. nohup node serve.js &)
docker compose --profile shots up -d     # скриншоты для оценки агентом
#   сначала на хосте: python -m http.server 8791 --bind 127.0.0.1 --directory <папка>
docker compose --profile mcp up -d       # локальные MCP наружу (supervisor)
#   сначала на хосте: home\host\start-mcp-public.ps1
#   URL: docker compose logs cloudflared-mcp | Select-String trycloudflare
docker compose --profile gallery up -d   # review-сайт галереи с хоста (:8765)
#   URL: docker compose logs cloudflared-gallery | Select-String trycloudflare

# постоянный ingress на ВСЕ loopback-эндпоинты хоста (без профилей):
#   на хосте: home\host\start-ingress.ps1
#   URL: docker compose logs cloudflared-ingress | Select-String trycloudflare
#   доступ: <INGRESS>/p/<порт>/<путь> + заголовок Authorization: Bearer <INGRESS_TOKEN>
#   порты со своей авторизацией (8792) — с их токеном, ingress-токен не нужен
```

Закрыть доступ наружу: `docker compose stop cloudflared cloudflared-mcp
cloudflared-preview cloudflared-shots` (или `down` — весь стек). Хостовую
цепочку mcp: `home\host\stop-mcp-public.ps1`.

Смена проекта: правка `PROJECT_DIR` в `.env` → `docker compose up -d`
(туннели выживают). Смена стека (`WITH_JAVA`/`WITH_FLUTTER`): сначала
`docker compose build toolbox`. URL меняются только при пересоздании
соответствующего cloudflared или vpn-контейнера.

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

Тулчейны опциональны: `WITH_JAVA=1` — JDK 21 (Temurin из Adoptium apt) для
Java/Spring и jdtls; `WITH_FLUTTER=1` — Flutter/Dart SDK (~4 ГБ, сборка
заметно дольше). По умолчанию оба 0 — получается стеково-нейтральный
toolbox: всё, чего не хватит, агент доустановит рантайм в `/opt/tools`
(volume, переживает пересоздание контейнера).

## 3. Собрать и поднять

```powershell
docker compose build
docker compose up -d
docker compose ps
```

Первая сборка при `WITH_FLUTTER=1` долгая: ставится Flutter SDK (~4 ГБ).
Базовый образ — `node:22-bookworm-slim`, JDK ставится из apt-репозитория
Adoptium; если сборка падает на этих внешних источниках — проверь их
доступность, из песочницы агента они не проверялись.

## 4. Проверить

```powershell
# сервис жив? (порт наружу не публикуется, смотрим изнутри контейнера)
docker compose exec toolbox curl -fsS http://127.0.0.1:8787/healthz

# публичный URL туннеля
docker compose logs cloudflared | Select-String trycloudflare
```

URL вида `https://xxxxx.trycloudflare.com` + токен из `.env` — это всё,
что нужно агенту.

## 5. Сменить проект

Одна строка в `.env` и перезапуск:

```powershell
notepad .env                 # PROJECT_DIR=C:/Users/<ты>/dev/app
docker compose up -d
```

Контейнер один и тот же: языковой сервер выбирается по расширению файла
(`jdtls` для `.java`, `dart` для `.dart`) — при условии, что стек испечён
в образ (`WITH_JAVA` / `WITH_FLUTTER`).

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

Агент может визуально оценивать скриншоты проекта, не монтируя проект в
девбокс: на хосте поднимается статический сервер, туннель идёт через тот же
VPN-сайдкар (профиль `shots`).

```powershell
# 1. статический сервер на хосте (loopback; Docker Desktop форвардит
#    host.docker.internal в loopback хоста):
python -m http.server 8791 --bind 127.0.0.1 --directory <папка со скриншотами>
# 2. туннель:
docker compose --profile shots up -d
docker compose logs cloudflared-shots | Select-String trycloudflare
# 3. после оценки — убрать:
docker compose stop cloudflared-shots
#    и убить python-сервер (pid в файле, которым его запускали)
```

URL скриншотов **без токена** (случайный hostname = слабая защита): поднимать
только на время сессии оценки, предрелизный UI не публиковать дальше.

## 9. Выдача агенту доступа к проекту

**Смена проекта** — одна строка в `.env` (`PROJECT_DIR`) + `docker compose up -d`.
`opencode.json` проекта автоматически экранируется shadow-монтом (воркер не
поднимает MCP-серверы проекта), тулчейны — build-args `WITH_JAVA`/`WITH_FLUTTER`.

**Уровень доступа** задаётся правилами разрешений OpenCode в `.env`
(`OPENCODE_MCP_PERMISSIONS`). По умолчанию мутации требуют разрешения на каждый
джоб; `--auto` у клиента агента — твоё явное доверие агенту внутри workspace.
Read-only сессия (оценки, аудит):

```
OPENCODE_MCP_PERMISSIONS={"edit":"deny","write":"deny","apply_patch":"deny","bash":"deny"}
```

**Страховка**: токен ротируется правкой `.env` + `up -d`; после сессии доступ
наружу закрывается `docker compose stop cloudflared` (или `down`); правки агента
откатываются git'ом проекта (держи агента в ветке/worktree). Это сетевой аналог
правил `permission` локального OpenCode: граница workspace + ask/allow на тулы.

## 10. Локальные MCP наружу (профиль mcp)

Свои MCP-серверы (например, `muffin-supervisor` из Muffin) можно отдать агенту
отдельным эндпоинтом с авторизацией. Мост их не публикует (каталог бриджа —
только нативные тулы OpenCode), поэтому цепочка отдельная:

```
MCP-сервер (streamable-http, 127.0.0.1:8790)
  → auth-proxy (Bearer, 127.0.0.1:8792)   # home/host/auth-proxy.py
  → cloudflared-mcp (профиль mcp, через vpn-сайдкар)
  → агент (mcp_client.py с вторым конфигом)
```

Запуск:

```powershell
# 1. токен в .env: MCP_PUBLIC_TOKEN=<64 hex>
# 2. сервер + прокси на хосте:
home\host\start-mcp-public.ps1
# 3. туннель:
docker compose --profile mcp up -d
docker compose logs cloudflared-mcp | Select-String trycloudflare
```

Агент работает со вторым конфигом: `MCP_CONF=~/.mcp-supervisor.conf ./mcp list`.
Сервер по умолчанию — muffin-supervisor (HTTP-режим включается его переменной
`MCP_HTTP_PORT`, правка в Muffin `tools/muffin-supervisor/cli.py`). Другой сервер:
`-ServerCwd/-ServerCommand/-ServerPort`. Стоп: `home\host\stop-mcp-public.ps1`
(убивает только записанные при старте pid, сверяя cmdline — pid мог быть
переиспользован ОС) + `docker compose stop cloudflared-mcp`. Процессы никогда не
убиваются по паттерну cmdline: на машине легально живут чужие python/node-
серверы с такими же командами; чужие сервисы останавливаются только их штатными
командами (у Muffin — `supervisor cli stop`).

Прокси сырым TCP: Bearer на каждый запрос (keep-alive у cloudflared
переиспользует соединения), Host переписывается на loopback, X-Forwarded-Host
вырезается — FastMCP валидирует Host (DNS-rebinding) и без этого отбивает 421.

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
