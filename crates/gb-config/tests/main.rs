//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

mod agent_environment;
mod golden;
mod repository;
mod repository_golden;
mod screens;
mod secrets;
