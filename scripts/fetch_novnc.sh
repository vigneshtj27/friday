#!/usr/bin/env sh
# Vendor the noVNC client into friday/web/static/novnc so the remote-view page
# loads nothing from third-party CDNs.
set -eu
VERSION="${NOVNC_VERSION:-1.5.0}"
SHA256="${NOVNC_SHA256:-}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/friday/web/static/novnc"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL -o "$TMP/novnc.tar.gz" "https://github.com/novnc/noVNC/archive/refs/tags/v${VERSION}.tar.gz"
if [ -n "$SHA256" ]; then
  echo "$SHA256  $TMP/novnc.tar.gz" | sha256sum -c -
fi
tar -xzf "$TMP/novnc.tar.gz" -C "$TMP"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -R "$TMP/noVNC-${VERSION}/core" "$DEST/core"
mkdir -p "$DEST/vendor"
cp -R "$TMP/noVNC-${VERSION}/vendor/pako" "$DEST/vendor/pako"
echo "noVNC ${VERSION} installed in $DEST"
