#!/bin/sh
# Check that the `branvil` provider and the Branvil crate of this checkout
# still work together: tickets and their relations, a push with a pull request
# and its merge, and checks deciding readiness, each against its own temporary
# `branvil serve` (debug build). See integration/branvil/test_smoke.py.
#
#   scripts/branvil-integration.sh [pytest args...]
#
# The checkout must have its environment synced
# (`uv sync --extra test --extra dev`). It prints one line per path.
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
cd "$here"
cargo build --quiet --package branvil
target=$(cargo metadata --quiet --format-version 1 --no-deps |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')

# A short traceback: the long one prints the arguments of the failing calls,
# the services' tokens among them.
BRANVIL_BIN="$target/debug/branvil" \
  exec .venv/bin/python -I -m pytest -p no:cacheprovider -v --tb=short \
    -o timeout=300 integration/branvil/test_smoke.py "$@"
