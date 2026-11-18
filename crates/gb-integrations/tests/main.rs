//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

mod diagnostic_inventory;
mod factory;
mod scope;

mod member_code;
