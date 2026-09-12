# remote-devbox-mcp — твой комп как удалённый devbox для арена-агента

Агент (например, arena.ai в агентском режиме) не имеет своего железа для
сборок. Этот репозиторий соединяет его с **твоим** компом: у тебя крутится
контейнер с инструментами (без LLM), агент подключается к нему через исходящий
Cloudflare-туннель и правит код, собирает и тестирует у тебя.

Никаких сторонних агентов: на твоём компе — только исполнитель, на стороне
агента — только тонкий клиент, который его дёргает.

```
┌─ home/  → ставится на ТВОЙ комп (Windows + Docker Desktop)
│   docker-compose.yml             toolbox + встроенный VPN (VLESS) + туннели cloudflared
│   docker/toolbox.Dockerfile      образ: мост к тулам OpenCode (стеково-нейтральный;
│                                  JDK 21 / Flutter — опциональные build-args)
│   docker/vpn.Dockerfile          sing-box TUN-сайдкар: туннели едут через VLESS,
│                                  DPI провайдера до туннеля не достаёт
│   docker/vpn-entrypoint.sh       подписка → конфиг sing-box, пробинг и ротация серверов
│   .env.example                   → скопировать в .env, заполнить PROJECT_DIR и токен
│   cloudflared-config.example.yml именованный туннель: MCP + preview приложения
│   host/auth-proxy.py             bearer-прокси для выдачи локальных MCP наружу
│   host/start-mcp-public.ps1      цепочка: MCP-сервер + прокси (профиль mcp)
│   SECURITY.md                    что запрещено монтировать, про изоляцию честно
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
  приложения. Тяжёлые тулчейны (JDK 21, Flutter) пекутся в образ build-args
  по необходимости (`WITH_JAVA` / `WITH_FLUTTER` в `.env`); прочее агент
  доустанавливает рантайм в `/opt/tools` (персистентный volume).
- **Песочница агента:** вспомогательные тулы — playwright + chromium,
  codegraph, конвертеры — агент ставит у себя через bash и дёргает локально,
  в devbox-образ они не попадают (`arena/SANDBOX_FACTS.md`).

## Порядок действий

1. **Ты:** клонируй репозиторий, перейди в `home/`, сделай `.env` из
   `.env.example` (папка проекта + токен; `WITH_JAVA=1` / `WITH_FLUTTER=1`,
   если проект этих стеков) и выполни
   `docker compose build ; docker compose up -d`.
2. **Ты:** возьми публичный URL —
   `docker compose logs cloudflared | Select-String trycloudflare`.
3. **Ты:** пришли агенту этот URL и токен (две строки) и ссылку на репозиторий.
4. **Агент:** скачает папку `arena/`, запишет URL и токен в
   `~/.remote-devbox-mcp.conf`, выполнит `./mcp check` и покажет ответ твоего
   контейнера. Дальше правит код уже у тебя.

## Агенту

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

## Лицензия

MIT — см. [LICENSE](LICENSE).
