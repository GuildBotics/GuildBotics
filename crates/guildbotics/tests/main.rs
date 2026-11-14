//! Integration tests share one executable; select a file with nextest's `--test main -E 'test(/^<file>::/)'`.

#[cfg(feature = "hub")]
mod cli_transport;
#[cfg(feature = "hub")]
mod diagnostics_cli;
mod git_credential_cli;
#[cfg(feature = "hub")]
mod hub_server;
#[cfg(feature = "hub")]
mod image_cli;
mod install_cli;
#[cfg(feature = "hub")]
mod server;
#[cfg(feature = "hub")]
mod workspace_cli;

#[cfg(not(feature = "hub"))]
mod client_only;
