# The base image of the agent environment for developing GuildBotics itself.
#
# GuildBotics' default base image (Debian with curl, the compilers and git) lets an
# agent run the AI CLI tools but not this repository's own build and tests.
# This image adds everything the Linux CI uses, so a member turn inside the
# isolated environment can run the same checks as `.branvil/ci.toml`, whose
# jobs run in this image:
#
#   - Python (uv-managed; the version `requires-python` allows) for `uv sync`,
#     ruff, mypy, pylint and pytest
#   - Node.js 24, as the Desktop workflows run it, for the frontend checks of
#     the CI jobs (in the environment snapshot, the build's own Node.js, at the
#     version GuildBotics pins, replaces it)
#   - the Rust toolchain for the Cargo workspace's checks, with cargo-nextest
#     for its tests; `cargo` is cargo-wrapper.sh, which calls the nearest
#     repository-owned .cargo/before-cargo.sh before running the real cargo
#   - sccache, through which rustc runs (RUSTC_WRAPPER), keeping compilation
#     results by their inputs up to SCCACHE_CACHE_SIZE. A member's writable
#     command binds the workspace's
#     .guildbotics/local/build-cache/<person_id>/sccache as SCCACHE_DIR, so
#     the member's next clone reuses its dependencies' compilation; a
#     read-only command keeps the cache on the microVM's own disk, which is
#     discarded with it. The CI's Rust jobs build without it (an empty
#     RUSTC_WRAPPER in `.branvil/ci.toml`). For the clones to share results,
#     aarch64 links through the repository's linker script copied to a fixed
#     path (cc-lld.sh)
#   - Playwright's Chromium with its system libraries for `npm run e2e`
#   - lychee, for the Markdown link check
#
# It keeps what GuildBotics' build recipe expects of a base image: curl and
# tar, gcc, g++ and make, and no bubblewrap (see the last step). GuildBotics
# installs Node.js and the AI CLI tools on top when it builds the environment
# snapshot.
#
# `scripts/build-agent-environment-image.sh` builds it for every architecture
# the workspace's devices run, loads it on the building device and into the
# CI runner of the workspace's code host, and declares the digests in the
# workspace's environment declaration (`image:` in
# `config/intelligences/agent_environment.yml`); the other devices load the
# archive it prints. The image is per CPU architecture: the environment runs
# the device's own CPU, so the declaration names one digest per
# architecture. The turn's
# HOME is the environment's own /opt/guildbotics/home, not the one this image
# was built with, so nothing below lives under /root: interpreters and toolchains sit under /opt
# and are reached through /usr/local/bin and the ENV settings, which the
# environment passes to every process it starts.
FROM node:24.22.1-bookworm

ARG UV_VERSION=0.13.5
ARG PYTHON_VERSION=3.15
# The channel of rust-toolchain.toml: the jobs cannot install another one.
ARG RUST_VERSION=1.96.0
ARG NEXTEST_VERSION=0.9.148
# The release's musl archives, verified against their published SHA-256.
ARG SCCACHE_VERSION=0.19.0
ARG SCCACHE_SHA256_AARCH64=3e47fc4a71b1ddd1b4294a0de9d34498922fdf41e3cd0bda5df3954e3d6deb9d
ARG SCCACHE_SHA256_X86_64=aa73c7270c997894038c0d8f0d297865f57569e1687883322bc431713d4bb9dd
ARG LYCHEE_VERSION=0.24.2
# Keep in step with `@playwright/test` in package-lock.json; a newer
# lock file makes `npm run e2e:install` fetch the matching browser at run time.
ARG PLAYWRIGHT_VERSION=1.62.1

ENV DEBIAN_FRONTEND=noninteractive

# Build tools. The microVM runtime crate (`microsandbox`) links libcap-ng on
# Linux.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libssl-dev \
        libcap-ng-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# uv, at the version GuildBotics' recipe installs, and a managed Python kept
# outside any home directory. `python3.15` on PATH is how `uv sync` finds it
# even without the ENV; Debian's own python3 (3.11) is left alone.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN arch="$(uname -m)" \
    && archive="uv-${arch}-unknown-linux-gnu" \
    && curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${archive}.tar.gz" \
        | tar -xz -C /usr/local/bin --strip-components=1 "${archive}/uv" "${archive}/uvx" \
    && uv python install "${PYTHON_VERSION}" \
    && python="$(uv python find "${PYTHON_VERSION}")" \
    && ln -s "${python}" "/usr/local/bin/python${PYTHON_VERSION}" \
    && "python${PYTHON_VERSION}" --version

# The Rust toolchain, with rustfmt and clippy, outside any home directory. The
# rustup proxies are linked into /usr/local/bin and find the toolchain through
# RUSTUP_HOME; cargo keeps its registry under CARGO_HOME. `cargo` itself is
# cargo-wrapper.sh, which cleans a target directory built under other build
# conditions before running the rustup proxy named by REAL_CARGO.
ENV RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    REAL_CARGO=/opt/cargo/bin/cargo
RUN curl -fsSL https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --profile minimal \
            --default-toolchain "${RUST_VERSION}" --component rustfmt --component clippy \
    && for tool in rustc rustup rustfmt cargo-fmt cargo-clippy clippy-driver; do \
        ln -s "/opt/cargo/bin/${tool}" "/usr/local/bin/${tool}"; \
    done \
    && rustfmt --version
# After rustup: its installer refuses to install over a `cargo` on PATH.
COPY --chmod=0755 cargo-wrapper.sh /usr/local/bin/cargo
RUN cargo --version

# cargo-nextest, which runs each test in a process of its own, for the CI's
# Rust test jobs. On PATH rather than under CARGO_HOME: the jobs point
# CARGO_HOME at their cache, and cargo finds `cargo nextest` on PATH either way.
RUN arch="$(uname -m)" \
    && curl -fsSL "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-${NEXTEST_VERSION}/cargo-nextest-${NEXTEST_VERSION}-${arch}-unknown-linux-gnu.tar.gz" \
        | tar -xz -C /usr/local/bin cargo-nextest \
    && cargo nextest --version

# sccache, as rustc's wrapper for every build in the environment. The cache
# is bounded below the write quota GuildBotics gives its bound directory
# (`SCCACHE_QUOTA_MIB` in crates/gb-environment/src/contract.rs).
RUN arch="$(uname -m)" \
    && case "${arch}" in \
        aarch64) sha256="${SCCACHE_SHA256_AARCH64}" ;; \
        x86_64) sha256="${SCCACHE_SHA256_X86_64}" ;; \
        *) echo "no sccache checksum for ${arch}" >&2; exit 1 ;; \
    esac \
    && archive="sccache-v${SCCACHE_VERSION}-${arch}-unknown-linux-musl" \
    && curl -fsSL -o /tmp/sccache.tar.gz "https://github.com/mozilla/sccache/releases/download/v${SCCACHE_VERSION}/${archive}.tar.gz" \
    && echo "${sha256}  /tmp/sccache.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/sccache.tar.gz -C /usr/local/bin --strip-components=1 "${archive}/sccache" \
    && rm /tmp/sccache.tar.gz \
    && sccache --version
# The repository's aarch64 linker (.cargo/cc-lld.sh), at a path that is the
# same in every checkout: sccache keys a compilation by rustc's arguments,
# and Cargo passes the linker's absolute path to every crate's rustc, so the
# repository's own copy would make each clone's results its own.
COPY --chmod=0755 cc-lld.sh /usr/local/bin/cc-lld
ENV RUSTC_WRAPPER=sccache \
    SCCACHE_CACHE_SIZE=4G \
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=/usr/local/bin/cc-lld

# lychee, for the CI's markdown-links job. The static musl
# build: the x86_64 glibc build wants a newer glibc than bookworm has.
RUN arch="$(uname -m)" \
    && archive="lychee-${arch}-unknown-linux-musl" \
    && curl -fsSL "https://github.com/lycheeverse/lychee/releases/download/lychee-v${LYCHEE_VERSION}/${archive}.tar.gz" \
        | tar -xz -C /usr/local/bin --strip-components=1 "${archive}/lychee" \
    && lychee --version

# Playwright's Chromium and the system libraries it needs, outside any home
# directory so `npm run e2e` finds it at the turn's HOME too.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN npx --yes "playwright@${PLAYWRIGHT_VERSION}" install --with-deps chromium \
    && npm cache clean --force \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Codex enforces its permission profile with the bubblewrap it bundles, but
# prefers one installed in the image, and Debian's 0.8.0 cannot exec Codex's
# helper: every Codex session then fails to start. Nothing above installs it;
# this fails the build if a package added later pulls it in.
RUN ! command -v bwrap
