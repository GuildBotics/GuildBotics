#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

HOST="${GUILDBOTICS_APP_API_HOST:-127.0.0.1}"
PORT="${GUILDBOTICS_APP_API_PORT:-8765}"
TOKEN="${GUILDBOTICS_APP_API_TOKEN:-dev-token}"

cd "$REPO_ROOT"

if command -v uv >/dev/null 2>&1; then
  exec uv run --no-sync python -m guildbotics.app_api \
    --host "$HOST" --port "$PORT" --token "$TOKEN"
fi

exec python3 -m guildbotics.app_api \
  --host "$HOST" --port "$PORT" --token "$TOKEN"
