#!/usr/bin/env bash
# 构建 GameAVRec.app（app 级「目标应用原声 + 画面」录制器）
# 产物：tools/gamerec/build/GameAVRec.app（已被 .gitignore 忽略，不入库）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/build/GameAVRec.app"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/Info.plist" "$APP/Contents/Info.plist"

echo "编译 main.swift → $APP/Contents/MacOS/GameAVRec"
swiftc -O \
  -target arm64-apple-macos15.0 \
  -framework ScreenCaptureKit -framework AVFoundation -framework CoreMedia \
  -framework CoreGraphics -framework CoreAudio -framework Foundation \
  -o "$APP/Contents/MacOS/GameAVRec" \
  "$HERE/main.swift"

echo "ad-hoc 签名"
codesign --force --sign - --timestamp=none "$APP" >/dev/null 2>&1 || {
  echo "codesign 失败（不影响功能，但 TCC 身份可能不稳定）" >&2
}
codesign -dv "$APP" 2>&1 | sed -n '1,6p' || true
echo "OK: $APP"
