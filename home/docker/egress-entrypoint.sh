#!/bin/sh
# Ключи WARP живут в /var/lib/cloudflare-warp (volume rdm-egress-state), в образ ничего не зашито.
set -eu

STATE=/var/lib/cloudflare-warp
log() { echo "[egress] $*"; }

mkdir -p /run/dbus "$STATE"
rm -f /run/dbus/pid
dbus-daemon --config-file=/usr/share/dbus-1/system.conf

warp-svc --accept-tos >/var/log/warp-svc.log 2>&1 &
for _ in $(seq 1 30); do
  warp-cli --accept-tos status >/dev/null 2>&1 && break
  sleep 1
done

if [ ! -f "$STATE/reg.json" ]; then
  log "регистрируем анонимное WARP-устройство"
  warp-cli --accept-tos registration new || log "регистрация не прошла; см. /var/log/warp-svc.log"
fi

warp-cli --accept-tos connect || log "connect не прошёл"
log "WARP стартовал"

while true; do
  sleep 30
  if ! warp-cli --accept-tos status 2>/dev/null | grep -qi connected; then
    log "соединение потеряно, переподключаемся"
    warp-cli --accept-tos connect || true
  fi
done