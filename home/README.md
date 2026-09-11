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

Если проект только Java и Flutter не нужен — поставь `WITH_FLUTTER=0`,
образ соберётся заметно быстрее и меньше.

## 3. Собрать и поднять

```powershell
docker compose build
docker compose up -d
docker compose ps
```

Первая сборка долгая: ставится Flutter SDK (~4 ГБ). Если сборка упадёт на
базовом образе (`eclipse-temurin:21-jdk-noble`) — это единственное место, где
тег не проверялся в песочнице; замени на актуальный тег Temurin 21.

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

Контейнер один и тот же: Java и Flutter обслуживаются им обоими, языковой
сервер выбирается по расширению файла (`jdtls` для `.java`, `dart` для `.dart`).

## 6. Остановить

```powershell
docker compose down          # туннель закроется, доступ извне пропадёт
```

## Если что-то не так

| Симптом | Что делать |
|---|---|
| `healthz` не отвечает | `docker compose logs toolbox` — мост сверяет версию Bun (нужна 1.3.14) и коммит upstream, зафиксированный `BRIDGE_COMMIT` в Dockerfile |
| 401 у агента | токен в `.env` и в `~/.remote-devbox-mcp.conf` у агента должны совпадать |
| URL каждый раз новый | это быстрый туннель; для постоянного создай именованный и впиши `TUNNEL_TOKEN` |
| сборка/тест обрываются по времени | подними `JOB_TIMEOUT_SECONDS` (максимум 3600) |
