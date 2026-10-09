#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir="$task_root/build"
backend=builtin
test_window=false
scratch_path=
cache_path=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --backend) backend="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    --scratch-path) scratch_path="$2"; shift 2 ;;
    --cache-path) cache_path="$2"; shift 2 ;;
    --test-window) test_window=true; shift ;;
    *) printf 'Unknown native build argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
case "$backend" in builtin|peekaboo|all) ;; *) printf 'Unknown native backend: %s\n' "$backend" >&2; exit 2 ;; esac
mkdir -p "$output_dir"
if [ "$backend" = builtin ] || [ "$backend" = all ]; then
  xcrun swiftc -swift-version 5 -parse-as-library -O -target "$(uname -m)-apple-macos14.0" \
    -framework AppKit -framework ApplicationServices -framework ScreenCaptureKit -lsqlite3 \
    "$task_root/native/Desktop.swift" "$task_root/native/NativeContract.swift" "$task_root/native/PeekabooBackend.swift" \
    -o "$output_dir/desktop-secretary"
  codesign --force --sign - "$output_dir/desktop-secretary"
  printf '%s\n' "$output_dir/desktop-secretary"
fi
if [ "$backend" = peekaboo ] || [ "$backend" = all ]; then
  python3 "$task_root/native/verify_sources.py"
  [ -n "$scratch_path" ] || scratch_path="$output_dir/native-peekaboo-build"
  [ -n "$cache_path" ] || cache_path="$output_dir/native-swiftpm-cache"
  xcrun swift build --package-path "$task_root/native" --scratch-path "$scratch_path" --cache-path "$cache_path" \
    -c release --force-resolved-versions --product desktop-secretary-peekaboo
  product_dir=$(xcrun swift build --package-path "$task_root/native" --scratch-path "$scratch_path" --cache-path "$cache_path" \
    -c release --show-bin-path)
  cp "$product_dir/desktop-secretary-peekaboo" "$output_dir/desktop-secretary-peekaboo"
  codesign --force --sign - "$output_dir/desktop-secretary-peekaboo"
  mkdir -p "$output_dir/native-notices"
  cp "$task_root/native/THIRD_PARTY_NOTICES.md" "$task_root/native/UPSTREAM.json" "$output_dir/native-notices/"
  cp -R "$task_root/native/Notices" "$output_dir/native-notices/"
  printf '%s\n' "$output_dir/desktop-secretary-peekaboo"
fi
if [ "$test_window" = true ]; then
mkdir -p "$output_dir/TestWindow.app/Contents/MacOS"
xcrun swiftc -swift-version 5 -parse-as-library -O -target "$(uname -m)-apple-macos14.0" -framework AppKit \
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
codesign --force --sign - "$output_dir/TestWindow.app"
printf '%s\n' "$output_dir/TestWindow.app"
fi
