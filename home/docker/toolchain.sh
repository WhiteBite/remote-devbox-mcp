#!/bin/sh
# Идемпотентная установка тулчейнов в /opt/tools (volume rdm-tools):
# переживает пересоздания контейнера и смены проекта. Повторный старт дешев.
# TOOLCHAIN="java21 flutter:3.44.9 ..." (спеки через пробел).
set -e

for spec in $TOOLCHAIN; do
  case "$spec" in
    java21)
      if [ ! -x /opt/tools/java21/bin/java ]; then
        echo "[toolchain] java21: скачиваю Temurin 21..."
        mkdir -p /opt/tools/java21
        curl -fsSL "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse" \
          | tar -xz -C /opt/tools/java21 --strip-components=1
        echo "[toolchain] java21: готово"
      fi
      ;;
    flutter:*)
      ver=${spec#flutter:}
      if [ ! -x "/opt/tools/flutter-$ver/bin/flutter" ]; then
        echo "[toolchain] flutter $ver: клонирую и грею Dart SDK..."
        git clone --depth 1 -b "$ver" https://github.com/flutter/flutter.git "/opt/tools/flutter-$ver"
        "/opt/tools/flutter-$ver/bin/flutter" --version
        "/opt/tools/flutter-$ver/bin/flutter" config --no-analytics
        echo "[toolchain] flutter $ver: готово"
      fi
      ;;
    *)
      echo "[toolchain] неизвестный спек: $spec (пропущен)"
      ;;
  esac
done

# PATH/JAVA_HOME для воркеров моста: всё, что лежит в /opt/tools
for d in /opt/tools/java21/bin /opt/tools/flutter-*/bin /opt/tools/bin; do
  [ -d "$d" ] && PATH="$d:$PATH"
done
export PATH
[ -d /opt/tools/java21 ] && export JAVA_HOME=/opt/tools/java21

# git-идентичность агента в контейнере (коммиты в проекте не падают)
[ -n "$GIT_NAME" ] && git config --global user.name "$GIT_NAME"
[ -n "$GIT_EMAIL" ] && git config --global user.email "$GIT_EMAIL"

exec npm run start:http
