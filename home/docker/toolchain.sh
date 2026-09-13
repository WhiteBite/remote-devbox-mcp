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
# пост-установочные команды профиля приходят base64 через env (bind-mount
# свеже-созданных путей ломается на Docker Desktop Windows)
if [ -n "$SETUP_SCRIPT_B64" ]; then
  echo "$SETUP_SCRIPT_B64" | base64 -d > /tmp/setup-project.sh
  . /tmp/setup-project.sh
fi

# registry.json: refcount тулчейнов по /opt/tools/refs/*.list (рендерит devbox.ps1).
# refcount=0 → кандидат на удаление (devbox.ps1 clean-tools в будущем).
node -e '
const fs = require("fs");
const P = "/opt/tools/";
const refs = {};
try {
  for (const f of fs.readdirSync(P + "refs")) {
    for (const s of fs.readFileSync(P + "refs/" + f, "utf8").split(/\s+/).filter(Boolean)) {
      (refs[s] = refs[s] || []).push(f);
    }
  }
} catch (e) {}
let reg = {};
try { reg = JSON.parse(fs.readFileSync(P + "registry.json", "utf8")); } catch (e) {}
const now = new Date().toISOString();
const entries = reg.entries || {};
for (const [spec, by] of Object.entries(refs)) {
  const e = entries[spec] || { installed_at: now };
  e.refcount = by.length;
  e.referenced_by = by;
  e.path = spec.startsWith("flutter:")
    ? "/opt/tools/flutter-" + spec.split(":")[1]
    : spec === "java21" ? "/opt/tools/java21" : "/opt/tools/" + spec;
  entries[spec] = e;
}
for (const spec of Object.keys(entries)) if (!refs[spec]) entries[spec].refcount = 0;
fs.writeFileSync(P + "registry.json", JSON.stringify({ entries }, null, 2));
' || echo "[toolchain] WARN: registry reconcile failed"

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
