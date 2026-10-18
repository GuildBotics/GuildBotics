#!/usr/bin/env bash
# The image's `cargo`: removes a target directory built under other build
# conditions, then runs the real cargo ($CARGO_HOME/bin/cargo) with the same
# arguments.
#
# Cargo names its outputs after the compiler and the profile settings, so when
# either changes it builds everything again under new names and leaves the old
# outputs behind; in a member's clone they pile up until a build runs out of
# disk. Inside a Cargo project this keeps the conditions (`rustc -vV` and the
# `[profile.*]` tables of the workspace's Cargo.toml) in the target directory
# Cargo reports, and runs `cargo clean` when they differ from the current ones
# or when the directory has no record. Outside a project, or when Cargo cannot
# describe it, it only runs the real cargo. Cleaning is housekeeping: it never
# stops the command, and since `cargo clean` takes no lock, two runs started
# together on an unmarked target may both clean.
set -u

cargo="${CARGO_HOME}/bin/cargo"

clean_stale_target() {
  local metadata target root manifest mark conditions
  metadata="$("$cargo" metadata --no-deps --format-version 1 2>/dev/null)" || return
  {
    IFS= read -r target && IFS= read -r root
  } < <(python3 -c 'import json, sys
metadata = json.load(sys.stdin)
print(metadata["target_directory"])
print(metadata["workspace_root"])' <<<"$metadata") || return
  manifest="$root/Cargo.toml"
  mark="$target/.build-conditions"
  conditions="$(rustc -vV && awk '/^[[:space:]]*\[/ { profile = /^[[:space:]]*\[profile\./ } profile' "$manifest")" || return
  if [[ -f "$mark" && "$(<"$mark")" == "$conditions" ]]; then
    return
  fi
  if [[ -e "$target" ]]; then
    echo "Build conditions of $target changed or are unknown; running cargo clean." >&2
    "$cargo" clean --manifest-path "$manifest" --target-dir "$target" || return
  fi
  mkdir -p "$target" && printf '%s\n' "$conditions" >"$mark"
}

clean_stale_target
exec "$cargo" "$@"
