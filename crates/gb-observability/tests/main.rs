//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

mod boundaries;
mod correlation;
mod golden;
mod health;
mod record_reads_golden;
mod resolution;
mod routing;
mod screens;
mod shared_records;
