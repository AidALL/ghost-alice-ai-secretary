#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir="$task_root/build"
mkdir -p "$output_dir/TestWindow.app/Contents/MacOS"
xcrun swiftc -swift-version 5 -parse-as-library -O -framework AppKit -framework ApplicationServices -framework ScreenCaptureKit \
  "$task_root/native/Desktop.swift" -o "$output_dir/desktop-secretary"
xcrun swiftc -swift-version 5 -parse-as-library -O -framework AppKit \
  "$task_root/native/TestWindow.swift" -o "$output_dir/TestWindow.app/Contents/MacOS/TestWindow"
cat > "$output_dir/TestWindow.app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.ai-secretary.fixture</string>
<key>CFBundleName</key><string>AI Secretary Fixture</string>
<key>CFBundleExecutable</key><string>TestWindow</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$output_dir/desktop-secretary"
codesign --force --sign - "$output_dir/TestWindow.app"
printf '%s\n' "$output_dir/desktop-secretary" "$output_dir/TestWindow.app"
