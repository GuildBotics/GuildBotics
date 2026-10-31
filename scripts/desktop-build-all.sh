#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# The programs carry the screens, and the package carries the programs.
(cd "$SCRIPT_DIR/../desktop" && npm ci && npm run build)
"$SCRIPT_DIR/desktop-build-backend.sh"
"$SCRIPT_DIR/desktop-build-frontend.sh"
