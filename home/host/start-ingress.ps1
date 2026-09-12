# start-ingress.ps1 — поднимает ingress-proxy (одна точка входа на все
# loopback-эндпоинты). Туннель к нему — сервис cloudflared-ingress в compose
# (поднимается обычной `docker compose up -d`, без профилей).
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path (Split-Path -Parent $here) ".env"
$tokenLine = Get-Content $envFile | Where-Object { $_ -match '^INGRESS_TOKEN=' } | Select-Object -First 1
$token = ($tokenLine -replace '^INGRESS_TOKEN=', '').Trim()
if (-not $token) { throw "INGRESS_TOKEN не задан в $envFile" }

$logDir = Join-Path $env:TEMP "rdm-ingress"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:INGRESS_TOKEN = $token
$env:PROXY_PORT = "8793"
$env:SELF_AUTHED_PORTS = "8792"
$p = Start-Process -FilePath "python" -ArgumentList "`"$here\ingress-proxy.py`"" `
  -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "$logDir\ingress.out" -RedirectStandardError "$logDir\ingress.err"
"$($p.Id)" | Out-File "$logDir\pids.txt"
"ingress-proxy pid=$($p.Id) :8793; логи: $logDir\"
