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

Заполнить две строки:

```
PROJECT_DIR=C:/Users/<ты>/dev/backend     # папка проекта, прямые слеши
MCP_BEARER_TOKEN=<64 hex>
```

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
docker compose exec toolbox flutter run -d web-server --web-hostname 0.0.0.0 --web-port 8080

# 2. Второй туннель на порт 8080 (по умолчанию выключен):
docker compose --profile preview up -d
docker compose logs cloudflared-preview | Select-String trycloudflare
```

Полученный `https://yyyyy.trycloudflare.com` пришли агенту. Токена на нём
нет — кто знает URL, тот видит приложение; после проверки выключи:
`docker compose stop cloudflared-preview`.

Постоянный вариант (свой домен на Cloudflare): именованный туннель с двумя
hostname — `mcp.* → toolbox:8787` и `preview.* → toolbox:8080`, настройка в
`cloudflared-config.example.yml`.

## Если что-то не так

| Симптом | Что делать |
|---|---|
| `healthz` не отвечает | `docker compose logs toolbox` — мост сверяет версию Bun (нужна 1.3.14) и коммит upstream, зафиксированный `BRIDGE_COMMIT` в Dockerfile |
| 401 у агента | токен в `.env` и в `~/.remote-devbox-mcp.conf` у агента должны совпадать |
| URL каждый раз новый | это быстрый туннель; для постоянного создай именованный и впиши `TUNNEL_TOKEN` |
| в логах cloudflared частые `Lost connection with the edge`, агент ловит 530/1033 | DPI провайдера рвёт соединения с краем Cloudflare (частая картина на QUIC — поэтому в compose стоит `--protocol http2`). Лечится маршрутом хоста через VPN или именованным туннелем через свой VPS. Клиент агента сам ретраит 502/520/521/523/524/530 |
| сборка/тест обрываются по времени | подними `JOB_TIMEOUT_SECONDS` (максимум 3600) |
