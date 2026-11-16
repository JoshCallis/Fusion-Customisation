#!/bin/bash
# build_app.sh — compile one WKWebView shell (a *App/main.swift) into
# /Applications/<Name>.app, with bundle id com.josh.<Name>, and sign it.
#
#   build_app.sh <Name> <path/to/main.swift> [--agent]
#
#   --agent   LSUIElement: no Dock icon, no menu bar (FusionDock, FusionValue).
#
# Every companion window (fusion_{params,browser,shortcuts,value}, fusion_dock,
# remap/RemapApp) builds through this; each dir's build.sh is a one-line wrapper
# so `<dir>/build.sh` keeps working. They were six copies of the same 70 lines.
# fusion_ui/WindowShell.swift (helpers the shells share) is compiled into every app.
#
# The bundle id and the signing identity are what TCC keys a grant on — keep both
# stable. Rebuild after editing main.swift: hotkeys launch the INSTALLED app, so an
# edited source that was never rebuilt does nothing.
set -e
NAME="$1"; SRC="$2"
[[ -n "$NAME" && -f "$SRC" ]] || { echo "usage: build_app.sh <Name> <main.swift> [--agent]" >&2; exit 2; }
AGENT=""
[[ "$3" == "--agent" ]] && AGENT="
    <key>LSUIElement</key>
    <true/>"

source "$(dirname "$0")/sign_id.sh"

OUT="${APP_DIR:-/Applications}/$NAME.app"      # APP_DIR: build elsewhere to test
MACOS="$OUT/Contents/MacOS"

echo "Building $NAME.app..."
mkdir -p "$MACOS"

cat > "$OUT/Contents/Info.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>$NAME</string>
    <key>CFBundleIdentifier</key>
    <string>com.josh.$NAME</string>
    <key>CFBundleName</key>
    <string>$NAME</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.7</string>$AGENT
    <key>NSPrincipalClass</key>
    <string>NSApplication</string>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsLocalNetworking</key>
        <true/>
    </dict>
    <key>NSMicrophoneUsageDescription</key>
    <string>Records your vocal (OneTake) — scripts this app starts use the microphone.</string>
</dict>
</plist>
EOF

swiftc \
  -target arm64-apple-macosx12.7 \
  -sdk "$(xcrun --show-sdk-path)" \
  -framework Cocoa \
  -framework WebKit \
  -o "$MACOS/$NAME" \
  "$SRC" "$(dirname "$0")/../fusion_ui/WindowShell.swift"

codesign -f -s "$SIGN_ID" "$OUT"
echo "  signed: $SIGN_ID"
echo "Done: $OUT"
