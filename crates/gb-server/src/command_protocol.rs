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
    #[cfg(feature = "server")]
    #[serde(skip)]
    pub(crate) remote_copy: bool,
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

#[cfg(test)]
mod tests {
    #[test]
    fn remote_copy_is_server_state_only() {
        let body = serde_json::json!({"command": "probe"});
        let request: super::RunRequest = serde_json::from_value(body.clone()).unwrap();
        #[cfg(feature = "server")]
        assert!(!request.remote_copy);
        assert!(
            serde_json::to_value(request)
                .unwrap()
                .get("remote_copy")
                .is_none()
        );
        let mut untrusted = body;
        untrusted["remote_copy"] = true.into();
        assert!(serde_json::from_value::<super::RunRequest>(untrusted).is_err());
    }
}
