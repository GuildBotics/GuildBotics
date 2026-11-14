//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

#[cfg(feature = "host")]
mod archive;
mod contract;
#[cfg(feature = "host")]
mod device;
#[cfg(feature = "host")]
mod golden;
#[cfg(feature = "host")]
mod image;
#[cfg(feature = "host")]
mod image_sweep;
#[cfg(feature = "host")]
mod shape;
#[cfg(feature = "host")]
mod snapshot;
mod worktree;
