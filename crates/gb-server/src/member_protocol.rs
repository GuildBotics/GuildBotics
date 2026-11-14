//! The interactive member client and server wire contract.
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[derive(Debug, Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Identity,
    Record,
    Known,
    Push,
    Tracking,
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct MemberRequest {
    pub version: String,
    pub workspace_id: String,
    pub arguments: Vec<String>,
    #[serde(default)]
    pub stdin: String,
    #[serde(default)]
    pub interactive_host: String,
    #[serde(default)]
    pub interactive_thread_key: String,
    pub phase: Option<Phase>,
    #[serde(default)]
    pub data: serde_json::Map<String, Value>,
}
#[derive(Default, Deserialize, Serialize)]
pub struct Result {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    #[serde(flatten)]
    pub metadata: serde_json::Map<String, Value>,
}
/// Identity of the source commit, shared across build targets.
pub fn binary_version() -> anyhow::Result<String> {
    Ok(gb_base::BUILD_VERSION.into())
}
