# remote-devbox-mcp / toolbox — ОДИН контейнер = ОДИН проект.
# Корень задаётся переменной PROJECT_DIR в .env, ничего больше менять не нужно.
#
# Образ стеково-нейтральный: внутри только мост к тулам OpenCode и минимум для
# тула bash (git, ripgrep, curl). Никакого агента и никакой LLM: мост только
# исполняет (read / write / edit / apply_patch / glob / grep / bash /
# todowrite + lsp при OPENCODE_MCP_LSP=true), думает агент на своей стороне.
#
# Кастомные тулы агента (playwright, codegraph и т.п.) сюда НЕ пекутся:
# агент ставит их у себя в песочнице и дёргает через свой bash
# (см. arena/SANDBOX_FACTS.md и arena/AGENT_INSTRUCTIONS.md §6).
#
# Тулчейны проекта НЕ пекутся в образ: entrypoint (docker/toolchain.sh)
# идемпотентно ставит их в /opt/tools (volume rdm-tools) по переменной
# TOOLCHAIN ("java21 flutter:3.44.9 ..."). Первая установка платная,
# дальше кэш: смена проекта не требует пересборки образа вообще.
#
# Версии зафиксированы по требованиям моста (он на старте сверяет Bun,
# коммит upstream и хеши адаптера — и падает, а не деградирует):
#   Bun 1.3.14 · OpenCode v1.18.29 · Node 22+
#
# Сборка на твоём компе, из папки home/ репозитория remote-devbox-mcp:
#     docker compose build toolbox

FROM node:22-bookworm-slim

ARG BUN_VERSION=1.3.14
ARG BRIDGE_REPO=https://github.com/nmt3325/notioncode.git
# Тегов у моста нет — пин по SHA. Мост на старте сверяет Bun/upstream-коммит
# и падает при рассинхроне, поэтому новый SHA обновляй вместе с BUN_VERSION.
ARG BRIDGE_COMMIT=4a6e45c58ecd2aad47f867aea35e0e8aaf9bac15

ENV DEBIAN_FRONTEND=noninteractive

# git нужен мосту для проверки upstream, ripgrep — нативному тулу grep
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git unzip xz-utils ripgrep gnupg \
    && rm -rf /var/lib/apt/lists/*

# Bun точной версии — мост отказывается стартовать на другой
RUN curl -fsSL https://bun.sh/install | bash -s "bun-v${BUN_VERSION}"
ENV PATH="/opt/tools/bin:/root/.bun/bin:${PATH}"

# Сам мост: собираем В ОБРАЗЕ, а не на старте контейнера.
# Рантайм (.opencode-runtime) живёт вне /workspace — так требует мост:
# «Keep the runtime and state directories outside the editable workspace».
WORKDIR /opt/bridge
RUN git init -q . \
    && git remote add origin "${BRIDGE_REPO}" \
    && git fetch --depth 1 origin "${BRIDGE_COMMIT}" \
    && git checkout -q FETCH_HEAD \
    && npm ci \
    && npm run build \
    && npm run setup:native

# Мост требует для state-каталога права 0700 и падает иначе;
# именованный volume при первом подключении наследует права этого каталога.
# /opt/tools — рантайм-тулчейны агента, volume переживает пересоздание контейнера.
RUN mkdir -p /state /opt/tools/bin && chmod 700 /state

ENV OPENCODE_MCP_RUNTIME_DIR=/opt/bridge/.opencode-runtime
ENV OPENCODE_MCP_STATE_DIR=/state
ENV OPENCODE_MCP_HOST=0.0.0.0
ENV OPENCODE_MCP_PORT=8787
ENV OPENCODE_MCP_LSP=true

EXPOSE 8787

# /healthz отдаёт только health/mode/version — секреты там не светятся.
# start-period большой: первая установка тулчейнов в volume платная.
HEALTHCHECK --interval=30s --timeout=5s --start-period=900s \
    CMD curl -fsS http://127.0.0.1:8787/healthz || exit 1

COPY toolchain.sh /usr/local/bin/devbox-entrypoint.sh
COPY toolchains/ /usr/local/bin/toolchains/
RUN chmod +x /usr/local/bin/devbox-entrypoint.sh /usr/local/bin/toolchains/*.sh

ENTRYPOINT ["/usr/local/bin/devbox-entrypoint.sh"]
