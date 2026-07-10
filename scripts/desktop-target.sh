#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${DESKTOP_TARGET:-}" ]]; then
  printf '%s\n' "$DESKTOP_TARGET"
  exit 0
fi

if command -v rustc >/dev/null 2>&1; then
  if target="$(rustc -vV 2>/dev/null | awk '$1 == "host:" { print $2 }')" && [[ -n "$target" ]]; then
    printf '%s\n' "$target"
    exit 0
  fi
fi

case "$(uname -s):$(uname -m)" in
  Darwin:arm64) printf '%s\n' 'aarch64-apple-darwin' ;;
  Darwin:x86_64) printf '%s\n' 'x86_64-apple-darwin' ;;
  Linux:x86_64) printf '%s\n' 'x86_64-unknown-linux-gnu' ;;
  Linux:aarch64 | Linux:arm64) printf '%s\n' 'aarch64-unknown-linux-gnu' ;;
  *)
    echo 'Could not determine the desktop target. Set DESKTOP_TARGET explicitly.' >&2
    exit 1
    ;;
esac
