//! Compare the current recipe with the device's snapshots, without booting.

use crate::Sentence;
use gb_base::files::FileLock;
use std::path::{Path, PathBuf};

const PREFIX: &str = "guildbotics-";
const LOCK_FILE: &str = "build.lock";
const FAILED_SUFFIX: &str = ".failed";

#[derive(Debug, Clone, PartialEq)]
pub enum State {
    Missing,
    Stale,
    Building,
    Failed(String),
    Ready,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Status {
    pub path: PathBuf,
    pub state: State,
}

pub fn snapshots_dir(workspace: &Path) -> PathBuf {
    workspace.join(".guildbotics/local/agent_environment/snapshots")
}

/// Only the current recipe's failure and directory affect readiness.
/// A running build takes precedence; filesystem errors are never "missing".
pub fn status(workspace: &Path, name: &str) -> std::io::Result<Status> {
    if !name.strip_prefix(PREFIX).is_some_and(|digest| {
        digest.len() == 16
            && digest
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    }) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Invalid snapshot recipe name",
        ));
    }
    let directory = snapshots_dir(workspace);
    let path = directory.join(name);
    let lock = directory.join(LOCK_FILE);
    if lock.try_exists()? && FileLock::try_hold(&lock)?.is_none() {
        return Ok(Status {
            path,
            state: State::Building,
        });
    }
    let failed = directory.join(format!("{name}{FAILED_SUFFIX}"));
    let state = match std::fs::read_to_string(&failed) {
        Ok(detail) => State::Failed(detail.trim().to_owned()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            if path.try_exists()? && std::fs::symlink_metadata(&path)?.file_type().is_dir() {
                State::Ready
            } else {
                let mut stale = false;
                match std::fs::read_dir(&directory) {
                    Ok(entries) => {
                        for entry in entries {
                            let entry = entry?;
                            if entry.file_name().to_string_lossy().starts_with(PREFIX)
                                && entry.file_type()?.is_dir()
                            {
                                stale = true;
                            }
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    Err(e) => return Err(e),
                }
                if stale { State::Stale } else { State::Missing }
            }
        }
        Err(e) => return Err(e),
    };
    Ok(Status { path, state })
}

pub fn ready(workspace: &Path, name: &str) -> Result<PathBuf, Sentence> {
    let status = status(workspace, name).map_err(|e| Sentence::Text(e.to_string()))?;
    let key = match status.state {
        State::Ready => return Ok(status.path),
        State::Missing => "intelligences.agent_environment.snapshot.missing",
        State::Stale => "intelligences.agent_environment.snapshot.stale",
        State::Building => "intelligences.agent_environment.snapshot.building",
        State::Failed(detail) => {
            return Err(Sentence::Key(
                "intelligences.agent_environment.snapshot.failed",
                vec![("detail", detail)],
            ));
        }
    };
    Err(Sentence::key(key))
}
