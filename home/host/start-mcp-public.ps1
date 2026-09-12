# start-mcp-public.ps1 — поднимает локальный MCP (streamable-http) + bearer-прокси.
#
# Наружу выходит через compose-профиль `mcp`:
#   docker compose --profile mcp up -d
#   docker compose logs cloudflared-mcp | Select-String trycloudflare
#
# Цепочка: supervisor (:8790, loopback) -> auth-proxy (:8792, loopback,
# Bearer) -> cloudflared-mcp -> агент. Остановка: stop-mcp-public.ps1.
[CmdletBinding()]
param(
  [string]$ServerCwd = "D:\Sources\StartUp\Muffin",
  [string]$ServerCommand = "python tools/muffin-supervisor/server.py",
  [int]$ServerPort = 8790,
  [int]$ProxyPort = 8792
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path (Split-Path -Parent $here) ".env"
$tokenLine = Get-Content $envFile | Where-Object { $_ -match '^MCP_PUBLIC_TOKEN=' } | Select-Object -First 1
$token = ($tokenLine -replace '^MCP_PUBLIC_TOKEN=', '').Trim()
if (-not $token) { throw "MCP_PUBLIC_TOKEN не задан в $envFile" }

$logDir = Join-Path $env:TEMP "mcp-public"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 1. MCP-сервер в HTTP-режиме на loopback (env-переменная наследуется дочерним)
$env:MCP_HTTP_PORT = "$ServerPort"
$srv = Start-Process -FilePath "cmd.exe" -ArgumentList "/c $ServerCommand" `
  -WorkingDirectory $ServerCwd -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "$logDir\server.out" -RedirectStandardError "$logDir\server.err"

# 2. bearer-прокси loopback -> loopback
$env:MCP_PUBLIC_TOKEN = $token
$env:PROXY_PORT = "$ProxyPort"
$env:TARGET_PORT = "$ServerPort"
$proxy = Start-Process -FilePath "python" -ArgumentList "`"$here\auth-proxy.py`"" `
  -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "$logDir\proxy.out" -RedirectStandardError "$logDir\proxy.err"

"$($srv.Id) $($proxy.Id)" | Out-File "$logDir\pids.txt"
"server pid=$($srv.Id) :$ServerPort | auth-proxy pid=$($proxy.Id) :$ProxyPort (bearer из .env)"
"логи: $logDir\"
