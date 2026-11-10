#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

if [[ ! -f node_modules/.package-lock.json || package-lock.json -nt node_modules/.package-lock.json ]]; then
  npm ci
fi

# Vite serves the pages and proxies `/-/guildbotics/api` to the backend at
# GUILDBOTICS_SERVER_PORT (7878 unless set; `desktop/vite.config.ts`).
exec npm run dev --workspace desktop
