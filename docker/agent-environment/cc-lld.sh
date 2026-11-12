#!/bin/sh
# Link through cc with the rust-lld of the toolchain rustup runs here, as
# x86_64-unknown-linux-gnu does by default.
exec cc -fuse-ld=lld \
    -B"$(rustc --print sysroot)/lib/rustlib/aarch64-unknown-linux-gnu/bin/gcc-ld" "$@"
