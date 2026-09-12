install_flutter() {
  ver=${1#flutter:}
  if [ ! -x "/opt/tools/flutter-$ver/bin/flutter" ]; then
    git clone --depth 1 -b "$ver" https://github.com/flutter/flutter.git "/opt/tools/flutter-$ver"
  fi
  "/opt/tools/flutter-$ver/bin/flutter" --version
  "/opt/tools/flutter-$ver/bin/flutter" config --no-analytics
}
