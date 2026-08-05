#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=scripts/desktop-token.sh
source "$SCRIPT_DIR/desktop-token.sh"

HOST="${GUILDBOTICS_APP_API_HOST:-127.0.0.1}"
PORT="${GUILDBOTICS_APP_API_PORT:-8765}"
# The browser preview is always served by Vite on its fixed dev port
# (`desktop/vite.config.ts`, strictPort), so that is the only extra origin CORS
# needs to accept. The packaged Tauri origin is always allowed by the server.
ALLOWED_ORIGINS="${GUILDBOTICS_APP_API_ALLOWED_ORIGINS:-http://localhost:1420,http://127.0.0.1:1420}"

TOKEN_FILE=""
BACKEND_PID=""

cleanup() {
  if [[ -n "$TOKEN_FILE" ]]; then
    rm -f "$TOKEN_FILE"
  fi
  if [[ -n "$BACKEND_PID" ]]; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
}

# The backend runs in the background and is waited on, so a signal is handled
# right away instead of after it exits: a published token must never outlive the
# process that minted it.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

TOKEN="${GUILDBOTICS_APP_API_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  # Mint a token for this run and publish it for desktop-dev-frontend.sh, which
  # runs in a separate terminal. Export the same value in both terminals to opt
  # out of the file entirely.
  TOKEN="$(guildbotics_random_token)"
  TOKEN_DIR="$(guildbotics_dev_token_dir)"
  TOKEN_FILE="$(guildbotics_dev_token_file "$PORT")"
  mkdir -p "$TOKEN_DIR"
  chmod 700 "$TOKEN_DIR"
  rm -f "$TOKEN_FILE"
  (umask 077 && printf '%s\n' "$TOKEN" >"$TOKEN_FILE")
  echo "dev token published at $TOKEN_FILE" >&2
fi

export GUILDBOTICS_APP_API_TOKEN="$TOKEN"
export GUILDBOTICS_APP_API_ALLOWED_ORIGINS="$ALLOWED_ORIGINS"

cd "$REPO_ROOT"

if command -v uv >/dev/null 2>&1; then
  uv run --no-sync python -m guildbotics.app_api --host "$HOST" --port "$PORT" &
else
  python3 -m guildbotics.app_api --host "$HOST" --port "$PORT" &
fi
BACKEND_PID=$!
wait "$BACKEND_PID"
