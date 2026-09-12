# Шаблон профиля проекта. Скопируй в projects/<имя>.ps1 и заполни.
# Применение: home\devbox.ps1 use <имя>
$ProjectDir = 'd:/Sources/StartUp/<repo>'
# спеки тулчейнов в /opt/tools (volume, кэш между проектами): java21, flutter:<тег>
$Toolchain = ''
# git-идентичность для коммитов агента внутри контейнера
$GitName = 'WhiteBite'
$GitEmail = 'ad.lord9000@yandex.ru'
# куда смотрит preview-туннель: приложение в контейнере или сервис на хосте
$PreviewOrigin = 'http://toolbox:8788'
# host-сервисы наружу через ingress (/p/<Port>/...):
#   Auth='bearer' → сервис слушает Port+1 без авторизации, auth-proxy на Port
#                   (клиент шлёт свой токен, ingress не подменяет)
#   Auth=''       → сервис на Port, авторизует ingress своим INGRESS_TOKEN
$HostServices = @(
  # @{ Port = 8792; Auth = 'bearer'; Cwd = 'D:\path\to\repo'; Cmd = 'python tools/supervisor/server.py' }
)
