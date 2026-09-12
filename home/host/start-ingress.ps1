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
$env:PROXY_PORT = "8799"
$selfAuthed = (Get-Content $envFile | Where-Object { $_ -match '^SELF_AUTHED_PORTS=' } | Select-Object -First 1)
$selfAuthed = if ($selfAuthed) { ($selfAuthed -replace '^SELF_AUTHED_PORTS=', '').Trim() } else { "8787,8792" }
$env:SELF_AUTHED_PORTS = $selfAuthed
$allowed = (Get-Content $envFile | Where-Object { $_ -match '^ALLOWED_PORTS=' } | Select-Object -First 1)
$env:ALLOWED_PORTS = if ($allowed) { ($allowed -replace '^ALLOWED_PORTS=', '').Trim() } else { "" }
$env:RDM_MANIFEST_PATH = Join-Path $env:TEMP "rdm-host\rdm-manifest.json"
$p = Start-Process -FilePath "python" -ArgumentList "`"$here\ingress-proxy.py`"" `
  -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "$logDir\ingress.out" -RedirectStandardError "$logDir\ingress.err"
"$($p.Id)" | Out-File "$logDir\pids.txt"
"ingress-proxy pid=$($p.Id) :8799; логи: $logDir\"
