# Профиль проекта Muffin. Копируй из _template.ps1 для новых проектов.
$ProjectDir = 'd:/Sources/StartUp/Muffin'
# спеки тулчейнов в /opt/tools (volume): java21, flutter:<тег>
$Toolchain = 'java21 flutter:3.44.9'
$GitName = 'WhiteBite'
$GitEmail = 'ad.lord9000@yandex.ru'
# куда смотрит preview-туннель: виджетбук живёт на хосте (supervisor, :8080)
$PreviewOrigin = 'http://host.docker.internal:8080'
# host-сервисы, отдаваемые наружу через ingress (/p/<Port>/...):
#   Auth='bearer' → сервис слушает Port+1 без авторизации, auth-proxy на Port
#   Auth=''       → сервис на Port, авторизует ingress своим INGRESS_TOKEN
$HostServices = @(
  @{ Port = 8792; Auth = 'bearer'; Cwd = 'D:\Sources\StartUp\Muffin'; Cmd = 'python tools/muffin-supervisor/server.py' }
)
