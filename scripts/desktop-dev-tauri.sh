#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

"$SCRIPT_DIR/desktop-write-dev-binaries.sh"

cd "$REPO_ROOT/desktop"

if [[ ! -d node_modules ]]; then
  npm ci
fi

# The Rust watcher restarts the whole app on a src-tauri change, and the Local
# API sidecar dies with it: a running service and its members' work are cut off
# mid-run. The frontend still hot-reloads (that is Vite's doing); restart this
# script to pick up a Rust or Tauri config change.
exec npm run tauri dev -- --no-watch
