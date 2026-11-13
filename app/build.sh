#!/bin/sh
# Builds the Mac app for this dashboard: app/build/<Name>.app
#   app/build.sh             build only
#   app/build.sh --install   build, then copy to /Applications (or ~/Applications)
set -eu
INSTALL=0; [ "${1:-}" = "--install" ] && INSTALL=1
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
NAME="$(/usr/bin/plutil -extract name raw -o - "$HERE/app.json")"
ID="$(/usr/bin/plutil -extract bundleId raw -o - "$HERE/app.json")"
ICON="$(/usr/bin/plutil -extract icon raw -o - "$HERE/app.json")"   # "BG FG GLYPH FONT"
OUT="$HERE/build/$NAME.app"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/dashapp.XXXXXX")"; trap 'rm -rf "$TMP"' EXIT
command -v swiftc >/dev/null || { echo "swiftc not found: run xcode-select --install"; exit 1; }

rm -rf "$OUT"; mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
swiftc -O -o "$OUT/Contents/MacOS/$NAME" "$HERE/DashApp.swift" -framework Cocoa -framework WebKit
cp "$HERE/app.json" "$OUT/Contents/Resources/app.json"

# icon
set -- $ICON
swiftc -o "$TMP/icon" "$HERE/icon.swift" -framework Cocoa
"$TMP/icon" "$TMP/icon.png" "$1" "$2" "$3" "${4:-}"
mkdir "$TMP/AppIcon.iconset"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/icon.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$TMP/icon.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$TMP/AppIcon.iconset" -o "$OUT/Contents/Resources/AppIcon.icns"

cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleExecutable</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>$ID</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.finance</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>DashRepoRoot</key><string>$REPO</string>
</dict></plist>
PLIST
codesign --force --deep -s - "$OUT" >/dev/null 2>&1 || true
echo "built $OUT"

if [ "$INSTALL" = 1 ]; then
  DEST=/Applications; [ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
  rm -rf "$DEST/$NAME.app"; cp -R "$OUT" "$DEST/"
  echo "installed $DEST/$NAME.app"
fi
