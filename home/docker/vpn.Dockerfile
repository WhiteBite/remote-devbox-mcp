# remote-devbox-mcp / vpn — VLESS-сайдкар для туннелей cloudflared.
# sing-box в TUN-режиме; cloudflared-сервисы делят его netns
# (network_mode: container:rdm-vpn). Трафик туннелей провайдеру виден
# только как VLESS к серверу подписки — DPI против края Cloudflare обойден.

FROM alpine:3.21

ARG SINGBOX_VERSION=1.11.15

RUN apk add --no-cache curl jq ca-certificates iproute2 \
 && curl -fsSL "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-amd64.tar.gz" -o /tmp/sb.tgz \
 && tar -xzf /tmp/sb.tgz -C /tmp \
 && install -m755 "/tmp/sing-box-${SINGBOX_VERSION}-linux-amd64/sing-box" /usr/local/bin/sing-box \
 && rm -rf /tmp/sb.tgz /tmp/sing-box-*

COPY vpn-entrypoint.sh /usr/local/bin/vpn-entrypoint.sh
RUN chmod +x /usr/local/bin/vpn-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/vpn-entrypoint.sh"]
