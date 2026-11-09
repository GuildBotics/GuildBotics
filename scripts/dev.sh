#!/usr/bin/env bash
# Build the screens and the server, set Branvil up once, and open both screens.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/dev.sh [--data-dir PATH] [--owner NAME] [--no-open]

Build and run the server of this checkout -- GuildBotics and Branvil in one
process -- until Ctrl-C, on data of its own, and open both screens once it is
ready.
  --data-dir PATH  Data directory (default: ~/.gbb/dev): GuildBotics' machine
                   root in its home/ (GUILDBOTICS_HOME), Branvil's data in its
                   branvil/ (BRANVIL_DATA_DIR), and the server's log of the
                   latest start in its server.log.
  --owner NAME     Branvil's owner name when it is set up (default: owner).
  --no-open        Start the server without opening a browser.
  -h, --help       Show this help.

GuildBotics listens on GUILDBOTICS_SERVER_PORT (default here: 8866), Branvil
on the address it was set up with (127.0.0.1:7879 here). GUILDBOTICS_WORKSPACE_ROOT
selects the workspace, which the server then records its runs in. The
device's runtime, which the Python version placed, is linked under home/data
for the commands to run in. Restart to pick up code changes.
EOF
}

repo="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
data_dir="$HOME/.gbb/dev"
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
if [[ "$data_dir" != /* ]]; then data_dir="$PWD/$data_dir"; fi
workspace="${GUILDBOTICS_WORKSPACE_ROOT:-}"
if [[ -n "$workspace" && "$workspace" != /* ]]; then
  export GUILDBOTICS_WORKSPACE_ROOT="$PWD/$workspace"
fi
export GUILDBOTICS_HOME="$data_dir/home"
export BRANVIL_DATA_DIR="$data_dir/branvil"
export GUILDBOTICS_SERVER_PORT="${GUILDBOTICS_SERVER_PORT:-8866}"
# The identities are the data's own too; the workspace is the one named.
unset GUILDBOTICS_IDENTITY_ROOT GUILDBOTICS_CONFIG_DIR
# Branvil says it listens only in the log, at info.
if [[ -n "${GUILDBOTICS_LOG+set}" ]]; then
  export GUILDBOTICS_LOG="${GUILDBOTICS_LOG:+$GUILDBOTICS_LOG,}branvil::cli=info"
fi

# A Ctrl-C before the server starts ends the launcher once the running step
# returns; afterwards the launcher is the server, which gets it itself.
cd "$repo"
for command in cargo npm git; do
  if ! type -P "$command" >/dev/null; then
    echo "Required command not found: $command." >&2
    exit 1
  fi
done

if [[ ! -f node_modules/.package-lock.json || package-lock.json -nt node_modules/.package-lock.json ]]; then
  npm ci
fi
npm run build --workspace desktop --workspace crates/branvil/web
# Through the development image's cargo, which cleans a target directory built
# under other build conditions first (in the image, `cargo` is that wrapper).
REAL_CARGO="${REAL_CARGO:-$(type -P cargo)}" docker/agent-environment/cargo-wrapper.sh \
  build --manifest-path "$repo/crates/guildbotics/Cargo.toml" --target-dir "$repo/target"
binary="$repo/target/debug/guildbotics"

# The device's runtime, which only the Python version places, where the
# server looks for it: a command runs in it.
runtime="$HOME/.guildbotics/data/msb"
if [[ -d "$runtime" && ! -e "$GUILDBOTICS_HOME/data/msb" && ! -L "$GUILDBOTICS_HOME/data/msb" ]]; then
  mkdir -p "$GUILDBOTICS_HOME/data"
  ln -s "$runtime" "$GUILDBOTICS_HOME/data/msb"
fi

if [[ ! -f "$BRANVIL_DATA_DIR/branvil.redb" ]]; then
  "$binary" branvil init --data-dir "$BRANVIL_DATA_DIR" --owner "$owner" --listen 127.0.0.1:7879
fi

mkdir -p "$data_dir"
# What the server says on stdout -- only that GuildBotics listens -- and its
# log, where Branvil says it listens, which still reaches the terminal.
notice="$data_dir/server.out"
log="$data_dir/server.log"
: >"$notice"
: >"$log"

# Says when both listen, and opens their screens: in the background, as the
# launcher becomes the server. A Ctrl-C ends it too where the shell lets it;
# where it does not, a server that stopped opens no screen.
open_screens() {
  local deadline=$((SECONDS + 60))
  until grep -q '^GUILDBOTICS_APP_API_PORT=' "$notice" && grep -q 'listening on http' "$log"; do
    # Ended, even before whoever started the launcher reaps it.
    case "$(ps -o stat= -p "$server" 2>/dev/null)" in ""|Z*) return 0 ;; esac
    if (( SECONDS >= deadline )); then
      echo "The server did not say within 60 seconds that GuildBotics and Branvil listen." >&2
      return 0
    fi
    sleep 0.2
  done
  echo "GuildBotics is running on http://127.0.0.1:$GUILDBOTICS_SERVER_PORT with Branvil. Press Ctrl-C to stop."
  if ! "$open_browser"; then return 0; fi
  # A link that no browser takes is printed instead.
  "$binary" open || return 0
  # Branvil's own credentials and URL for this data directory, not the ones
  # the calling shell may hold for another instance.
  unset BRANVIL_TOKEN BRANVIL_URL
  if ! "$binary" branvil open --data-dir "$BRANVIL_DATA_DIR"; then
    echo "Could not open Branvil in the browser; the server is still running. Open this single-use link within 60 seconds:" >&2
    "$binary" branvil open --print --data-dir "$BRANVIL_DATA_DIR" >&2 || true
  fi
}
server=$$
# Not the server's child, which nothing would reap.
(open_screens &)
# The server is this process from here on: a Ctrl-C reaches it once -- the
# first waits for the commands it runs, a second cancels them -- TERM cancels
# them, and the launcher ends as it does. The log's copy outlives the signals
# that stop the server, to its last line.
exec "$binary" server start >"$notice" 2> >(trap '' INT TERM HUP; exec tee "$log" >&2)
