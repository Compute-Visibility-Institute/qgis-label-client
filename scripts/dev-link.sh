#!/usr/bin/env bash
# Symlink this checkout into the QGIS plugins directory.
#
# Symlink, not copy: with a copy you edit one file and test another, and the two diverge
# within the hour. With Plugin Reloader bound to a key the cycle is about two seconds
# against thirty for a QGIS restart.
#
# Usage:  ./scripts/dev-link.sh [profile]        (default profile: "default")

set -euo pipefail

PROFILE="${1:-default}"
PACKAGE="qgis_label_client"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$(uname -s)" in
  Darwin) BASE="$HOME/Library/Application Support/QGIS/QGIS3/profiles" ;;
  Linux)  BASE="$HOME/.local/share/QGIS/QGIS3/profiles" ;;
  MINGW*|MSYS*|CYGWIN*) BASE="$APPDATA/QGIS/QGIS3/profiles" ;;
  *) echo "Unsupported platform: $(uname -s)" >&2; exit 1 ;;
esac

TARGET_DIR="$BASE/$PROFILE/python/plugins"
TARGET="$TARGET_DIR/$PACKAGE"

mkdir -p "$TARGET_DIR"

if [ -L "$TARGET" ]; then
  rm "$TARGET"
elif [ -e "$TARGET" ]; then
  echo "Refusing to replace a real directory at:" >&2
  echo "  $TARGET" >&2
  echo "Move or delete it first - it may be a manually installed copy." >&2
  exit 1
fi

ln -s "$REPO_ROOT/$PACKAGE" "$TARGET"
echo "Linked $TARGET -> $REPO_ROOT/$PACKAGE"
echo
echo "Next: restart QGIS, enable 'CVI Label Client' in Plugins > Manage and Install."
echo "A plugin that fails to import fails SILENTLY in the manager - if it does not"
echo "appear, look in View > Panels > Log Messages > Plugins."
