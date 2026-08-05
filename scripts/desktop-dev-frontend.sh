#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=scripts/desktop-token.sh
source "$SCRIPT_DIR/desktop-token.sh"

HOST="${GUILDBOTICS_APP_API_HOST:-127.0.0.1}"
PORT="${GUILDBOTICS_APP_API_PORT:-8765}"

TOKEN="${GUILDBOTICS_APP_API_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  TOKEN_FILE="$(guildbotics_dev_token_file "$PORT")"
  if [[ ! -r "$TOKEN_FILE" ]]; then
    echo "no dev token for port $PORT at $TOKEN_FILE." >&2
    echo "Start scripts/desktop-dev-backend.sh first, or export the same" >&2
    echo "GUILDBOTICS_APP_API_TOKEN in both terminals." >&2
    exit 1
  fi
  TOKEN="$(cat "$TOKEN_FILE")"
fi

cd "$REPO_ROOT/desktop"

if [[ ! -d node_modules ]]; then
  npm install
fi

VITE_GUILDBOTICS_API_TOKEN="$TOKEN" \
  VITE_GUILDBOTICS_API_BASE="http://${HOST}:${PORT}" \
  npm run dev
