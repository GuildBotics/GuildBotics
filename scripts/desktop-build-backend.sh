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
APP_API_NAME="guildbotics-app-api-${DESKTOP_TARGET}"
CLI_NAME="guildbotics-cli-${DESKTOP_TARGET}"

cd "$REPO_ROOT"

uv sync --extra test --extra dev

uv run --with pyinstaller python -m PyInstaller \
  desktop/sidecar/guildbotics-app-api.spec \
  --noconfirm --clean \
  --distpath dist --workpath build/sidecar

uv run --with pyinstaller python -m PyInstaller \
  desktop/sidecar/guildbotics-cli.spec \
  --noconfirm --clean \
  --distpath dist --workpath build/cli

mkdir -p desktop/src-tauri/binaries
cp dist/guildbotics-app-api "desktop/src-tauri/binaries/${APP_API_NAME}"
cp dist/guildbotics-cli "desktop/src-tauri/binaries/${CLI_NAME}"
chmod +x "desktop/src-tauri/binaries/${APP_API_NAME}"
chmod +x "desktop/src-tauri/binaries/${CLI_NAME}"

echo "Built backend sidecar: desktop/src-tauri/binaries/${APP_API_NAME}"
echo "Built member CLI: desktop/src-tauri/binaries/${CLI_NAME}"
