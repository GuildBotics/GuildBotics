#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

"$SCRIPT_DIR/desktop-build-backend.sh"
"$SCRIPT_DIR/desktop-build-frontend.sh"
