#!/bin/sh
# remote-devbox-mcp: VLESS-сайдкар. Подписка -> sing-box в TUN-режиме.
# Туннели cloudflared делят его netns (network_mode: container:rdm-vpn),
# провайдер видит только VLESS-трафик к серверу подписки.
set -u

CACHE=/cache/sub.txt
LINKS=/tmp/links.txt
CONF=/run/sing-box.json
UA="sing-box/1.11.15"

log() { echo "[vpn] $*"; }

if [ -z "${VLESS_SUB_URL:-}" ]; then
  log "VLESS_SUB_URL пуст — прямой режим: трафик туннелей идёт напрямую"
  exec sleep infinity
fi
touch /run/vpn-active

fetch_sub() {
  raw=$(curl -fsSL -A "$UA" --max-time 20 --max-redirs 5 "$VLESS_SUB_URL" 2>/dev/null) || raw=""
  if [ -n "$raw" ]; then
    printf '%s' "$raw" > "$CACHE"
  else
    log "подписка не скачалась, беру кэш"
    raw=$(cat "$CACHE" 2>/dev/null) || raw=""
  fi
  [ -n "$raw" ] || { log "нет ни подписки, ни кэша"; exit 1; }
  dec=$(printf '%s' "$raw" | base64 -d 2>/dev/null || true)
  printf '%s' "$dec" | grep -q 'vless://' || dec=$raw
  printf '%s' "$dec" | grep -o 'vless://[^[:space:]"]*' > "$LINKS" || true
  [ -s "$LINKS" ] || { log "в подписке нет vless-линков"; exit 1; }
  log "серверов в подписке: $(wc -l < "$LINKS")"
}

# значение query-параметра из QUERY
getp() { printf '%s' "$QUERY" | tr '&' '\n' | sed -n "s/^$1=//p" | head -1; }

# TCP-доступность host:port (busybox nc -z: чистый connect, без ожидания данных —
# TLS-сервер молчит до ClientHello, поэтому curl telnet:// здесь не подходит)
tcp_alive() { nc -z -w 3 "$1" "$2" >/dev/null 2>&1; }

# $1 = смещение в списке. Пишет CONF, печатает "idx host port". 1 = никто не жив.
probe_and_write() {
  total=$(wc -l < "$LINKS")
  i=$1; cnt=0
  while [ "$cnt" -lt "$total" ]; do
    i=$(( i % total + 1 )); cnt=$((cnt + 1))
    link=$(sed -n "${i}p" "$LINKS")
    u=${link#vless://}
    uuid=${u%%@*}; rest=${u#*@}
    hostport=${rest%%\?*}; hostport=${hostport%%#*}
    QUERY=$(printf '%s' "$rest" | sed -n 's/^[^?]*?\([^#]*\).*/\1/p')
    host=${hostport%%:*}; port=${hostport##*:}
    type=$(getp type); flow=$(getp flow)
    [ "$type" = "xhttp" ] && { log "пропуск xhttp ($host)"; continue; }
    tcp_alive "$host" "$port" || { log "мёртв: $host:$port"; continue; }
    sni=$(getp sni); pbk=$(getp pbk); sid=$(getp sid); fp=$(getp fp)
    [ -n "$fp" ] || fp=chrome
    [ -n "$pbk" ] || { log "у $host нет reality public_key, пропуск"; continue; }
    if [ "$type" = "grpc" ]; then
      jq -n --arg s "$host" --argjson p "$port" --arg u "$uuid" \
            --arg sni "$sni" --arg pbk "$pbk" --arg sid "$sid" --arg fp "$fp" \
            --arg svc "$(getp serviceName)" '
        {log:{level:"warn"},
         inbounds:[{type:"tun",tag:"tun-in",interface_name:"tun0",
                    address:["198.18.0.1/30"],auto_route:true,sniff:true}],
         outbounds:[{type:"vless",tag:"vless",server:$s,server_port:$p,uuid:$u,
                     tls:{enabled:true,server_name:$sni,
                          utls:{enabled:true,fingerprint:$fp},
                          reality:{enabled:true,public_key:$pbk,short_id:$sid}},
                     transport:{type:"grpc",service_name:$svc}},
                    {type:"direct",tag:"direct"}],
         route:{rules:[{ip_cidr:["10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","127.0.0.0/8"],
                        outbound:"direct"}],
                final:"vless",auto_detect_interface:true}}' > "$CONF"
    else
      jq -n --arg s "$host" --argjson p "$port" --arg u "$uuid" \
            --arg sni "$sni" --arg pbk "$pbk" --arg sid "$sid" --arg fp "$fp" \
            --arg flow "$flow" '
        {log:{level:"warn"},
         inbounds:[{type:"tun",tag:"tun-in",interface_name:"tun0",
                    address:["198.18.0.1/30"],auto_route:true,sniff:true}],
         outbounds:[{type:"vless",tag:"vless",server:$s,server_port:$p,uuid:$u,
                     flow:$flow,
                     tls:{enabled:true,server_name:$sni,
                          utls:{enabled:true,fingerprint:$fp},
                          reality:{enabled:true,public_key:$pbk,short_id:$sid}}},
                    {type:"direct",tag:"direct"}],
         route:{rules:[{ip_cidr:["10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","127.0.0.0/8"],
                        outbound:"direct"}],
                final:"vless",auto_detect_interface:true}}' > "$CONF"
    fi
    echo "$i $host $port"
    return 0
  done
  return 1
}

fetch_sub
OFFSET=0
while true; do
  picked=$(probe_and_write "$OFFSET") || {
    log "живых серверов нет; повтор через 30 с"
    sleep 30; fetch_sub; continue
  }
  OFFSET=${picked%% *}
  CUR_HOST=$(echo "$picked" | awk '{print $2}')
  CUR_PORT=$(echo "$picked" | awk '{print $3}')
  log "сервер: $CUR_HOST:$CUR_PORT (#$OFFSET)"
  sing-box run -c "$CONF" &
  SB=$!
  fails=0
  while kill -0 $SB 2>/dev/null; do
    sleep 60
    kill -0 $SB 2>/dev/null || break
    if tcp_alive "$CUR_HOST" "$CUR_PORT"; then
      fails=0
    else
      fails=$((fails + 1)); log "проба $CUR_HOST не прошла ($fails/3)"
      if [ "$fails" -ge 3 ]; then
        log "ротация: сервер сменится"
        kill $SB 2>/dev/null; wait $SB 2>/dev/null
        break
      fi
    fi
  done
  wait $SB 2>/dev/null || true
  sleep 2
done
