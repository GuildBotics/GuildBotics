#!/usr/bin/env bash
# Build the agent environment base image for developing GuildBotics
# (docker/agent-environment/Dockerfile) for every architecture the
# workspace's devices run, load the one this device runs, and declare them
# all in the workspace's shared environment declaration.
#
# Usage: scripts/build-agent-environment-image.sh [REFERENCE]
#
# REFERENCE is the image name the declaration uses (default:
# guildbotics/agent-environment:dev). The image is per CPU architecture: the
# environment runs the device's own CPU, so one archive is built per
# platform in PLATFORMS (default: linux/arm64,linux/amd64; a Docker that
# builds foreign platforms through emulation is enough), each saved as
# <ARCHIVE_DIR>/<reference>-<arch>.tar (default ARCHIVE_DIR:
# dist/agent-environment). The archive for this device's architecture is
# loaded here with `guildbotics environment image load`; the declaration is
# then given every architecture's digest with `guildbotics environment image
# declare`, so the other devices only have to load their archive (the path to
# copy is printed at the end). Devices of another architecture run with the
# image they last loaded, and say so, until they load the new archive.
#
# GUILDBOTICS_CLI names the CLI that loads and declares (default: the
# Desktop's managed CLI, or `guildbotics` on PATH); set it to `uv run
# --no-sync guildbotics` to use this checkout's own CLI. Extra arguments for
# it (such as `--workspace <dir>`) go in GUILDBOTICS_CLI_ARGS.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
reference="${1:-guildbotics/agent-environment:dev}"
context="${repo_root}/docker/agent-environment"
platforms="${PLATFORMS:-linux/arm64,linux/amd64}"
archive_dir="${ARCHIVE_DIR:-${repo_root}/dist/agent-environment}"

if command -v docker >/dev/null 2>&1; then
    builder=docker
elif command -v podman >/dev/null 2>&1; then
    builder=podman
else
    echo "docker or podman is required to build the image" >&2
    exit 1
fi

if [ -n "${GUILDBOTICS_CLI:-}" ]; then
    guildbotics="${GUILDBOTICS_CLI}"
elif [ -x "${HOME}/.guildbotics/bin/guildbotics" ]; then
    guildbotics="${HOME}/.guildbotics/bin/guildbotics"
else
    guildbotics=guildbotics
fi
# shellcheck disable=SC2086 # GUILDBOTICS_CLI and its args may carry words
cli() { ${guildbotics} environment ${GUILDBOTICS_CLI_ARGS:-} "$@"; }

case "$(uname -m)" in
    x86_64 | amd64) device_arch=amd64 ;;
    aarch64 | arm64) device_arch=arm64 ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "${archive_dir}"
archive_base="${archive_dir}/$(echo "${reference}" | tr '/:' '__')"
declare_args=()
foreign_archives=()
native_archive=""

for platform in ${platforms//,/ }; do
    arch="${platform#*/}"
    # The same reference for every platform: the archive's own tag is then
    # the declared reference, and the device that loads it needs no --tag.
    "${builder}" build --platform "${platform}" --tag "${reference}" \
        --file "${context}/Dockerfile" "${context}"
    digest="$("${builder}" image inspect --format '{{.Id}}' "${reference}")"
    archive="${archive_base}-${arch}.tar"
    "${builder}" save --output "${archive}" "${reference}"
    echo "saved ${archive} (${arch} ${digest})"
    declare_args+=(--digest "${arch}=${digest}")
    if [ "${arch}" = "${device_arch}" ]; then
        native_archive="${archive}"
    else
        foreign_archives+=("${archive}")
    fi
done

if [ -n "${native_archive}" ]; then
    cli image load "${native_archive}"
fi
cli image declare "${reference}" "${declare_args[@]}"

for archive in "${foreign_archives[@]}"; do
    echo "copy ${archive} to a device of that architecture and run there:"
    echo "  guildbotics environment image load $(basename "${archive}")"
done
