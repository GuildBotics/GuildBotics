//! The snapshot a command's microVM boots from: the one the Python version
//! built for the workspace (`guildbotics/environment/snapshot.py`), under
//! `<workspace>/.guildbotics/local/agent_environment/snapshots`.
//!
//! Which snapshot the workspace's declaration asks for is the Python
//! version's recipe, not carried over yet. Until it is, the workspace's only
//! snapshot is the one, and only while no build runs and none failed: any
//! other state is refused, in the words the Python version refuses it with.

use std::path::{Path, PathBuf};

use gb_base::files::FileLock;

use crate::Sentence;

/// What a snapshot's name starts with.
const PREFIX: &str = "guildbotics-";
/// What a build holds while it runs.
const LOCK_FILE: &str = "build.lock";
/// What a failed build leaves beside its snapshot's name, saying why.
const FAILED_SUFFIX: &str = ".failed";

/// Where the workspace's snapshots are on this device.
pub fn snapshots_dir(workspace: &Path) -> PathBuf {
    workspace.join(".guildbotics/local/agent_environment/snapshots")
}

/// The snapshot a command boots from, or why there is none to boot from.
pub fn ready(workspace: &Path) -> Result<PathBuf, Sentence> {
    let directory = snapshots_dir(workspace);
    let refused = |state: &str, params| {
        Sentence::Key(
            match state {
                "building" => "intelligences.agent_environment.snapshot.building",
                "failed" => "intelligences.agent_environment.snapshot.failed",
                "stale" => "intelligences.agent_environment.snapshot.stale",
                _ => "intelligences.agent_environment.snapshot.missing",
            },
            params,
        )
    };
    if building(&directory) {
        return Err(refused("building", vec![]));
    }
    let mut snapshots = Vec::new();
    for entry in std::fs::read_dir(&directory)
        .into_iter()
        .flatten()
        .flatten()
    {
        let name = entry.file_name().to_string_lossy().into_owned();
        let path = entry.path();
        if !name.starts_with(PREFIX) {
            continue;
        }
        if name.ends_with(FAILED_SUFFIX) && path.is_file() {
            let detail = std::fs::read_to_string(&path).unwrap_or_default();
            return Err(refused(
                "failed",
                vec![("detail", detail.trim().to_owned())],
            ));
        }
        if path.is_dir() {
            snapshots.push(path);
        }
    }
    match <[PathBuf; 1]>::try_from(snapshots) {
        Ok([snapshot]) => Ok(snapshot),
        Err(snapshots) if snapshots.is_empty() => Err(refused("missing", vec![])),
        // Which one the declaration asks for is the recipe's to say; a build
        // of a newer declaration leaves the older one behind.
        Err(_) => Err(refused("stale", vec![])),
    }
}

/// Whether a build holds the directory's lock, in this process or another.
fn building(directory: &Path) -> bool {
    let lock = directory.join(LOCK_FILE);
    lock.exists() && matches!(FileLock::try_hold(&lock), Ok(None))
}
