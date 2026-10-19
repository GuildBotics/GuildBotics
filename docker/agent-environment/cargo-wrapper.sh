#!/usr/bin/env bash
# The image's `cargo`: removes a target directory built under other build
# conditions, then runs the real cargo with the same arguments. The image names
# the real cargo in REAL_CARGO rather than leaving it to CARGO_HOME, which a
# command may point elsewhere (a registry it can write to).
#
# Cargo names its outputs after the compiler and the profile settings, so when
# either changes it builds everything again under new names and leaves the old
# outputs behind; in a member's clone they pile up until a build runs out of
# disk. This keeps the conditions (`rustc -vV` and the `[profile.*]` tables of
# the workspace's Cargo.toml) in the target directory, and runs `cargo clean`
# when they differ from the current ones or when the directory has no record.
# Cargo itself finds the workspace and the directories for both, given the
# command's own options that choose them, so they are the ones the command
# builds in. When there is no such workspace, or Cargo cannot describe it, it
# only runs the real cargo. Options that an alias in Cargo's configuration adds,
# or hands to the program it runs, are not seen. Cleaning is housekeeping: it never
# stops the command, and since `cargo clean` takes no lock, two runs started
# together on an unmarked target may both clean.
set -u

cargo="$REAL_CARGO"

# The real cargo, finding the workspace and the directories as the command
# does: with its --manifest-path and --config options, and its --target-dir
# through the variable (`cargo metadata` takes no such option), set for this
# call only.
as_the_command() {
  env ${target_dir:+"CARGO_TARGET_DIR=$target_dir"} "$cargo" "$@" ${options[@]+"${options[@]}"}
}

clean_stale_target() {
  local metadata target root manifest mark conditions options=() target_dir=
  # The options before `--` that choose the workspace and the directories;
  # what follows `--` is the built program's.
  while (($#)); do
    case $1 in
      --) break ;;
      --manifest-path | --config) options+=("$1" "${2-}") && shift ;;
      --manifest-path=* | --config=*) options+=("$1") ;;
      --target-dir) target_dir=${2-} && shift ;;
      --target-dir=*) target_dir=${1#*=} ;;
    esac
    shift
  done
  metadata="$(as_the_command metadata --no-deps --format-version 1 2>/dev/null)" || return
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
    as_the_command clean >&2 || return
  fi
  # The target directory does not exist here. Cargo tags one it creates as a
  # cache (https://bford.info/cachedir/) and leaves one that exists alone, so
  # the tag is written along with the directory.
  mkdir -p "$target" &&
    printf '%s\n' "Signature: 8a477f597d28d172789f06886806bc55" \
      "# This file is a cache directory tag created by cargo." \
      "# For information about cache directory tags see https://bford.info/cachedir/" \
      >"$target/CACHEDIR.TAG" &&
    printf '%s\n' "$conditions" >"$mark"
}

clean_stale_target "$@"
exec "$cargo" "$@"
