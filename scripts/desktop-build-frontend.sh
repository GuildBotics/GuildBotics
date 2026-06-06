#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-aarch64-apple-darwin}"
SIDECAR_PATH="$REPO_ROOT/desktop/src-tauri/binaries/guildbotics-app-api-${DESKTOP_TARGET}"

if [[ ! -x "$SIDECAR_PATH" ]]; then
  echo "Missing executable backend sidecar: $SIDECAR_PATH" >&2
  echo "Run scripts/desktop-build-backend.sh first, or set DESKTOP_TARGET." >&2
  exit 1
fi

cd "$REPO_ROOT/desktop"

npm install
npm run tauri build -- --target "$DESKTOP_TARGET"
