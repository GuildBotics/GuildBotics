#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-$("$SCRIPT_DIR/desktop-target.sh")}"
PROGRAMS_DIR="$REPO_ROOT/desktop/src-tauri/binaries/guildbotics"
SIDECAR_PATH="$PROGRAMS_DIR/guildbotics-app-api"
CLI_PATH="$PROGRAMS_DIR/guildbotics"
if [[ "$DESKTOP_TARGET" == *-pc-windows-msvc ]]; then
  SIDECAR_PATH="${SIDECAR_PATH}.exe"
  CLI_PATH="${CLI_PATH}.exe"
fi
# The sidecar takes its port from the environment, as it does in the app.
export GUILDBOTICS_SERVER_PORT="${GUILDBOTICS_SERVER_PORT:-8765}"
PORT="$GUILDBOTICS_SERVER_PORT"
SMOKE_HOME="$(mktemp -d)"
export HOME="$SMOKE_HOME"
if [[ "$DESKTOP_TARGET" == *-pc-windows-msvc ]]; then
  export USERPROFILE="$(cygpath -w "$SMOKE_HOME")"
fi

OWNER_TOKEN="$SMOKE_HOME/.guildbotics/data/server/$PORT/owner.token"

"$SIDECAR_PATH" &
SIDECAR_PID=$!
cleanup() {
  kill "$SIDECAR_PID" 2>/dev/null || true
  rm -rf -- "$SMOKE_HOME"
}
trap cleanup EXIT

# The sidecar writes its owner token before it listens. The header goes through
# stdin, so the token never shows in the process list.
healthy() {
  [[ -r "$OWNER_TOKEN" ]] &&
    printf 'Authorization: Bearer %s\n' "$(<"$OWNER_TOKEN")" |
    curl -fsS -H @- "http://127.0.0.1:$PORT/api/health" >/dev/null
}

# The sidecar carries the screens it serves (desktop/sidecar/guildbotics.spec):
# the page, and the script the page loads.
serves_screens() {
  local script
  curl -fsS "http://127.0.0.1:$PORT/" -o "$SMOKE_HOME/index.html" &&
    script="$(grep -o -m1 'src="/assets/[^"]*\.js"' "$SMOKE_HOME/index.html" | cut -d'"' -f2)" &&
    curl -fsS "http://127.0.0.1:$PORT$script" -o /dev/null
}

for _ in $(seq 1 30); do
  if healthy; then
    echo "sidecar health check passed"
    serves_screens || {
      echo "the bundled Local API does not serve the screens" >&2
      exit 1
    }
    "$CLI_PATH" --help >/dev/null
    "$CLI_PATH" member --help >/dev/null
    # The bundle must carry the agent environment runtime: in this fresh home
    # the CLI places it under ~/.guildbotics/data/msb itself. The status is
    # read against an empty workspace, because it is a workspace's question.
    mkdir -p "$SMOKE_HOME/ws/.guildbotics/config"
    # Drain stdout so pipefail cannot mistake an early grep exit for CLI failure.
    "$CLI_PATH" environment --workspace "$SMOKE_HOME/ws" status | grep "^runtime: available" >/dev/null || {
      echo "the bundled CLI does not carry the agent environment runtime" >&2
      "$CLI_PATH" environment --workspace "$SMOKE_HOME/ws" status >&2 || true
      exit 1
    }
    exit 0
  fi
  sleep 1
done

echo "sidecar did not become healthy in time" >&2
exit 1
