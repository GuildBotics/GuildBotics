#!/usr/bin/env bash
# Repository-owned Cargo cache housekeeping. Failure must not stop Cargo.
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
  mark="$target/.build-conditions-v2"
  conditions="$(rustc -vV && awk '/^[[:space:]]*\[/ { profile = /^[[:space:]]*\[profile\./ } profile' "$manifest")" || return
  if [[ -f "$mark" && "$(<"$mark")" == "$conditions" ]]; then
    clean_workspace_on_change
    return
  fi
  if [[ -e "$target" ]]; then
    echo "Build conditions of $target changed or are unknown; running cargo clean." >&2
    as_the_command clean >&2 || return
  fi
  # The target directory does not exist here. Cargo tags one it creates as a
  # cache (https://bford.info/cachedir/), and on macOS leaves it out of Time
  # Machine's backups, but leaves one that exists alone, so both are done along
  # with the directory.
  mkdir -p "$target" || return
  if type -P tmutil >/dev/null; then
    tmutil addexclusion "$target" >&2
  fi
  printf '%s\n' "Signature: 8a477f597d28d172789f06886806bc55" \
    "# This file is a cache directory tag created by cargo." \
    "# For information about cache directory tags see https://bford.info/cachedir/" \
    >"$target/CACHEDIR.TAG" &&
    printf '%s\n' "$conditions" >"$mark" &&
    workspace_state >"$target/.workspace-conditions"
}

# The branch is intentionally empty for detached HEAD. Hash the lockfile without
# printing or depending on its contents in the stamp.
workspace_state() {
  git -C "$root" symbolic-ref --quiet --short HEAD 2>/dev/null || true
  python3 -c 'import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1]) / "Cargo.lock"
print(hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing")' "$root"
}

clean_workspace_on_change() {
  local state stamp="$target/.workspace-conditions"
  state="$(workspace_state)" || return
  [[ -f "$stamp" && "$(<"$stamp")" == "$state" ]] && return
  as_the_command clean --workspace >&2 || return
  # Cargo can leave old hashes of renamed targets and test executables behind.
  # Match workspace target names only; never discard third-party dependencies.
  python3 - "$target" "$metadata" <<'PRUNE'
import json, pathlib, re, shutil, sys
root = pathlib.Path(sys.argv[1])
metadata = json.loads(sys.argv[2])
members = set(metadata["workspace_members"])
names = {t["name"].replace("-", "_")
         for p in metadata["packages"] if p["id"] in members
         for t in p["targets"]}
for deps in [*root.glob("*/deps"), *root.glob("*/*/deps")]:
    for path in deps.iterdir():
        names_on_disk = {path.name, re.sub(r"^lib", "", path.name)}
        if any(re.fullmatch(re.escape(n) + r"-[0-9a-f]+(?:\..*)?", name)
               for n in names for name in names_on_disk):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
for incremental in [*root.glob("*/incremental"), *root.glob("*/*/incremental")]:
    for path in incremental.iterdir():
        if any(path.name.startswith(n + "-") for n in names | {"build_script_build"}):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
PRUNE
  [[ $? == 0 ]] && printf '%s\n' "$state" >"$stamp"
}

clean_stale_target "$@"
