#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

# The backend listens on 127.0.0.1 at GUILDBOTICS_SERVER_PORT (8765 unless
# set), the port desktop-dev-frontend.sh proxies `/api` to. Enter its UI with
# an entry link from `guildbotics open`.
cd "$REPO_ROOT"

if command -v uv >/dev/null 2>&1; then
  exec uv run --no-sync python -m guildbotics.app_api
else
  exec python3 -m guildbotics.app_api
fi
