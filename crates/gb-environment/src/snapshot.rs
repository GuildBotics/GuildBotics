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

/// Whether automatic upkeep may build: never retry the current failed recipe.
pub fn needs_build(state: &State) -> bool {
    matches!(state, State::Missing | State::Stale)
}

/// Remove only environment snapshots and their failure markers under the build
/// lock. The caller removes each snapshot through its runtime's metadata API.
pub async fn remove<F, Fut>(workspace: &Path, mut perform: F) -> Result<(), String>
where
    F: FnMut(PathBuf) -> Fut,
    Fut: Future<Output = Result<(), String>>,
{
    let directory = snapshots_dir(workspace);
    let _held = FileLock::try_hold(&directory.join(LOCK_FILE))
        .map_err(|e| e.to_string())?
        .ok_or("An environment build is already running")?;
    for entry in std::fs::read_dir(&directory).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with(PREFIX) {
            continue;
        }
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        if kind.is_dir() {
            perform(entry.path()).await?;
        } else if kind.is_file() && name.ends_with(FAILED_SUFFIX) {
            std::fs::remove_file(entry.path()).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Serialize builds across processes, remember failure only for this recipe,
/// and prune old artifacts only after a successful replacement.
pub async fn build<F, Fut>(workspace: &Path, name: &str, perform: F) -> Result<Status, String>
where
    F: FnOnce(PathBuf) -> Fut,
    Fut: Future<Output = Result<PathBuf, String>>,
{
    status(workspace, name).map_err(|e| e.to_string())?;
    let directory = snapshots_dir(workspace);
    let _held = FileLock::try_hold(&directory.join(LOCK_FILE))
        .map_err(|e| e.to_string())?
        .ok_or("An environment build is already running")?;
    let marker = directory.join(format!("{name}{FAILED_SUFFIX}"));
    match std::fs::remove_file(&marker) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.to_string()),
    }
    let built = tokio::time::timeout(
        std::time::Duration::from_secs(30 * 60),
        perform(directory.clone()),
    )
    .await
    .unwrap_or_else(|_| Err("Environment build exceeded 30 minutes".into()));
    match built {
        Err(reason) => {
            gb_base::files::atomic_write(&marker, format!("{reason}\n").as_bytes())
                .map_err(|e| e.to_string())?;
            Err(reason)
        }
        Ok(path) => {
            if path.parent() != Some(directory.as_path())
                || !std::fs::symlink_metadata(&path)
                    .map_err(|e| e.to_string())?
                    .is_dir()
            {
                return Err("Build returned an invalid snapshot".into());
            }
            for entry in std::fs::read_dir(&directory).map_err(|e| e.to_string())? {
                let entry = entry.map_err(|e| e.to_string())?;
                let other = entry.path();
                let name = entry.file_name().to_string_lossy().into_owned();
                if other != path && name.starts_with(PREFIX) {
                    let kind = entry.file_type().map_err(|e| e.to_string())?;
                    if kind.is_dir() {
                        std::fs::remove_dir_all(other).map_err(|e| e.to_string())?;
                    } else if name.ends_with(FAILED_SUFFIX) {
                        std::fs::remove_file(other).map_err(|e| e.to_string())?;
                    }
                }
            }
            Ok(Status {
                path,
                state: State::Ready,
            })
        }
    }
}

/// One interval loop owned by the server; aborting the task stops upkeep.
pub async fn upkeep<F, Fut>(mut once: F)
where
    F: FnMut() -> Fut,
    Fut: Future<Output = ()>,
{
    let mut interval = tokio::time::interval(std::time::Duration::from_secs(30));
    loop {
        interval.tick().await;
        once().await;
    }
}
