#!/usr/bin/env bash
# What building the companion app needs on a Linux machine, without root: a JDK 21 in ~/.jdks/temurin-21 and the
# Android SDK in ~/Android/Sdk (command-line tools, platform-tools, the platforms and build tools the build names).
# Run once per machine; it skips what is there. Gradle itself comes through the wrapper (./gradlew).
# About 1.2 GB on disk, plus Gradle's caches (~1.5 GB after the first build).
set -euo pipefail

JDK_DIR=${JDK_DIR:-$HOME/.jdks/temurin-21}
SDK=${ANDROID_HOME:-$HOME/Android/Sdk}
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

if [[ ! -x $JDK_DIR/bin/java ]]; then
  echo "JDK 21 (Eclipse Temurin) into $JDK_DIR"
  meta=$(curl -fsSL "https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jdk&os=linux&vendor=eclipse")
  link=$(python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["binary"]["package"]["link"])' <<<"$meta")
  sum=$(python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["binary"]["package"]["checksum"])' <<<"$meta")
  curl -fsSL -o "$tmp/jdk.tar.gz" "$link"
  echo "$sum  $tmp/jdk.tar.gz" | sha256sum -c -
  mkdir -p "$JDK_DIR" && tar -xzf "$tmp/jdk.tar.gz" -C "$JDK_DIR" --strip-components=1
fi
export JAVA_HOME=$JDK_DIR

if [[ ! -x $SDK/cmdline-tools/latest/bin/sdkmanager ]]; then
  echo "Android command-line tools into $SDK"
  # the current build of the tools and its checksum, from the SDK's own repository index
  read -r zip sum < <(curl -fsSL https://dl.google.com/android/repository/repository2-3.xml | python3 -c '
import re, sys
x = sys.stdin.read()
pkg = re.search(r"<remotePackage path=\"cmdline-tools;latest\">.*?</remotePackage>", x, re.S).group(0)
arc = [a for a in re.findall(r"<archive>.*?</archive>", pkg, re.S) if "linux" in a][0]
print(re.search(r"<url>([^<]+)</url>", arc).group(1), re.search(r"<checksum[^>]*>([^<]+)</checksum>", arc).group(1))')
  curl -fsSL -o "$tmp/tools.zip" "https://dl.google.com/android/repository/$zip"
  echo "$sum  $tmp/tools.zip" | sha1sum -c -
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$tmp/tools.zip" "$tmp/x"
  mkdir -p "$SDK/cmdline-tools" && mv "$tmp/x/cmdline-tools" "$SDK/cmdline-tools/latest" && chmod +x "$SDK/cmdline-tools/latest/bin/"*
fi

# installing these means accepting the Android SDK licences
# (fed from a process substitution: `yes | …` ends with yes killed by SIGPIPE, which pipefail takes for a failure)
"$SDK/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDK" --licenses < <(yes) >/dev/null
"$SDK/cmdline-tools/latest/bin/sdkmanager" --sdk_root="$SDK" "platform-tools" "platforms;android-37.1" "build-tools;36.0.0" | tail -1

echo
echo "ready. In android/:"
echo "  export JAVA_HOME=$JDK_DIR ANDROID_HOME=$SDK"
echo "  ./gradlew --no-daemon :core:test :app:testDebugUnitTest :app:assembleDebug"
