#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-$("$SCRIPT_DIR/desktop-target.sh")}"
PROGRAMS_DIR="$REPO_ROOT/desktop/src-tauri/binaries/guildbotics"
BUILD_ARGS=(--target "$DESKTOP_TARGET")
if [[ "$DESKTOP_TARGET" == *-pc-windows-msvc ]]; then
  BUILD_ARGS+=(--bundles nsis)
fi

if [[ ! -f "$PROGRAMS_DIR/build-id" ]]; then
  echo "Missing desktop programs: $PROGRAMS_DIR" >&2
  echo "Run scripts/desktop-build-backend.sh first." >&2
  exit 1
fi

cd "$REPO_ROOT/desktop"

npm ci
npm run tauri build -- "${BUILD_ARGS[@]}"
