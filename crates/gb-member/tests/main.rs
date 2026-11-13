//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

mod support;

mod capabilities;
mod chat;
mod control;
mod documents;
mod git_workspaces;
mod records;
mod reference;
mod session;
