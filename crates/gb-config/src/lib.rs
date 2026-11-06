//! GuildBotics' configuration as running a command reads it, carried over
//! from the Python version piece by piece: the team, which file a command
//! name runs with what that command declares, and what its isolated
//! environment boots with.

pub mod agent_environment;
pub mod catalog;
pub mod command_files;
pub mod command_writes;
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
pub struct ConfigFile {
    path: std::path::PathBuf,
    reason: String,
}
impl ConfigFile {
    /// Attach the file once while preserving the original error for downcasts.
    pub fn wrap(path: impl Into<std::path::PathBuf>, error: anyhow::Error) -> anyhow::Error {
        if error.downcast_ref::<Self>().is_some() {
            return error;
        }
        let reason = format!("{error:#}");
        error.context(Self {
            path: path.into(),
            reason,
        })
    }

    pub fn path(&self) -> &std::path::Path {
        &self.path
    }

    pub fn reason(&self) -> &str {
        &self.reason
    }
}
impl std::fmt::Display for ConfigFile {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.path.display(), self.reason)
    }
}
impl std::error::Error for ConfigFile {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn config_file_keeps_reason_source_and_innermost_path() {
        let source = std::io::Error::new(std::io::ErrorKind::PermissionDenied, "permission denied");
        let error = ConfigFile::wrap(
            "member.yml",
            anyhow::Error::new(source).context("cannot read configuration"),
        );
        let error = ConfigFile::wrap("outer.yml", error);
        let file = error.downcast_ref::<ConfigFile>().unwrap();
        assert_eq!(file.path(), std::path::Path::new("member.yml"));
        assert_eq!(
            file.reason(),
            "cannot read configuration: permission denied"
        );
        assert_eq!(
            error.to_string(),
            "member.yml: cannot read configuration: permission denied"
        );
        assert_eq!(
            error.downcast_ref::<std::io::Error>().unwrap().kind(),
            std::io::ErrorKind::PermissionDenied
        );
    }
}
