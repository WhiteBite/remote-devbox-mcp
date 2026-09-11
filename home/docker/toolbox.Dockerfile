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
# Тулчейны проекта — двумя способами:
#   * тяжёлые известные стеки — build-аргументы:
#       WITH_JAVA=1     JDK 21 (Temurin из Adoptium apt) — нужен ещё и для jdtls
#       WITH_FLUTTER=1  Flutter/Dart SDK (~4 ГБ) — нужен ещё и для dart LSP
#   * всё остальное агент ставит рантайм через bash в /opt/tools
#     (персистентный volume; /opt/tools/bin уже в PATH).
#
# Версии зафиксированы по требованиям моста (он на старте сверяет Bun,
# коммит upstream и хеши адаптера — и падает, а не деградирует):
#   Bun 1.3.14 · OpenCode v1.18.29 · Node 22+
#
# Сборка на твоём компе, из папки home/ репозитория remote-devbox-mcp:
#     docker compose build
#
# ← ПРОВЕРЬ ПРИ СБОРКЕ: доступность Flutter-ветки и репозитория Adoptium.

FROM node:22-bookworm-slim

ARG BUN_VERSION=1.3.14
ARG BRIDGE_REPO=https://github.com/nmt3325/notioncode.git
# Тегов у моста нет — пин по SHA. Мост на старте сверяет Bun/upstream-коммит
# и падает при рассинхроне, поэтому новый SHA обновляй вместе с BUN_VERSION.
ARG BRIDGE_COMMIT=4a6e45c58ecd2aad47f867aea35e0e8aaf9bac15
ARG WITH_JAVA=0
ARG WITH_FLUTTER=0
ARG FLUTTER_CHANNEL=stable

ENV DEBIAN_FRONTEND=noninteractive

# git нужен мосту для проверки upstream, ripgrep — нативному тулу grep
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git unzip xz-utils ripgrep gnupg \
    && rm -rf /var/lib/apt/lists/*

# Bun точной версии — мост отказывается стартовать на другой
RUN curl -fsSL https://bun.sh/install | bash -s "bun-v${BUN_VERSION}"

# JDK 21 из apt-репозитория Adoptium — стек опционален, базовый образ не тянет
RUN if [ "${WITH_JAVA}" = "1" ]; then \
        curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public \
            | gpg --dearmor -o /usr/share/keyrings/adoptium.gpg \
        && echo "deb [signed-by=/usr/share/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" \
            > /etc/apt/sources.list.d/adoptium.list \
        && apt-get update \
        && apt-get install -y --no-install-recommends temurin-21-jdk \
        && rm -rf /var/lib/apt/lists/* \
        && mkdir -p /opt/java \
        && ln -s /usr/lib/jvm/temurin-21-jdk-* /opt/java/openjdk; \
    fi
ENV JAVA_HOME=/opt/java/openjdk

# Flutter SDK (даёт и `flutter`, и `dart` для языкового сервера).
# Долго и ~4 ГБ — только если проект реально Flutter.
RUN if [ "${WITH_FLUTTER}" = "1" ]; then \
        git clone --depth 1 -b "${FLUTTER_CHANNEL}" \
            https://github.com/flutter/flutter.git /opt/flutter \
        && /opt/flutter/bin/flutter --version \
        && /opt/flutter/bin/flutter config --no-analytics; \
    fi

ENV PATH="/opt/tools/bin:/opt/flutter/bin:/root/.bun/bin:${PATH}"

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

# /healthz отдаёт только health/mode/version — секреты там не светятся
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -fsS http://127.0.0.1:8787/healthz || exit 1

CMD ["npm", "run", "start:http"]
