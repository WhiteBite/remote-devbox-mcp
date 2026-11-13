# remote-devbox-mcp / egress - Cloudflare WARP (официальный клиент) как выход
# для туннелей cloudflared: провайдер видит только WARP-трафик, а не соединения
# с краем Cloudflare (cloudflare/cloudflared#1456).

FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl dbus gnupg iproute2 procps \
 && curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
      | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ bookworm main" \
      > /etc/apt/sources.list.d/cloudflare-client.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends cloudflare-warp \
 && rm -rf /var/lib/apt/lists/*

COPY egress-entrypoint.sh /usr/local/bin/egress-entrypoint.sh
RUN chmod +x /usr/local/bin/egress-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/egress-entrypoint.sh"]