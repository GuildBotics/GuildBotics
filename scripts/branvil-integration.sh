#!/usr/bin/env bash
# Exercise the Rust provider and the Branvil crate in the same process.
set -euo pipefail
cd "$(dirname "$0")/.."
cargo test --locked -p gb-integrations --test branvil-integration
