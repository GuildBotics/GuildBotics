//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

mod archive;
mod contract;
mod device;
mod golden;
mod image;
mod image_sweep;
mod shape;
mod snapshot;
mod worktree;
