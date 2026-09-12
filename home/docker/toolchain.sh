#!/bin/sh
# Идемпотентная установка тулчейнов в /opt/tools (volume rdm-tools):
# переживает пересоздания контейнера и смены проекта. Повторный старт дешев.
# TOOLCHAIN="java21 flutter:3.44.9 ..." — спеки через пробел; каждый спек =
# файл /usr/local/bin/toolchains/<имя>.sh с функцией install_<имя>(спек).
# Маркер /opt/tools/.toolchain-<спек>: смена версии спека = новый маркер.
set -e

for f in /usr/local/bin/toolchains/*.sh; do
  [ -f "$f" ] && . "$f"
done

for spec in $TOOLCHAIN; do
  name=${spec%%:*}
  fn="install_$name"
  marker="/opt/tools/.toolchain-$spec"
  if [ ! -f "$marker" ]; then
    if command -v "$fn" >/dev/null 2>&1; then
      echo "[toolchain] $spec: ставлю..."
      "$fn" "$spec"
      touch "$marker"
      echo "[toolchain] $spec: готово"
    else
      echo "[toolchain] неизвестный спек: $spec (пропущен)"
    fi
  fi
done

# пост-установочные команды проекта (рендерит devbox.ps1 из $SetupCmds профиля;
# маркеры и warn/required-семантика уже вшиты в сгенерированный файл)
[ -f /opt/tools/setup-project.sh ] && . /opt/tools/setup-project.sh

# PATH/JAVA_HOME для воркеров моста: всё, что лежит в /opt/tools
for d in /opt/tools/java21/bin /opt/tools/flutter-*/bin /opt/tools/bin; do
  [ -d "$d" ] && PATH="$d:$PATH"
done
export PATH
[ -d /opt/tools/java21 ] && export JAVA_HOME=/opt/tools/java21

# git-хуки проекта (.husky и т.п.) не должны выполняться в контейнере
git config --global core.hooksPath /dev/null
[ -n "$GIT_NAME" ] && git config --global user.name "$GIT_NAME"
[ -n "$GIT_EMAIL" ] && git config --global user.email "$GIT_EMAIL"

exec npm run start:http
