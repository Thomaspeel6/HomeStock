#!/bin/bash
# HomeStock release builder — one command to a downloadable, updatable app.
#
#   ./scripts/release.sh <version> <build-number>
#   e.g.  ./scripts/release.sh 0.2.0 2
#
# <build-number> must increase every release: Sparkle compares it to decide
# whether an installed copy is out of date. Keep it simple — 1, 2, 3.
#
# Produces dist/HomeStock-<version>.dmg, EdDSA-signs it with the private key in
# your login Keychain, and regenerates appcast.xml. See RELEASING.md to publish.
set -euo pipefail

VERSION="${1:?usage: release.sh <version> <build-number>}"
BUILD="${2:?usage: release.sh <version> <build-number>}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$ROOT/dist"
APP="$ROOT/app/build/HomeStock.app"
DOWNLOAD_PREFIX="https://github.com/Thomaspeel6/HomeStock/releases/download/v$VERSION/"

echo "==> Building HomeStock $VERSION ($BUILD)"
HOMESTOCK_VERSION="$VERSION" HOMESTOCK_BUILD="$BUILD" "$ROOT/app/build.sh" release

# Ad-hoc signature. Not an Apple Developer ID, so Gatekeeper still asks the
# first time (RELEASING.md tells users how) — but an arm64 binary must carry at
# least an ad-hoc signature or macOS refuses to launch it at all.
echo "==> Signing"
codesign --force --sign - --timestamp=none "$APP/Contents/MacOS/HomeStock"
codesign --force --sign - --timestamp=none "$APP"
codesign --verify --strict "$APP" && echo "    signature ok"

echo "==> Packaging DMG"
mkdir -p "$DIST"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="$DIST/HomeStock-$VERSION.dmg"
rm -f "$DMG"
hdiutil create -volname "HomeStock $VERSION" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
echo "    $(du -h "$DMG" | cut -f1)  $DMG"

# Sparkle's tools live in the SPM checkout once the package has resolved.
TOOLS="$ROOT/.sparkle-tools"
if [[ -x "$TOOLS/generate_appcast" ]]; then
  echo "==> Signing update + regenerating appcast"
  "$TOOLS/generate_appcast" --download-url-prefix "$DOWNLOAD_PREFIX" "$DIST"
  cp "$DIST/appcast.xml" "$ROOT/appcast.xml"
else
  echo "==> Skipping appcast (run scripts/sparkle-tools.sh once to build the tools)"
fi

cat <<NEXT

Built. To publish:

  gh release create v$VERSION "$DMG" \\
    --title "HomeStock $VERSION" --notes "What changed…"
  git add appcast.xml && git commit -m "Release $VERSION" && git push

The appcast points at
  ${DOWNLOAD_PREFIX}HomeStock-$VERSION.dmg
so the tag must be v$VERSION and the asset name must match exactly.
NEXT
