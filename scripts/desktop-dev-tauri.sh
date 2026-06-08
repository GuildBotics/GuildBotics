#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-aarch64-apple-darwin}"
SIDECAR_PATH="$REPO_ROOT/desktop/src-tauri/binaries/guildbotics-app-api-${DESKTOP_TARGET}"

mkdir -p "$(dirname "$SIDECAR_PATH")"
cat >"$SIDECAR_PATH" <<'SH'
#!/bin/sh
set -eu
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"
if command -v uv >/dev/null 2>&1; then
  exec uv run --no-sync python -m guildbotics.app_api "$@"
fi
exec python3 -m guildbotics.app_api "$@"
SH
chmod +x "$SIDECAR_PATH"

cd "$REPO_ROOT/desktop"

if [[ ! -d node_modules ]]; then
  npm install
fi

exec npm run tauri dev
