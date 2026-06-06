#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

HOST="${GUILDBOTICS_APP_API_HOST:-127.0.0.1}"
PORT="${GUILDBOTICS_APP_API_PORT:-8765}"
TOKEN="${GUILDBOTICS_APP_API_TOKEN:-dev-token}"
BASE_URL="http://${HOST}:${PORT}"

"$SCRIPT_DIR/desktop-dev-backend.sh" &
BACKEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in {1..150}; do
  if curl -fsS -H "X-GuildBotics-Session-Token: ${TOKEN}" \
    "${BASE_URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.3
done

if ! curl -fsS -H "X-GuildBotics-Session-Token: ${TOKEN}" \
  "${BASE_URL}/health" >/dev/null 2>&1; then
  echo "Backend did not become healthy: ${BASE_URL}" >&2
  exit 1
fi

"$SCRIPT_DIR/desktop-dev-frontend.sh"
