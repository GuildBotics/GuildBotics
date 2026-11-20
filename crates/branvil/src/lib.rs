//! Branvil: the code hosting (Git, tickets, pull requests, CI) of GuildBotics.
//!
//! The `guildbotics` executable prepares the service in its server
//! (`prepare_embedded`), with its authentication (`host::Host`) and its vault
//! (`vault::Vault`), and runs the runner, the Git hooks, and the maintenance
//! of the data as its own commands.

pub mod agent;
pub mod api;
mod app;
mod auth;
mod autoland;
mod backup;
mod ci;
mod config;
mod domain;
mod error;
pub mod events;
mod git;
mod hooks;
pub mod host;
mod http;
pub mod hub;
mod landing;
mod model;
mod rank;
mod references;
mod refs;
pub mod repair;
mod runner;
mod service;
mod smart_http;
mod store;
#[cfg(any(test, feature = "test-support"))]
pub mod test_support;
mod upgrade;
mod upstream;
mod util;
pub mod vault;
mod webhook;

#[cfg(feature = "host")]
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};

pub use backup::{request as request_backup, restore};
pub use runner::{RunnerArgs, main as run_runner};
pub use service::{Embedded, Maintenance, maintain};
pub use upstream::askpass_main;

/// Prepare the service on `data_dir`, creating it on first use, without
/// binding. All advertised URLs use the host origin `base_url`.
#[cfg(feature = "host")]
pub async fn prepare_embedded(
    data_dir: &Path,
    vm_home: PathBuf,
    vault: Arc<dyn vault::Vault>,
    base_url: &str,
    host: Arc<dyn host::Host>,
    active_agents: agent::ActiveAgents,
) -> anyhow::Result<Embedded> {
    let base_url = config::normalize_base_url(base_url)?;
    let embedded = service::prepare(
        service::load_config(data_dir)?,
        vault,
        base_url.clone(),
        base_url,
        active_agents,
    )
    .await?;
    let _ = embedded.app.host.set(host);
    let _ = embedded.app.ci.settings().vm_home.set(vm_home);
    Ok(embedded)
}

/// `guildbotics server git-hook <name>`, which Git runs inside a push.
pub async fn git_hook(name: &str) -> u8 {
    hooks::run_hook(name).await as u8
}

/// The HTTP route inventory, independent of the embedding host.
pub use http::route_inventory;

/// Exact hook paths whose shared-secret authentication replaces Host/Origin checks.
pub fn hook_paths() -> impl Iterator<Item = &'static str> {
    http::HOOK_ROUTES.iter().map(|route| route.path)
}
