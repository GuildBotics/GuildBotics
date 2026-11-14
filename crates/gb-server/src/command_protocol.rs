//! Command submission wire contract.
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
/// History preparation includes provider calls and the member Git lock wait.
pub(crate) const HISTORY_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(60);
#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RunRequest {
    #[serde(default)]
    pub(crate) preflight: bool,
    #[serde(default)]
    pub(crate) remotes: std::collections::BTreeMap<String, String>,
    #[serde(default)]
    pub(crate) client_cwd: Option<PathBuf>,
    pub(crate) upload: Option<gb_environment::worktree::transfer::Upload>,
    pub(crate) command: String,
    #[serde(default)]
    pub(crate) args: Vec<String>,
    pub(crate) person: Option<String>,
    #[serde(default)]
    pub(crate) message: String,
    pub(crate) cwd: Option<PathBuf>,
    #[serde(default)]
    pub(crate) input_ids: Vec<String>,
    /// The workspace the caller means, which must still be the selected one.
    pub(crate) expected_workspace: Option<String>,
    pub(crate) request_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) expected_command_file_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) expected_command_file_revision: Option<String>,
}
