# Профиль MidasAI: Windows-native monorepo (pnpm/node24 + python3.12 venv внутри
# репо) => сборки/тесты ТОЛЬКО на хосте через runner-mcp; контейнер = редактор
# (read/write/edit/glob/grep + git), bash моста без node_modules не использует.
$ProjectDir = 'd:/Sources/StartUp/MidasAI'
$Toolchain = ''
$GitName = 'WhiteBite'
$GitEmail = 'ad.lord9000@yandex.ru'
# web (Vite) живёт на хосте: FRONTEND_PORT из ports.env
$PreviewOrigin = 'http://host.docker.internal:35204'

# Хостовые команды через runner-mcp (argv-модель, без shell). read-only проверки
# по умолчанию; мутации (dev:start/dev:stop) объявлены явно.
$RunnerCommands = @(
  @{ Name = 'check:layers';  Cmd = @('npm', 'run', 'check:layers');  Description = 'arch-layers gate (js+py)' }
  @{ Name = 'check:parity';  Cmd = @('npm', 'run', 'check:parity');  Description = 'bdd/parity baseline gate' }
  @{ Name = 'check:rpc';     Cmd = @('npm', 'run', 'check:rpc-sync'); Description = 'rpc sync gate' }
  @{ Name = 'test:web';      Cmd = @('npx', 'vitest', 'run');
     Args = @{ file = @{ Type = 'path'; Position = 'append' } };
     Description = 'vitest workspace (опц. путь файла)' }
  @{ Name = 'test:backend';  Cmd = @('backend/.venv/Scripts/python.exe', '-m', 'pytest', '-q');
     Args = @{ path = @{ Type = 'path'; Position = 'append' } };
     Description = 'pytest backend (опц. путь)' }
  @{ Name = 'dev:start';     Cmd = @('powershell', '-NoProfile', '-File', 'start-dev.ps1');
     Background = $true; Port = 35204; Description = 'backend+web dev-серверы (tree-kill только через dev:stop)' }
  @{ Name = 'dev:stop';      Cmd = @('powershell', '-NoProfile', '-File', 'stop.ps1');
     Description = 'остановка всех dev-серверов MidasAI' }
)
$RunnerPort = 8796

# порты, routable через ingress с ingress-токеном (fail-closed)
$AllowedPorts = @(35203, 35204)
# секреты/токены проекта, закрытые deny-монтами из /workspace
$DenyMounts = @('backend/.env', 'ports.env', '.midas-bridge-token')
$SetupCmds = @()
