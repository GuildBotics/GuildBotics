//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

mod chat;
mod contracts;
mod diagnostics;
