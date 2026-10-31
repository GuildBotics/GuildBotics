#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT/desktop"

if [[ ! -d node_modules ]]; then
  npm ci
fi

# The windows show the dev server, but a browser (`guildbotics open`) is given
# the screens the backend serves from desktop/dist, which the real programs
# built on Windows carry too: build them from this checkout, so a browser opened
# now shows the same screens. Type errors do not stop a development launch, so
# this is the Vite build alone.
npx vite build

"$SCRIPT_DIR/desktop-write-dev-binaries.sh"

# The Rust watcher restarts the whole app on a src-tauri change, and the Local
# API sidecar dies with it: a running service and its members' work are cut off
# mid-run. The frontend still hot-reloads (that is Vite's doing); restart this
# script to pick up a Rust or Tauri config change.
exec npm run tauri dev -- --no-watch
