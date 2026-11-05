//! GuildBotics' configuration as running a command reads it, carried over
//! from the Python version piece by piece: the team, which file a command
//! name runs with what that command declares, and what its isolated
//! environment boots with.

pub mod agent_environment;
pub mod command_files;
pub mod commands;
pub mod credential_vault;
mod python_literal;
pub mod repository;
pub mod resolved_guest;
pub mod secrets;
pub mod team;
mod yaml;

/// Parse configuration with the same scalar semantics as all host readers.
pub fn yaml_value(text: &str) -> anyhow::Result<serde_json::Value> {
    yaml::value(text).map_err(|_| anyhow::anyhow!("Invalid configuration YAML"))
}

/// The configuration file whose read or validation failed.
#[derive(Debug)]
pub struct ConfigFile(pub std::path::PathBuf);
impl std::fmt::Display for ConfigFile {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.display().fmt(f)
    }
}
impl std::error::Error for ConfigFile {}
