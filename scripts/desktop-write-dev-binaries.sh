#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-$("$SCRIPT_DIR/desktop-target.sh")}"
BIN_DIR="$REPO_ROOT/desktop/src-tauri/binaries"
SIDECAR_PATH="$BIN_DIR/guildbotics-app-api-${DESKTOP_TARGET}"
CLI_PATH="$BIN_DIR/guildbotics-cli-${DESKTOP_TARGET}"

mkdir -p "$BIN_DIR"
cat >"$SIDECAR_PATH" <<SH
#!/bin/sh
set -eu
REPO_ROOT="$REPO_ROOT"
cd "$REPO_ROOT"
if command -v uv >/dev/null 2>&1; then
  exec uv run --no-sync python -m guildbotics.app_api "\$@"
fi
exec python3 -m guildbotics.app_api "\$@"
SH
chmod +x "$SIDECAR_PATH"

cat >"$CLI_PATH" <<SH
#!/bin/sh
set -eu
REPO_ROOT="$REPO_ROOT"
cd "$REPO_ROOT"
if command -v uv >/dev/null 2>&1; then
  exec uv run --no-sync guildbotics "\$@"
fi
exec python3 -m guildbotics.cli "\$@"
SH
chmod +x "$CLI_PATH"
