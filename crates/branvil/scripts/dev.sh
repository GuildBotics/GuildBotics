#!/usr/bin/env bash
# Build the GUI and server, initialize local data once, and open the browser.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/dev.sh [--data-dir PATH] [--owner NAME] [--no-open]

Build and run Branvil until Ctrl-C. Open the GUI after the server is ready.
  --data-dir PATH  Data directory (default: BRANVIL_DATA_DIR or crates/branvil/data).
                  Relative paths are resolved from the crate's directory.
  --owner NAME     Owner name for first initialization (default: owner).
  --no-open        Start the server without opening a browser.
  -h, --help       Show this help.

Existing data and branvil.toml are reused. Restart to pick up code changes.
EOF
}

crate_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="$(CDPATH= cd -- "$crate_root/../.." && pwd)"
data_dir="${BRANVIL_DATA_DIR:-$crate_root/data}"
owner=owner
open_browser=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data-dir|--owner)
      if [[ $# -lt 2 || -z "$2" || "$2" == --* ]]; then
        echo "Missing value for $1" >&2
        exit 2
      fi
      case "$1" in
        --data-dir) data_dir="$2" ;;
        *) owner="$2" ;;
      esac
      shift 2
      ;;
    --no-open) open_browser=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

startup_log=
server_pid=
# INT: the server stops at once (TERM would wait for its running CI jobs).
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill -INT "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  if [[ -n "$startup_log" ]]; then rm -f "$startup_log"; fi
}
trap cleanup EXIT
# Set before the build: a TERM during it ends the launcher once the running
# step returns, before any server starts.
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$crate_root"
for command in cargo npm git; do
  if ! type -P "$command" >/dev/null; then
    echo "Required command not found: $command (see README requirements)." >&2
    exit 1
  fi
done

if [[ ! -f web/node_modules/.package-lock.json || web/package-lock.json -nt web/node_modules/.package-lock.json ]]; then
  npm --prefix web ci
fi
npm --prefix web run build
target="$workspace_root/target"
# Through the development image's cargo, which cleans a target directory built
# under other build conditions first (in the image, `cargo` is that wrapper).
REAL_CARGO="${REAL_CARGO:-$(type -P cargo)}" "$workspace_root/docker/agent-environment/cargo-wrapper.sh" \
  build --manifest-path "$crate_root/Cargo.toml" --target-dir "$target"
binary="$target/debug/branvil"

if [[ ! -f "$data_dir/branvil.redb" ]]; then
  "$binary" init --data-dir "$data_dir" --owner "$owner"
fi

startup_log="$(mktemp "${TMPDIR:-/tmp}/branvil-dev.XXXXXX")"
# The service flushes this line only after startup recovery is complete.
# Watching our child's output avoids mistaking another server for this one.
"$binary" serve --data-dir "$data_dir" >"$startup_log" &
server_pid=$!
deadline=$((SECONDS + 60))
until grep -q '^branvil listening on ' "$startup_log"; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    cat "$startup_log"
    wait "$server_pid" || exit $?
    echo "Branvil exited before it was ready." >&2
    exit 1
  fi
  if (( SECONDS >= deadline )); then
    echo "Branvil did not finish starting within 60 seconds." >&2
    exit 1
  fi
  sleep 0.2
done
cat "$startup_log"
echo "Branvil is running. Press Ctrl-C to stop."

if "$open_browser"; then
  # Use this data directory's owner token and configured URL even when the
  # calling shell has credentials for a different Branvil instance.
  (
    unset BRANVIL_TOKEN BRANVIL_URL
    if ! "$binary" open --data-dir "$data_dir"; then
      echo "Could not open the browser; the server is still running. Open this single-use link within 60 seconds:" >&2
      "$binary" open --print --data-dir "$data_dir" >&2 || true
    fi
  )
fi

wait "$server_pid"
