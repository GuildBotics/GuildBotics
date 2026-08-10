#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-$("$SCRIPT_DIR/desktop-target.sh")}"
SIDECAR_PATH="$REPO_ROOT/desktop/src-tauri/binaries/guildbotics-app-api-${DESKTOP_TARGET}"
CLI_PATH="$REPO_ROOT/desktop/src-tauri/binaries/guildbotics-cli-${DESKTOP_TARGET}"
if [[ "$DESKTOP_TARGET" == *-pc-windows-msvc ]]; then
  SIDECAR_PATH="${SIDECAR_PATH}.exe"
  CLI_PATH="${CLI_PATH}.exe"
fi
PORT="${GUILDBOTICS_SIDECAR_SMOKE_PORT:-8765}"
SMOKE_HOME="$(mktemp -d)"
export HOME="$SMOKE_HOME"
if [[ "$DESKTOP_TARGET" == *-pc-windows-msvc ]]; then
  export USERPROFILE="$(cygpath -w "$SMOKE_HOME")"
fi

# shellcheck source=scripts/desktop-token.sh
source "$SCRIPT_DIR/desktop-token.sh"
TOKEN="${GUILDBOTICS_SIDECAR_SMOKE_TOKEN:-$(guildbotics_random_token)}"

GUILDBOTICS_APP_API_TOKEN="$TOKEN" "$SIDECAR_PATH" --host 127.0.0.1 --port "$PORT" &
SIDECAR_PID=$!
cleanup() {
  kill "$SIDECAR_PID" 2>/dev/null || true
  rm -rf -- "$SMOKE_HOME"
}
trap cleanup EXIT

for _ in $(seq 1 30); do
  if curl -fsS -H "X-GuildBotics-Session-Token: $TOKEN" "http://127.0.0.1:$PORT/health" >/dev/null; then
    echo "sidecar health check passed"
    "$CLI_PATH" --help >/dev/null
    "$CLI_PATH" member --help >/dev/null
    exit 0
  fi
  sleep 1
done

echo "sidecar did not become healthy in time" >&2
exit 1
