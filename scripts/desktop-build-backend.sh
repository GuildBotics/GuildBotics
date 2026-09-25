#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-$("$SCRIPT_DIR/desktop-target.sh")}"
case "$DESKTOP_TARGET" in
  aarch64-apple-darwin) export GUILDBOTICS_PYINSTALLER_TARGET_ARCH=arm64 ;;
  x86_64-apple-darwin) export GUILDBOTICS_PYINSTALLER_TARGET_ARCH=x86_64 ;;
  *) unset GUILDBOTICS_PYINSTALLER_TARGET_ARCH ;;
esac
PROGRAMS_DIR="desktop/src-tauri/binaries/guildbotics"

cd "$REPO_ROOT"

uv sync --extra test --extra dev

uv run --with pyinstaller python -m PyInstaller \
  desktop/sidecar/guildbotics.spec \
  --noconfirm --clean \
  --distpath dist --workpath build/sidecar

rm -rf "$PROGRAMS_DIR"
mkdir -p "$(dirname "$PROGRAMS_DIR")"
cp -R dist/guildbotics "$PROGRAMS_DIR"
# The desktop app installs the CLI once per build id.
uv run --no-sync python -c 'import uuid; print(uuid.uuid4())' >"$PROGRAMS_DIR/build-id"

echo "Built desktop programs: $PROGRAMS_DIR"
