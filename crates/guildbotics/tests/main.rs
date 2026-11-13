//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

mod cli_transport;
mod diagnostics_cli;
mod git_credential_cli;
mod hub_server;
mod image_cli;
mod install_cli;
mod server;
mod workspace_cli;
