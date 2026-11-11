#!/bin/zsh
# Install helper for ad-hoc signed Senrelay.app on macOS 14.0+ (arm64).
# Clears com.apple.quarantine extended attributes so Gatekeeper does not block
# an ad-hoc build downloaded from a browser or extracted from a ZIP archive.
set -euo pipefail

if (( $# > 2 )); then
  echo "Usage: $0 [/path/to/Senrelay.app] [/target/Applications]" >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
SOURCE_APP="${1:-$HERE/Senrelay.app}"
TARGET_DIR="${2:-${SENRELAY_INSTALL_DIR:-/Applications}}"
TARGET_APP="$TARGET_DIR/Senrelay.app"

if [[ ! -d "$SOURCE_APP" ]]; then
  echo "Senrelay.app not found at: $SOURCE_APP" >&2
  echo "Usage: $0 [/path/to/Senrelay.app] [/target/Applications]" >&2
  exit 1
fi

xattr -cr "$SOURCE_APP" || true

if [[ "${SENRELAY_NO_COPY:-0}" == "1" ]]; then
  codesign --verify --deep --strict "$SOURCE_APP"
  echo "Cleared quarantine attributes on $SOURCE_APP"
  exit 0
fi

mkdir -p "$TARGET_DIR"
SOURCE_REAL="${SOURCE_APP:A}"
TARGET_REAL="${TARGET_APP:A}"

if [[ "$SOURCE_REAL" == "$TARGET_REAL" ]]; then
  xattr -cr "$TARGET_APP" || true
  codesign --verify --deep --strict "$TARGET_APP"
  echo "Installed $TARGET_APP (quarantine attributes cleared)."
  exit 0
fi

STAGE_APP="$TARGET_DIR/.Senrelay.app.installing.$$"
BACKUP_APP="$TARGET_DIR/.Senrelay.app.previous.$$"

cleanup() {
  rm -rf "$STAGE_APP"
  if [[ -d "$BACKUP_APP" && ! -d "$TARGET_APP" ]]; then
    mv "$BACKUP_APP" "$TARGET_APP" || true
  else
    rm -rf "$BACKUP_APP"
  fi
}
trap cleanup EXIT

rm -rf "$STAGE_APP" "$BACKUP_APP"
ditto "$SOURCE_APP" "$STAGE_APP"
xattr -cr "$STAGE_APP" || true
codesign --verify --deep --strict "$STAGE_APP"

if [[ -e "$TARGET_APP" || -L "$TARGET_APP" ]]; then
  mv "$TARGET_APP" "$BACKUP_APP"
fi
mv "$STAGE_APP" "$TARGET_APP"
rm -rf "$BACKUP_APP"
codesign --verify --deep --strict "$TARGET_APP"
echo "Installed $TARGET_APP (quarantine attributes cleared)."
