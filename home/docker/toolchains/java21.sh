install_java21() {
  if [ ! -x /opt/tools/java21/bin/java ]; then
    mkdir -p /opt/tools/java21
    curl -fsSL "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse" \
      | tar -xz -C /opt/tools/java21 --strip-components=1
  fi
}
