#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT/desktop"

if [[ ! -d node_modules ]]; then
  npm install
fi

# Vite serves the pages and proxies `/api` to the backend at
# GUILDBOTICS_SERVER_PORT (8765 unless set; `desktop/vite.config.ts`).
exec npm run dev
