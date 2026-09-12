# stop-ingress.ps1 — остановка ingress-proxy: только записанный pid,
# перед kill сверяем cmdline (pid мог быть переиспользован ОС).
$logDir = Join-Path $env:TEMP "rdm-ingress"
$pidsFile = Join-Path $logDir "pids.txt"
if (-not (Test-Path $pidsFile)) {
  "нет $pidsFile — ingress не поднимался start-ingress.ps1"
  exit 0
}
foreach ($id in ((Get-Content $pidsFile) -split '\s+' | Where-Object { $_ -match '^\d+$' })) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
  if (-not $p) { "pid $id уже мёртв"; continue }
  if ($p.CommandLine -match 'ingress-proxy\.py') {
    taskkill /F /T /PID $id | Out-Null
    "убит pid $id"
  } else {
    "pid $id НЕ ingress-proxy — не трогаю"
  }
}
Remove-Item $pidsFile -ErrorAction SilentlyContinue
