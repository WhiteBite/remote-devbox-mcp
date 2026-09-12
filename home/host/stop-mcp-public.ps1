# stop-mcp-public.ps1 — штатная остановка цепочки mcp (см. start-mcp-public.ps1).
#
# Убивает ТОЛЬКО pid, записанные при старте, и перед kill сверяет cmdline:
# pid мог быть переиспользован ОС под чужой процесс. Убийство по паттерну
# cmdline запрещено: на этой машине легально живут чужие python/node-серверы.
$logDir = Join-Path $env:TEMP "mcp-public"
$pidsFile = Join-Path $logDir "pids.txt"
if (-not (Test-Path $pidsFile)) {
  "нет $pidsFile — цепочка не поднималась start-mcp-public.ps1"
  exit 0
}
$ids = (Get-Content $pidsFile) -split '\s+' | Where-Object { $_ -match '^\d+$' }
foreach ($id in $ids) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
  if (-not $p) { "pid $id уже мёртв"; continue }
  $cmd = $p.CommandLine
  $short = $cmd.Substring(0, [Math]::Min(70, $cmd.Length))
  if ($cmd -match 'auth-proxy\.py' -or $cmd -match 'muffin-supervisor') {
    # /T — деревом: pid обёртки cmd /c без этого оставил бы ребёнка-сироту
    taskkill /F /T /PID $id | Out-Null
    "убит pid $id деревом ($short...)"
  } else {
    "pid $id НЕ из нашей цепочки ($short...) — не трогаю"
  }
}
Remove-Item $pidsFile -ErrorAction SilentlyContinue
"остановка завершена; туннель: docker compose stop cloudflared-mcp"
