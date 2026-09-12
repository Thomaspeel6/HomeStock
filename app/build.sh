#!/bin/bash
# Assemble HomeStock.app: the Swift window, with the Python engine inside it.
#
# The engine is the same code the MCP server runs — bundled rather than
# reimplemented, so there is one set of rules about the event log and one
# place where the arithmetic lives.
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="${1:-release}"
APP="build/HomeStock.app"
VERSION="$(grep -m1 '^version = ' ../pyproject.toml | cut -d'"' -f2)"
PYTHON_VERSION="3.12.12"

swift build -c "$CONFIG" --disable-sandbox

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine"

cp ".build/$CONFIG/HomeStock" "$APP/Contents/MacOS/HomeStock"
cp -R ../homestock "$APP/Contents/Resources/engine/homestock"
cp -R ../prompts "$APP/Contents/Resources/engine/homestock/prompts"
cp -R ../recipes "$APP/Contents/Resources/engine/homestock/recipes"
# Bring our own Python. macOS still ships 3.9, the engine needs 3.11+, and
# pydantic_core is a compiled extension whose wheel is built per interpreter
# version — so "use whatever python3 the user has" cannot work for a download.
# python-build-standalone is relocatable by design; uv fetches it for us.
uv python install "$PYTHON_VERSION" >/dev/null 2>&1 || true
RUNTIME_BIN="$(uv python find "$PYTHON_VERSION")"
RUNTIME_ROOT="$(cd "$(dirname "$RUNTIME_BIN")/.." && pwd)"
cp -R "$RUNTIME_ROOT" "$APP/Contents/Resources/python"

# Dependencies installed with the very interpreter that will run them, so the
# compiled wheels match exactly.
"$APP/Contents/Resources/python/bin/python3" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
uv pip install --quiet --target "$APP/Contents/Resources/engine" \
  --python "$APP/Contents/Resources/python/bin/python3" "mcp>=1.26,<2"

find "$APP/Contents/Resources/engine" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
# .dist-info stays: mcp reads its own version through importlib.metadata at
# import time, and without the metadata it raises PackageNotFoundError.

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>HomeStock</string>
  <key>CFBundleDisplayName</key><string>HomeStock</string>
  <key>CFBundleIdentifier</key><string>app.homestock.mac</string>
  <key>CFBundleExecutable</key><string>HomeStock</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Copyright 2026 Thomas Peel. FSL-1.1-MIT.</string>
</dict></plist>
PLIST

echo "Built $APP (version $VERSION)"
