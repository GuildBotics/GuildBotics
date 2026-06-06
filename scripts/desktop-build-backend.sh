#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
DESKTOP_TARGET="${DESKTOP_TARGET:-aarch64-apple-darwin}"
SIDECAR_NAME="guildbotics-app-api-${DESKTOP_TARGET}"

cd "$REPO_ROOT"

uv sync --extra test --extra dev

uv run --with pyinstaller python -m PyInstaller \
  desktop/sidecar/guildbotics-app-api.spec \
  --noconfirm --clean \
  --distpath dist --workpath build/sidecar

mkdir -p desktop/src-tauri/binaries
cp dist/guildbotics-app-api "desktop/src-tauri/binaries/${SIDECAR_NAME}"
chmod +x "desktop/src-tauri/binaries/${SIDECAR_NAME}"

echo "Built backend sidecar: desktop/src-tauri/binaries/${SIDECAR_NAME}"
