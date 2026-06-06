#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

HOST="${GUILDBOTICS_APP_API_HOST:-127.0.0.1}"
PORT="${GUILDBOTICS_APP_API_PORT:-8765}"
TOKEN="${GUILDBOTICS_APP_API_TOKEN:-dev-token}"

cd "$REPO_ROOT/desktop"

if [[ ! -d node_modules ]]; then
  npm install
fi

VITE_GUILDBOTICS_API_TOKEN="$TOKEN" \
  VITE_GUILDBOTICS_API_BASE="http://${HOST}:${PORT}" \
  npm run dev
