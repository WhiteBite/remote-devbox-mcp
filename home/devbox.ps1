# devbox.ps1 — управление проект-независимым devbox.
#
#   devbox.ps1 use <имя>   применить профиль projects/<имя>.ps1:
#                          рендер .env, host-сервисы, пересоздание toolbox
#   devbox.ps1 status      контейнеры + host-сервисы активного профиля
#   devbox.ps1 stop-host   остановить host-сервисы активного профиля
#
# Профиль проекта = projects/<имя>.ps1 (см. _template.ps1). Muffin/MidasAI/любой
# другой — просто файл профиля; особого кода под проект нет.
param([string]$Cmd = 'status', [string]$Name = '')

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $here '.env'
$hostDir = Join-Path $here 'host'
$logRoot = Join-Path $env:TEMP 'rdm-host'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null

$EnvOrder = @('PROJECT_DIR','TOOLCHAIN','GIT_NAME','GIT_EMAIL','PREVIEW_ORIGIN',
              'SELF_AUTHED_PORTS','ALLOWED_PORTS','ACTIVE_PROFILE','MCP_BEARER_TOKEN',
              'MCP_PUBLIC_TOKEN','INGRESS_TOKEN','VLESS_SUB_URL',
              'JOB_TIMEOUT_SECONDS','TUNNEL_TOKEN')

function Read-Env($path) {
  $map = @{}
  if (Test-Path $path) {
    Get-Content $path | ForEach-Object {
      $m = [regex]::Match($_, '^\s*([A-Za-z0-9_]+)=(.*)$')
      if ($m.Success) { $map[$m.Groups[1].Value] = $m.Groups[2].Value.Trim() }
    }
  }
  $map
}

function Write-Env($path, $map) {
  $lines = @()
  foreach ($k in $EnvOrder) { if ($map.ContainsKey($k)) { $lines += "$k=$($map[$k])" } }
  foreach ($k in ($map.Keys | Sort-Object)) { if ($EnvOrder -notcontains $k) { $lines += "$k=$($map[$k])" } }
  Set-Content -Path $path -Value ($lines -join "`n")
}

function Stop-HostServices($profileName) {
  $pf = Join-Path $logRoot "$profileName-pids.txt"
  if (-not (Test-Path $pf)) { return }
  Get-Content $pf | ForEach-Object {
    $id, $marker = $_ -split '\|', 2
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
    if (-not $p) { return }
    if ($marker -and $p.CommandLine -match [regex]::Escape($marker)) {
      taskkill /F /T /PID $id | Out-Null
      "stop-host: pid $id ($marker)"
    } else {
      "stop-host: pid $id не наш (cmdline изменился) — не трогаю"
    }
  }
  Remove-Item $pf -ErrorAction SilentlyContinue
}

function Start-HostServices($profileName, $services, $publicToken) {
  $pids = @()
  foreach ($svc in $services) {
    $marker = ($svc.Cmd -split ' ')[0..2] -join ' '
    if ($svc.Auth -eq 'bearer') {
      $inner = [int]$svc.Port + 1
      $env:MCP_HTTP_PORT = "$inner"
      $c = Start-Process -FilePath 'cmd.exe' -ArgumentList "/c $($svc.Cmd)" `
        -WorkingDirectory $svc.Cwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$profileName-svc$($svc.Port).out") `
        -RedirectStandardError (Join-Path $logRoot "$profileName-svc$($svc.Port).err")
      $pids += "$($c.Id)|$marker"
      $env:MCP_PUBLIC_TOKEN = $publicToken
      $env:PROXY_PORT = "$($svc.Port)"
      $env:TARGET_PORT = "$inner"
      $a = Start-Process -FilePath 'python' -ArgumentList "`"$hostDir\auth-proxy.py`"" `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$profileName-auth$($svc.Port).out") `
        -RedirectStandardError (Join-Path $logRoot "$profileName-auth$($svc.Port).err")
      $pids += "$($a.Id)|auth-proxy.py"
      "start-host: bearer-сервис :$($svc.Port) -> :$inner (pid $($c.Id), auth pid $($a.Id))"
    } else {
      $c = Start-Process -FilePath 'cmd.exe' -ArgumentList "/c $($svc.Cmd)" `
        -WorkingDirectory $svc.Cwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$profileName-svc$($svc.Port).out") `
        -RedirectStandardError (Join-Path $logRoot "$profileName-svc$($svc.Port).err")
      $pids += "$($c.Id)|$marker"
      "start-host: сервис :$($svc.Port) (pid $($c.Id))"
    }
  }
  $pids | Set-Content (Join-Path $logRoot "$profileName-pids.txt")
}

switch ($Cmd) {
  'use' {
    if (-not $Name) { throw 'usage: devbox.ps1 use <имя>' }
    $profileFile = Join-Path (Split-Path -Parent $here) "projects\$Name.ps1"
    if (-not (Test-Path $profileFile)) { throw "нет профиля $profileFile" }
    . $profileFile
    $map = Read-Env $envFile
    $old = if ($map.ContainsKey('ACTIVE_PROFILE')) { $map['ACTIVE_PROFILE'] } else { '' }
    if ($old -and $old -ne $Name) { Stop-HostServices $old }
    $map['PROJECT_DIR'] = $ProjectDir
    $map['TOOLCHAIN'] = $Toolchain
    $map['GIT_NAME'] = $GitName
    $map['GIT_EMAIL'] = $GitEmail
    if ($PreviewOrigin) { $map['PREVIEW_ORIGIN'] = $PreviewOrigin } else { $map.Remove('PREVIEW_ORIGIN') }
    $bearerPorts = @($HostServices | Where-Object { $_.Auth -eq 'bearer' } | ForEach-Object { $_.Port })

    # runner-mcp: хостовые команды проекта (argv-модель, без shell)
    $hostSvcs = @() + $HostServices
    if ($RunnerCommands -and $RunnerCommands.Count) {
        $rPort = if ($RunnerPort) { $RunnerPort } else { 8796 }
        $runnerCfg = Join-Path $logRoot "runner-$Name.json"
        @{ profile = $Name; cwd = $ProjectDir; commands = $RunnerCommands } |
            ConvertTo-Json -Depth 8 | Set-Content $runnerCfg -Encoding utf8
        $env:RUNNER_CONFIG = $runnerCfg
        $env:RUNNER_PORT = "$($rPort + 1)"
        $hostSvcs += @{ Port = $rPort; Auth = 'bearer'; Cwd = $here; Cmd = "python host\runner-mcp.py" }
        $bearerPorts += $rPort
        "runner-mcp: порт $rPort (inner $($rPort+1)), команд: $($RunnerCommands.Count)"
    }
    $map['SELF_AUTHED_PORTS'] = (@('8787') + $bearerPorts | Select-Object -Unique) -join ','

    # fail-closed allowlist портов ingress: профиль + небearer-сервисы + background-порты
    $allowed = @()
    if ($AllowedPorts) { $allowed += $AllowedPorts }
    $allowed += @($HostServices | Where-Object { $_.Auth -ne 'bearer' } | ForEach-Object { $_.Port })
    $allowed += @($RunnerCommands | Where-Object { $_.Port } | ForEach-Object { $_.Port })
    $map['ALLOWED_PORTS'] = (@($allowed | Select-Object -Unique) -join ',')

    $map['ACTIVE_PROFILE'] = $Name
    Write-Env $envFile $map

    # override-compose: deny-монты секретов + тень .opencode/ (empty dir)
    $ov = "services:`n  toolbox:`n    volumes:`n      - ./docker/workspace-empty:/workspace/.opencode:ro`n"
    foreach ($d in $DenyMounts) {
        $ov += "      - /dev/null:/workspace/" + $d + ":ro`n"
    }
    [IO.File]::WriteAllText((Join-Path $here 'docker-compose.override.yml'), $ov)

    # setup-project.sh: пост-установочные команды профиля с маркерами (LF!)
    $setup = "# generated: devbox.ps1 use $Name`n"
    $i = 0
    foreach ($s in $SetupCmds) {
        $i++
        $h = ([BitConverter]::ToString([System.Security.Cryptography.MD5]::Create().ComputeHash(
              [Text.Encoding]::UTF8.GetBytes($s.Cmd + $s.Marker + $Name))) -replace '-', '').ToLower()
        $onfail = if ($s.Required) { 'exit 1' } else { "echo '[setup] WARN: cmd $i failed, continue'" }
        $setup += "if [ ! -f /opt/tools/.setup-$i-$h ]; then`n  " + $s.Cmd + " || " + $onfail + "`n  touch /opt/tools/.setup-$i-$h`nfi`n"
    }
    [IO.File]::WriteAllText((Join-Path $here 'docker\setup-project.sh'), $setup)

    Start-HostServices $Name $hostSvcs $map['MCP_PUBLIC_TOKEN']
    # ingress-proxy перечитывает SELF_AUTHED_PORTS/ALLOWED_PORTS только при старте
    & "$hostDir\stop-ingress.ps1" | Out-Null
    & "$hostDir\start-ingress.ps1" | Out-Null
    docker compose -f (Join-Path $here 'docker-compose.yml') up -d --force-recreate toolbox
    "профиль $Name применён; тулчейны ставятся при старте toolbox (кэш в rdm-tools)"
  }
  'stop-host' {
    $map = Read-Env $envFile
    if ($map.ContainsKey('ACTIVE_PROFILE')) { Stop-HostServices $map['ACTIVE_PROFILE'] }
    else { 'ACTIVE_PROFILE не задан' }
  }
  default {
    docker compose -f (Join-Path $here 'docker-compose.yml') ps --format '{{.Name}} {{.Status}}'
    $map = Read-Env $envFile
    "active profile: $($map['ACTIVE_PROFILE'])"
    $pf = Join-Path $logRoot "$($map['ACTIVE_PROFILE'])-pids.txt"
    if (Test-Path $pf) { Get-Content $pf | ForEach-Object { "host pid: $_" } }
  }
}
