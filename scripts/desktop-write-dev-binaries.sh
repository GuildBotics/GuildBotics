#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-$("$SCRIPT_DIR/desktop-target.sh")}"
BIN_DIR="$REPO_ROOT/desktop/src-tauri/binaries"
SIDECAR_PATH="$BIN_DIR/guildbotics-app-api-${DESKTOP_TARGET}"
CLI_PATH="$BIN_DIR/guildbotics-cli-${DESKTOP_TARGET}"

if [[ "$DESKTOP_TARGET" == *-pc-windows-msvc ]]; then
  "$SCRIPT_DIR/desktop-build-backend.sh"
  exit 0
fi

# The wrappers stand in for binaries that carry their own interpreter, so they
# must not depend on the PATH of whoever launches them. That is not the
# developer's shell: the desktop app is started from a launcher, and the
# managed CLI copied out of the wrapper is run by AI CLI tools and by
# non-interactive SSH sessions on a hub machine. `uv` is therefore resolved
# here, once, and written in as an absolute path.
UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
  echo "uv was not found. Install uv before building the desktop app." >&2
  exit 1
fi

mkdir -p "$BIN_DIR"
cat >"$SIDECAR_PATH" <<SH
#!/bin/sh
set -eu
cd "$REPO_ROOT"
exec "$UV_BIN" run --no-sync python -m guildbotics.app_api "\$@"
SH
chmod +x "$SIDECAR_PATH"

cat >"$CLI_PATH" <<SH
#!/bin/sh
set -eu
cd "$REPO_ROOT"
exec "$UV_BIN" run --no-sync guildbotics "\$@"
SH
chmod +x "$CLI_PATH"
