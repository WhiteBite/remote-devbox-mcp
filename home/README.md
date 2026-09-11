# Часть «дом» — установка на твой компьютер

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
