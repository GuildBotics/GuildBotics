# The base image of the agent environment for developing GuildBotics itself.
#
# GuildBotics' default base image (Debian with Node.js, npm, git and uv) lets an
# agent run the AI CLI tools but not this repository's own build and tests.
# This image adds everything the CI workflows use, so a member turn inside the
# isolated environment can run the same checks as `.github/workflows/*.yml`:
#
#   - Python (uv-managed; the version `requires-python` allows) for `uv sync`,
#     ruff, mypy, pylint and pytest
#   - Node.js 24, as the Desktop workflows run it, for the frontend checks
#   - the Rust toolchain and the Tauri system libraries for `cargo check` /
#     `scripts/desktop-test-rust.sh`
#   - Playwright's Chromium with its system libraries for `npm run e2e`
#   - lychee, for the Markdown link check
#
# It keeps what GuildBotics' build recipe expects of a base image: Debian's apt,
# Node.js with npm, curl and tar, and no bubblewrap (see the last step).
# GuildBotics installs the AI CLI tools and the declared packages on top when
# it builds the environment snapshot.
#
# `scripts/build-agent-environment-image.sh` builds it for every architecture
# the workspace's devices run, loads it on the building device, and declares
# the digests in the workspace's environment declaration (`image:` in
# `config/intelligences/agent_environment.yml`); the other devices load the
# archive it prints. The image is per CPU architecture: the environment runs
# the device's own CPU, so the declaration names one digest per
# architecture. The turn's
# HOME is the host's home path, not the one this image was built with, so
# nothing below lives under /root: interpreters and toolchains sit under /opt
# and are reached through /usr/local/bin and the ENV settings, which the
# environment passes to every process it starts.
FROM node:24.21.0-bookworm

ARG UV_VERSION=0.12.10
ARG PYTHON_VERSION=3.12
ARG RUST_VERSION=1.96.0
ARG LYCHEE_VERSION=0.24.2
# Keep in step with `@playwright/test` in desktop/package-lock.json; a newer
# lock file makes `npm run e2e:install` fetch the matching browser at run time.
ARG PLAYWRIGHT_VERSION=1.62.1

ENV DEBIAN_FRONTEND=noninteractive

# Build tools, and the Tauri system libraries the Linux desktop workflow installs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libssl-dev \
        libwebkit2gtk-4.1-dev \
        libgtk-3-dev \
        libayatana-appindicator3-dev \
        librsvg2-dev \
        libxdo-dev \
        patchelf \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# uv, at the version GuildBotics' recipe installs, and a managed Python kept
# outside any home directory. `python3.12` on PATH is how `uv sync` finds it
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
# RUSTUP_HOME; cargo keeps its registry under CARGO_HOME.
ENV RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo
RUN curl -fsSL https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --profile minimal \
            --default-toolchain "${RUST_VERSION}" --component rustfmt --component clippy \
    && for tool in cargo rustc rustup rustfmt cargo-fmt cargo-clippy clippy-driver; do \
        ln -s "/opt/cargo/bin/${tool}" "/usr/local/bin/${tool}"; \
    done \
    && cargo --version && rustfmt --version

# lychee, at the version the CI's markdown-links job pins. The static musl
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
# helper: every Codex session then fails to start. The Tauri libraries above
# pull the package in as a dependency, so the binary is removed here; nothing
# a turn runs needs it (WebKit's own sandbox is not exercised by `cargo check`).
RUN rm -f /usr/bin/bwrap \
    && ! command -v bwrap
