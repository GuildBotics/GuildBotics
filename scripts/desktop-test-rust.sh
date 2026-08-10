#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT/desktop/src-tauri"

# Unit tests do not launch or package the configured external binaries.
TAURI_CONFIG='{"bundle":{"externalBin":[]}}' cargo test "$@"
