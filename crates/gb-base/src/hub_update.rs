//! Device-local records exchanged by the Hub server and its automatic updater.

use crate::files::{atomic_write_mode, read_if_present};
use serde::{Deserialize, Serialize};
use std::path::Path;

pub const STATE_FILE: &str = "auto-update.json";
pub const STATUS_KEY: &str = "status";
pub const FAILURE_KEY: &str = "update_failure";
const CONFIRMATION_FILE: &str = "auto-update-confirmed.json";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ManualStep {
    pub version: u64,
    pub ticket: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ManualUpdate {
    pub sha: String,
    pub release: String,
    pub steps: Vec<ManualStep>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct Failure {
    pub sha: String,
    pub stage: String,
    pub reason: String,
    pub next: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Status {
    Manual(ManualUpdate),
    Failed(Failure),
}

pub fn status(directory: &Path) -> anyhow::Result<Option<Status>> {
    #[derive(Deserialize)]
    struct State {
        status: Option<Status>,
        update_failure: Option<Status>,
    }
    read_if_present(&directory.join(STATE_FILE))?
        .map(|bytes| {
            serde_json::from_slice::<State>(&bytes)
                .map(|state| state.status.or(state.update_failure))
        })
        .transpose()
        .map(Option::flatten)
        .map_err(Into::into)
}

impl ManualUpdate {
    /// Match the recorded steps even when a newer release has the same prerequisites.
    pub fn confirmed(&self, directory: &Path) -> std::io::Result<bool> {
        let previous = read_if_present(&directory.join(CONFIRMATION_FILE))?
            .and_then(|bytes| serde_json::from_slice::<Vec<ManualStep>>(&bytes).ok());
        Ok(previous.as_ref() == Some(&self.steps))
    }

    pub fn confirm(&self, directory: &Path) -> anyhow::Result<()> {
        atomic_write_mode(
            &directory.join(CONFIRMATION_FILE),
            &serde_json::to_vec(&self.steps)?,
            Some(0o600),
        )?;
        Ok(())
    }
}
