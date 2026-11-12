#!/usr/bin/env bash
# Run the nearest repository hook, then Cargo. REAL_CARGO names the toolchain
# proxy even when the caller chooses a different writable CARGO_HOME.
set -u

hook_dir="$PWD"
while :; do
  if [[ -f "$hook_dir/.cargo/before-cargo.sh" ]]; then
    bash "$hook_dir/.cargo/before-cargo.sh" "$@"
    break
  fi
  [[ "$hook_dir" == / ]] && break
  hook_dir=$(dirname "$hook_dir")
done
exec "$REAL_CARGO" "$@"
