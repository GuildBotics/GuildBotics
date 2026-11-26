//! `guildbotics server hub-data`: the Hub's records, the code host's included,
//! backed up by the running server and checked, upgraded, or reindexed with
//! the server stopped. A backup path never selects live data.

use std::path::PathBuf;

#[derive(clap::Subcommand)]
pub enum HubDataCommand {
    /// Write a backup of the running server's records into a new or empty directory
    Backup {
        /// Directory to fill, on this machine
        #[arg(long)]
        to: PathBuf,
        /// Leave out CI logs and artifacts. The running server's jobs go on; the backup's copy records them as a restore will show them: results without files, running jobs interrupted
        #[arg(long)]
        without_ci_files: bool,
    },
    /// Restore a backup into a new or empty directory and check it; check and upgrade take the result as --data-dir
    Restore {
        /// Backup directory written by `server hub-data backup`
        #[arg(long)]
        from: PathBuf,
        /// Directory to fill
        #[arg(long)]
        data_dir: PathBuf,
    },
    /// Check the records; a passing check lets the server open an upgraded code host database
    Check {
        /// A restored backup instead of this machine's records
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Upgrade the records from the previous version, then check them (stop the server and take a backup first)
    Upgrade {
        /// A restored backup instead of this machine's records
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
    /// Rebuild the code host's indexes from its records
    Reindex {
        /// A restored backup instead of this machine's records
        #[arg(long)]
        data_dir: Option<PathBuf>,
    },
}

pub async fn run(command: HubDataCommand) -> anyhow::Result<()> {
    use branvil::Maintenance;
    let language = gb_server::client::language().await;
    let result = match command {
        HubDataCommand::Backup {
            to,
            without_ci_files,
        } => gb_server::hub_data::backup(&to, without_ci_files).await,
        HubDataCommand::Restore { from, data_dir } => {
            gb_server::hub_data::restore(&from, &data_dir).await
        }
        HubDataCommand::Check { data_dir } => {
            gb_server::hub_data::maintain(Maintenance::Check, data_dir.as_deref())
        }
        HubDataCommand::Upgrade { data_dir } => {
            gb_server::hub_data::maintain(Maintenance::Upgrade, data_dir.as_deref()).and_then(
                |()| gb_server::hub_data::maintain(Maintenance::Check, data_dir.as_deref()),
            )
        }
        HubDataCommand::Reindex { data_dir } => {
            gb_server::hub_data::maintain(Maintenance::Reindex, data_dir.as_deref())
        }
    };
    result.map_err(|error| {
        anyhow::anyhow!(gb_server::i18n::t(
            language,
            "runtime.service_lock.hub_operation_failed",
            &[("reason", &format!("{error:#}"))]
        ))
    })
}
