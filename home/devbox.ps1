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
              'SELF_AUTHED_PORTS','ALLOWED_PORTS','OPENCODE_MCP_PERMISSIONS',
              'SETUP_SCRIPT_B64','PUBLIC_URL','ACTIVE_PROFILE','MCP_BEARER_TOKEN',
              'MCP_PUBLIC_TOKEN','INGRESS_TOKEN','VLESS_SUB_URL',
              'JOB_TIMEOUT_SECONDS','TUNNEL_TOKEN','TUNNEL_TAIL')

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
    $cmdline = ($p.CommandLine -replace '\s+', ' ')
    if ($marker -and $cmdline -match [regex]::Escape(($marker -replace '\s+', ' '))) {
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
    $marker = ($svc.Cmd -replace '\s+', ' ')
    if ($svc.Auth -eq 'bearer') {
      $inner = [int]$svc.Port + 1
      $env:MCP_HTTP_PORT = "$inner"
      $c = Start-Process -FilePath 'cmd.exe' -ArgumentList "/c $($svc.Cmd)" `
        -WorkingDirectory $svc.Cwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$profileName-svc$($svc.Port).out") `
        -RedirectStandardError (Join-Path $logRoot "$profileName-svc$($svc.Port).err")
      # pid wrappers умирает вместе с pwsh; фиксируем реальный pid сервиса:
      # владелец LISTEN-сокета внутреннего порта
      Start-Sleep -Seconds 3
      $lst = Get-NetTCPConnection -LocalPort $inner -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
      $svcPid = if ($lst) { $lst.OwningProcess } else { $c.Id }
      $pids += "$svcPid|$marker"
      $env:MCP_PUBLIC_TOKEN = $publicToken
      $env:PROXY_PORT = "$($svc.Port)"
      $env:TARGET_PORT = "$inner"
      $a = Start-Process -FilePath 'python' -ArgumentList "`"$hostDir\auth-proxy.py`"" `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$profileName-auth$($svc.Port).out") `
        -RedirectStandardError (Join-Path $logRoot "$profileName-auth$($svc.Port).err")
      Start-Sleep -Seconds 2
      $lstA = Get-NetTCPConnection -LocalPort $svc.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
      $authPid = if ($lstA) { $lstA.OwningProcess } else { $a.Id }
      $pids += "$authPid|auth-proxy.py"
      "start-host: bearer-сервис :$($svc.Port) -> :$inner (pid $svcPid, auth pid $authPid)"
    } else {
      $c = Start-Process -FilePath 'cmd.exe' -ArgumentList "/c $($svc.Cmd)" `
        -WorkingDirectory $svc.Cwd -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logRoot "$profileName-svc$($svc.Port).out") `
        -RedirectStandardError (Join-Path $logRoot "$profileName-svc$($svc.Port).err")
      Start-Sleep -Seconds 3
      $lst = Get-NetTCPConnection -LocalPort $svc.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
      $svcPid = if ($lst) { $lst.OwningProcess } else { $c.Id }
      $pids += "$svcPid|$marker"
      "start-host: сервис :$($svc.Port) (pid $svcPid)"
    }
  }
  $pids | Set-Content (Join-Path $logRoot "$profileName-pids.txt")
}

function Test-Profile {
  param($ProjectDir, $Toolchain, $GitName, $GitEmail, $HostServices,
        $RunnerCommands, $RunnerPort, $AllowedPorts, $DenyMounts, $SetupCmds, $PreviewOrigin)
  $v = @()
  if (-not $ProjectDir) { $v += 'R1: задай $ProjectDir' }
  elseif (-not (Test-Path $ProjectDir)) { $v += "R2: папка проекта $ProjectDir не существует" }
  if ($Toolchain -isnot [string]) { $v += 'R3: $Toolchain должен быть строкой' }
  if (-not $GitName) { $v += 'R4: задай $GitName' }
  if (-not $GitEmail -or $GitEmail -notmatch '@') { $v += 'R5: задай $GitEmail (с @)' }
  $ports = @()
  $i = 0
  foreach ($s in $HostServices) {
    $i++
    if (-not $s.Port -or $s.Port -lt 1 -or $s.Port -gt 65535) { $v += "R6: HostService[$i]: Port 1-65535 обязателен" }
    else { $ports += [int]$s.Port }
    if (-not $s.Cmd) { $v += "R6: HostService[$i]: Cmd непустой" }
  }
  if (($ports | Group-Object | Where-Object Count -gt 1)) { $v += 'R7: дубликат порта в HostServices' }
  $names = @()
  $i = 0
  $metachars = '[|&;`$()<>%!' + '"' + "'" + ']'
  foreach ($c in $RunnerCommands) {
    $i++
    if (-not $c.Name) { $v += "R8: RunnerCommand[$i]: Name обязателен" }
    else { $names += $c.Name }
    if (-not $c.Cmd -or $c.Cmd.Count -eq 0) { $v += "R8: RunnerCommand[$i]: Cmd непустой массив" }
    else { foreach ($el in $c.Cmd) { if ($el -match $metachars) { $v += "R9: Cmd элемент '$el' содержит shell-символ" } } }
  }
  if (($names | Group-Object | Where-Object Count -gt 1)) { $v += 'R10: дубликат имени в RunnerCommands' }
  if ($RunnerCommands -and $RunnerCommands.Count -and (-not $RunnerPort -or $RunnerPort -lt 1 -or $RunnerPort -gt 65535)) { $v += 'R11: $RunnerPort 1-65535' }
  $ap = @()
  foreach ($p in $AllowedPorts) { if ($p -lt 1 -or $p -gt 65535) { $v += "R12: порт $p вне 1-65535" } else { $ap += [int]$p } }
  if (($ap | Group-Object | Where-Object Count -gt 1)) { $v += 'R12: дубликат в AllowedPorts' }
  foreach ($p in $ports) { if ($ap -contains $p) { $v += "R13: порт $p и в HostServices, и в AllowedPorts" } }
  if ($RunnerPort) {
    if ($ap -contains $RunnerPort) { $v += "R14: RunnerPort $p в AllowedPorts" }
    if ($ports -contains $RunnerPort) { $v += "R15: RunnerPort $p в HostServices" }
  }
  foreach ($d in $DenyMounts) { if (-not (Test-Path (Join-Path $ProjectDir $d))) { $v += "WARN R16: DenyMounts $ProjectDir/$d не существует (ничего не денится)" } }
  $i = 0
  foreach ($s in $SetupCmds) { $i++; if (-not $s.Cmd) { $v += "R17: SetupCmd[$i]: Cmd обязателен" } }
  if (-not $PreviewOrigin) { $v += 'WARN R19: $PreviewOrigin не задан — preview-туннель не поднимется' }
  $v
}

function Get-IngressUrl {
  $map = Read-Env $envFile
  if ($map['PUBLIC_URL']) { return $map['PUBLIC_URL'] }
  $logs = docker compose -f (Join-Path $here 'docker-compose.yml') logs cloudflared-ingress 2>$null |
    Select-String 'https://[a-z0-9-]+\.trycloudflare\.com' |
    ForEach-Object { $_.Matches[0].Value } | Select-Object -Last 1
  $logs
}

function Get-ChatBlock($map, [bool]$full) {
  $url = Get-IngressUrl
  $mask = { param($t) if ($full) { $t } else { "$($t.Substring(0,4))...$($t.Substring($t.Length-4))" } }
  $lines = @()
  $lines += 'Работай по инструкции: https://github.com/WhiteBite/remote-devbox-mcp/blob/main/ARENA.md'
  $lines += "INGRESS=$url"
  $lines += "BRIDGE_TOKEN=$(& $mask $map['MCP_BEARER_TOKEN'])"
  $lines += "HOST_TOKEN=$(& $mask $map['MCP_PUBLIC_TOKEN'])"
  $lines += "INGRESS_TOKEN=$(& $mask $map['INGRESS_TOKEN'])"
  $lines += 'Эндпоинты и порты: GET <INGRESS>/p/9000/manifest.json (Bearer INGRESS_TOKEN)'
  $lines += 'ТЗ: /workspace/ARENA_TASK.md'
  $lines -join "`n"
}

function Write-Manifest($Name, $map, $hostSvcs, $RunnerCommands, $AllowedPorts) {
  $eps = @(@{ name = 'bridge'; port = 8787; auth = 'bearer'; path = '/p/8787/mcp' })
  foreach ($s in $hostSvcs) { $eps += @{ name = "host-$($s.Port)"; port = [int]$s.Port; auth = 'bearer'; path = "/p/$($s.Port)/mcp" } }
  foreach ($p in $AllowedPorts) { $eps += @{ name = "allowed-$p"; port = [int]$p; auth = 'ingress'; path = "/p/$p" } }
  $manifest = @{
    profile = $Name
    project = $map['PROJECT_DIR']
    ingress_url = (Get-IngressUrl)
    endpoints = $eps
    allowed_ports = @($AllowedPorts)
    runner_commands = @($RunnerCommands | ForEach-Object { $_.Name })
    scripts = @($Scripts | ForEach-Object { $_.Name })
    preview_origin = $map['PREVIEW_ORIGIN']
    mode = $map['OPENCODE_MCP_PERMISSIONS'] ? 'custom' : 'standard'
    host_requirements = @{ memory_mb = 4096; storage_mb = 20480; note = 'toolchains in /opt/tools volume' }
  }
  $manifest | ConvertTo-Json -Depth 6 |
    Set-Content (Join-Path $logRoot 'rdm-manifest.json') -Encoding utf8
}

switch ($Cmd) {
  'use' {
    if (-not $Name) { throw 'usage: devbox.ps1 use <имя>' }
    $profileFile = Join-Path (Split-Path -Parent $here) "projects\$Name.ps1"
    if (-not (Test-Path $profileFile)) { throw "нет профиля $profileFile" }
    . $profileFile
    $viol = @(Test-Profile -ProjectDir $ProjectDir -Toolchain $Toolchain -GitName $GitName `
      -GitEmail $GitEmail -HostServices $HostServices -RunnerCommands $RunnerCommands `
      -RunnerPort $RunnerPort -AllowedPorts $AllowedPorts -DenyMounts $DenyMounts `
      -SetupCmds $SetupCmds -PreviewOrigin $PreviewOrigin)
    $perrs = @($viol | Where-Object { $_ -notmatch '^WARN' })
    if ($perrs) { $perrs | ForEach-Object { "profile error: $_" }; exit 1 }
    $viol | Where-Object { $_ -match '^WARN' } | ForEach-Object { "profile warn: $($_ -replace '^WARN ','')" }
    $map = Read-Env $envFile
    $old = if ($map.ContainsKey('ACTIVE_PROFILE')) { $map['ACTIVE_PROFILE'] } else { '' }
    if ($old) { Stop-HostServices $old }
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
    $mode = if ($Mode) { $Mode } else { 'standard' }
    $perm = switch ($mode) {
      'readonly' { '{"write":"deny","edit":"deny","apply_patch":"deny","bash":"deny"}' }
      'full'     { '{"write":"allow","edit":"allow","apply_patch":"allow","bash":"allow"}' }
      default    { '' }
    }
    if ($perm) { $map['OPENCODE_MCP_PERMISSIONS'] = $perm }
    else { $map.Remove('OPENCODE_MCP_PERMISSIONS') }
    Write-Env $envFile $map
    Write-Manifest $Name $map $hostSvcs $RunnerCommands $allowed

    # refs тулчейнов для registry.json (refcount по профилям)
    docker run --rm -v rdm-tools:/opt/tools alpine sh -c "mkdir -p /opt/tools/refs && echo '$Toolchain' > /opt/tools/refs/$Name.list" 2>$null | Out-Null

    # AGENTS.md-сниппет из профиля в /agent (agent-consumable manifest)
    $rcLines = ($RunnerCommands | ForEach-Object { "- run_$($_.Name -replace '[:\-]','_'): $($_.Description)" }) -join "`n"
    if (-not $rcLines) { $rcLines = '- (нет)' }
    $scLines = ($Scripts | ForEach-Object { "- run_script_$($_.Name): $($_.Description)" }) -join "`n"
    if (-not $scLines) { $scLines = '- (нет)' }
    $agentsMd = @"
<!-- auto-generated: devbox.ps1 use $Name -->
## Environment
- profile: $Name; project: $ProjectDir
- toolchain: $Toolchain
- mode: $mode; allowed ports: $($map['ALLOWED_PORTS'])
## Runner commands
$rcLines
## Scripts
$scLines
"@
    $agentsMd | docker run --rm -i -v rdm-agent:/agent alpine sh -c "cat > /agent/AGENTS.md" 2>$null | Out-Null

    # gitleaks pre-mount скан секретов проекта в фоне (SKIP_GITLEAKS=1 отключает);
    # отчёт читает doctor
    if (-not $env:SKIP_GITLEAKS) {
      $glReport = Join-Path $logRoot "gitleaks-$Name.json"
      Remove-Item $glReport -ErrorAction SilentlyContinue
      Start-Process -FilePath docker -ArgumentList @(
        'run', '--rm',
        "-v", "$($ProjectDir.Replace('\', '/')):/src:ro",
        '-v', "$($here.Replace('\', '/'))/docker/gitleaks.toml:/cfg.toml:ro",
        '-v', "$($logRoot.Replace('\', '/')):/out",
        'zricethezav/gitleaks', 'detect', '--source', '/src', '--no-git',
        '--config', '/cfg.toml', '--report-format', 'json',
        '--report-path', "/out/gitleaks-$Name.json", '--exit-code', '0'
      ) -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logRoot "gitleaks-$Name.out") `
        -RedirectStandardError (Join-Path $logRoot "gitleaks-$Name.err") | Out-Null
      "gitleaks: скан секретов запущен в фоне: $glReport"
    }

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
    $setupDir = Join-Path $here 'docker\setup'
    $setupB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($setup))
    $map['SETUP_SCRIPT_B64'] = $setupB64

    # именованный туннель (стабильный URL) vs быстрый trycloudflare — по TUNNEL_TOKEN
    if ($map['TUNNEL_TOKEN']) { $map['TUNNEL_TAIL'] = "run --token $($map['TUNNEL_TOKEN'])" }
    else { $map['TUNNEL_TAIL'] = '--protocol http2 --url http://host.docker.internal:8799' }

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
  'doctor' {
    $map = Read-Env $envFile
    $fail = 0
    function Check($name, $cond, $hint) {
      if ($cond) { "[PASS] $name" } else { $script:fail = 1; "[FAIL] $name — $hint" }
    }
    Check 'docker engine' ($(docker info --format '{{.ServerVersion}}' 2>$null | Out-Null; $LASTEXITCODE -eq 0)) 'Docker Desktop не запущен'
    $keysOk = ($map['MCP_BEARER_TOKEN'] -and $map['MCP_PUBLIC_TOKEN'] -and $map['INGRESS_TOKEN'] -and $map['PROJECT_DIR'])
    Check '.env keys' $keysOk 'заполни токены и PROJECT_DIR в home/.env'
    $lenOk = (@($map['MCP_BEARER_TOKEN'], $map['MCP_PUBLIC_TOKEN'], $map['INGRESS_TOKEN']) |
      Where-Object { $_ -and $_.Length -ge 24 }).Count -eq 3
    Check 'token lengths >=24' $lenOk 'токен короче 24 символов'
    $prof = $map['ACTIVE_PROFILE']
    $profileFile = Join-Path (Split-Path -Parent $here) "projects\$prof.ps1"
    Check 'profile exists' ($prof -and (Test-Path $profileFile)) 'devbox.ps1 use <имя>'
    if ($prof -and (Test-Path $profileFile)) {
      . $profileFile
      $viol = @(Test-Profile -ProjectDir $ProjectDir -Toolchain $Toolchain -GitName $GitName `
        -GitEmail $GitEmail -HostServices $HostServices -RunnerCommands $RunnerCommands `
        -RunnerPort $RunnerPort -AllowedPorts $AllowedPorts -DenyMounts $DenyMounts `
        -SetupCmds $SetupCmds -PreviewOrigin $PreviewOrigin | Where-Object { $_ -notmatch '^WARN' })
      Check 'profile validation' ($viol.Count -eq 0) ($viol -join '; ')
    }
    Check 'override yml' (Test-Path (Join-Path $here 'docker-compose.override.yml')) 'devbox.ps1 use <имя>'
    $tb = docker compose -f (Join-Path $here 'docker-compose.yml') ps toolbox --format '{{.Status}}' 2>$null
    Check 'toolbox healthy' ($tb -match 'healthy') 'docker compose up -d toolbox'
    if ($map['VLESS_SUB_URL']) {
      $vp = docker compose -f (Join-Path $here 'docker-compose.yml') ps vpn --format '{{.Status}}' 2>$null
      Check 'vpn healthy' ($vp -match 'healthy') 'docker compose logs vpn'
    }
    $ipid = (Get-Content (Join-Path $env:TEMP 'rdm-ingress\pids.txt') -ErrorAction SilentlyContinue |
      Select-Object -First 1)
    $iproc = if ($ipid) { Get-CimInstance Win32_Process -Filter "ProcessId=$ipid" -ErrorAction SilentlyContinue } else { $null }
    Check 'ingress pid' ($iproc -and $iproc.CommandLine -match 'ingress-proxy\.py') 'host\start-ingress.ps1'
    $listen = Get-NetTCPConnection -LocalPort 8799 -State Listen -ErrorAction SilentlyContinue
    Check 'ingress listen 8799' ($listen -ne $null) 'перезапусти host\start-ingress.ps1'
    $url = Get-IngressUrl
    Check 'ingress url' ($url -ne $null) 'docker compose logs cloudflared-ingress'
    if ($url) {
      $code = curl.exe -s -m 10 -o NUL -w '%{http_code}' -H "Authorization: Bearer $($map['MCP_BEARER_TOKEN'])" "$url/p/8787/healthz"
      Check 'bridge via ingress 200' ($code -eq '200') 'docker compose logs toolbox'
      $code = curl.exe -s -m 10 -o NUL -w '%{http_code}' -X POST `
        -H "Authorization: Bearer wrong-token-wrong-token" `
        -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" `
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' "$url/p/8787/mcp"
      Check 'ingress auth 401' ($code -eq '401') 'проверь MCP_BEARER_TOKEN/INGRESS_TOKEN'
      $code = curl.exe -s -m 10 -o NUL -w '%{http_code}' -H "Authorization: Bearer $($map['INGRESS_TOKEN'])" "$url/p/1/"
      Check 'allowlist 403' ($code -eq '403') 'проверь ALLOWED_PORTS'
    }
    $pf = Join-Path $logRoot "$prof-pids.txt"
    if (Test-Path $pf) {
      $dead = 0
      Get-Content $pf | ForEach-Object { $id, $marker = $_ -split '\|', 2
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        $cmdline = if ($p) { ($p.CommandLine -replace '\s+', ' ') } else { '' }
        if (-not $p -or ($marker -and $cmdline -notmatch [regex]::Escape(($marker -replace '\s+', ' ')))) { $dead++ } }
      Check 'host services alive' ($dead -eq 0) 'devbox.ps1 use <имя> перезапустит'
    }
    $glReport = Join-Path $logRoot "gitleaks-$prof.json"
    if (Test-Path $glReport) {
      $gl = @(Get-Content $glReport -Raw | ConvertFrom-Json | Where-Object { $_ })
      if ($gl.Count) { "[WARN] gitleaks: найдено секретов $($gl.Count) — проверь DenyMounts" }
      else { '[PASS] gitleaks clean' }
    } else {
      '[SKIP] gitleaks отчёт ещё не готов (фоновый скан)'
    }
    if ($fail) { exit 1 } else { 'doctor: all PASS'; exit 0 }
  }
  'watch' {
    $map = Read-Env $envFile
    $wl = Join-Path $env:TEMP 'rdm-watchdog\watchdog.log'
    New-Item -ItemType Directory -Force -Path (Split-Path $wl) | Out-Null
    $fails = 0; $healed = 0
    while ($true) {
      Start-Sleep -Seconds 15
      $url = Get-IngressUrl
      $code = if ($url) { curl.exe -s -m 10 -o NUL -w '%{http_code}' -H "Authorization: Bearer $($map['MCP_BEARER_TOKEN'])" "$url/p/8787/healthz" } else { '000' }
      if ($code -ne '200') { $fails++ } else { $fails = 0 }
      if ($fails -ge 3) {
        "$(Get-Date -Format o) tunnel flap: recreate ingress" | Add-Content $wl
        docker compose -f (Join-Path $here 'docker-compose.yml') up -d --force-recreate cloudflared-ingress | Out-Null
        $healed++; $fails = 0; Start-Sleep -Seconds 30
      }
      $prof = $map['ACTIVE_PROFILE']
      $pf = Join-Path $logRoot "$prof-pids.txt"
      if (Test-Path $pf) {
        $dead = $false
        Get-Content $pf | ForEach-Object { $id, $marker = $_ -split '\|', 2
          $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
          $cmdline = if ($p) { ($p.CommandLine -replace '\s+', ' ') } else { '' }
          if (-not $p -or ($marker -and $cmdline -notmatch [regex]::Escape(($marker -replace '\s+', ' ')))) { $dead = $true } }
        if ($dead) {
          "$(Get-Date -Format o) host services dead: restart" | Add-Content $wl
          $profileFile = Join-Path (Split-Path -Parent $here) "projects\$prof.ps1"
          if (Test-Path $profileFile) {
            . $profileFile
            Stop-HostServices $prof
            $hostSvcs = @() + $HostServices
            if ($RunnerCommands -and $RunnerCommands.Count) {
              $rPort = if ($RunnerPort) { $RunnerPort } else { 8796 }
              $env:RUNNER_CONFIG = Join-Path $logRoot "runner-$prof.json"
              $env:RUNNER_PORT = "$($rPort + 1)"
              $hostSvcs += @{ Port = $rPort; Auth = 'bearer'; Cwd = $here; Cmd = "python host\runner-mcp.py" }
            }
            Start-HostServices $prof $hostSvcs $map['MCP_PUBLIC_TOKEN']
            $healed++
          }
        }
      }
      $tb = docker compose -f (Join-Path $here 'docker-compose.yml') ps toolbox --format '{{.Status}}' 2>$null
      if ($tb -notmatch 'healthy') {
        "$(Get-Date -Format o) toolbox unhealthy: up -d" | Add-Content $wl
        docker compose -f (Join-Path $here 'docker-compose.yml') up -d toolbox | Out-Null
        $healed++
      }
    }
  }
  'info' {
    $map = Read-Env $envFile
    "Профиль:    $($map['ACTIVE_PROFILE'])"
    "Проект:     $($map['PROJECT_DIR'])"
    "Тулчейны:   $($map['TOOLCHAIN'])"
    "Git:        $($map['GIT_NAME']) <$($map['GIT_EMAIL'])>"
    "INGRESS:    $(Get-IngressUrl)"
    "Порты:      self-authed $($map['SELF_AUTHED_PORTS']); allowed $($map['ALLOWED_PORTS'])"
    "Preview:    $($map['PREVIEW_ORIGIN'])"
    '--- чат-блок (маскированный) ---'
    Get-ChatBlock $map $false
  }
  'share' {
    $map = Read-Env $envFile
    Get-ChatBlock $map $true
  }
  'issue-tokens' {
    $map = Read-Env $envFile
    foreach ($k in @('MCP_BEARER_TOKEN','MCP_PUBLIC_TOKEN','INGRESS_TOKEN')) {
      $map[$k] = -join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
    }
    Write-Env $envFile $map
    docker compose -f (Join-Path $here 'docker-compose.yml') up -d --force-recreate toolbox | Out-Null
    $prof = $map['ACTIVE_PROFILE']
    if ($prof) {
      $profileFile = Join-Path (Split-Path -Parent $here) "projects\$prof.ps1"
      if (Test-Path $profileFile) {
        . $profileFile
        Stop-HostServices $prof
        $hostSvcs = @() + $HostServices
        if ($RunnerCommands -and $RunnerCommands.Count) {
          $rPort = if ($RunnerPort) { $RunnerPort } else { 8796 }
          $env:RUNNER_CONFIG = Join-Path $logRoot "runner-$prof.json"
          $env:RUNNER_PORT = "$($rPort + 1)"
          $hostSvcs += @{ Port = $rPort; Auth = 'bearer'; Cwd = $here; Cmd = "python host\runner-mcp.py" }
        }
        Start-HostServices $prof $hostSvcs $map['MCP_PUBLIC_TOKEN']
      }
    }
    & "$hostDir\stop-ingress.ps1" | Out-Null
    & "$hostDir\start-ingress.ps1" | Out-Null
    Get-ChatBlock $map $true
  }
  default {
    docker compose -f (Join-Path $here 'docker-compose.yml') ps --format '{{.Name}} {{.Status}}'
    $map = Read-Env $envFile
    "active profile: $($map['ACTIVE_PROFILE'])"
    $pf = Join-Path $logRoot "$($map['ACTIVE_PROFILE'])-pids.txt"
    if (Test-Path $pf) { Get-Content $pf | ForEach-Object { "host pid: $_" } }
  }
}
