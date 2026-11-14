//! The interactive member client and server wire contract.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::OnceLock;
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
/// Compare the executable builds, even when they share a package version.
pub fn binary_version() -> anyhow::Result<String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    static VERSION: OnceLock<String> = OnceLock::new();
    if let Some(version) = VERSION.get() {
        return Ok(version.clone());
    }
    let mut file = std::fs::File::open(std::env::current_exe()?)?;
    let mut hasher = Sha256::new();
    let mut bytes = [0; 65536];
    loop {
        let count = file.read(&mut bytes)?;
        if count == 0 {
            break;
        }
        hasher.update(&bytes[..count]);
    }
    let hash = hasher
        .finalize()
        .iter()
        .map(|v| format!("{v:02x}"))
        .collect::<String>();
    let version = format!("{}+{hash}", env!("CARGO_PKG_VERSION"));
    let _ = VERSION.set(version.clone());
    Ok(version)
}
