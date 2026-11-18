//! Integration tests share one executable; select a file with nextest's `--test member -E 'test(/^<file>::/)'`.

mod support;

mod capabilities;
mod chat;
mod control;
mod git_workspaces;
mod member_code;
mod records;
mod session;

/// The ordinary Branvil hook, dispatched by this libtest executable.
#[test]
fn hook() {
    branvil::test_support::hook();
}
