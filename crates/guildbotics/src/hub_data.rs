//! Offline data boundary for Hub updates. Archive paths never select live roots.

use std::path::PathBuf;

#[derive(Clone, Copy, clap::ValueEnum)]
pub enum Operation {
    Backup,
    Check,
    Upgrade,
}

pub async fn run(
    operation: Operation,
    archive: Option<PathBuf>,
    to: Option<PathBuf>,
) -> anyhow::Result<()> {
    let language = gb_server::client::language().await;
    let operation = match operation {
        Operation::Backup => gb_server::hub_data::Operation::Backup,
        Operation::Check => gb_server::hub_data::Operation::Check,
        Operation::Upgrade => gb_server::hub_data::Operation::Upgrade,
    };
    gb_server::hub_data::execute(operation, archive.as_deref(), to.as_deref()).map_err(|error| {
        anyhow::anyhow!(gb_server::i18n::t(
            language,
            "runtime.service_lock.hub_operation_failed",
            &[("reason", &error.to_string())]
        ))
    })
}
