//! Writing the host's files as the Python version does
//! (`guildbotics/utils/fileio.py`, `advisory_lock.py`, `shared_writes.py`), so
//! the two take the same locks and leave the same bytes.

use std::fs::File;
use std::io::Write;
use std::path::Path;
use std::time::{Duration, Instant};

/// The generation of every shared record this version writes.
pub const SHARED_RECORD_SCHEMA_VERSION: u64 = 4;

/// How long a writer of a workspace's shared files waits for another one.
pub const SHARED_WRITE_TIMEOUT: Duration = Duration::from_secs(30);

/// An advisory lock on a file, held until dropped: another process, or
/// another holder in this one, waits for it.
pub struct FileLock(File);

impl FileLock {
    /// The locked file, for a holder that records itself in it.
    pub fn file(&self) -> &File {
        &self.0
    }

    /// The lock at `path` if no one holds it now, creating the file.
    pub fn try_hold(path: &Path) -> std::io::Result<Option<FileLock>> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut options = std::fs::OpenOptions::new();
        options.read(true).write(true).create(true).truncate(false);
        #[cfg(unix)]
        std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
        let file = options.open(path)?;
        match file.try_lock() {
            Ok(()) => Ok(Some(FileLock(file))),
            Err(std::fs::TryLockError::WouldBlock) => Ok(None),
            Err(std::fs::TryLockError::Error(e)) => Err(e),
        }
    }

    /// Waits up to `timeout` for the lock at `path`.
    pub fn hold(path: &Path, timeout: Duration) -> std::io::Result<FileLock> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(lock) = FileLock::try_hold(path)? {
                return Ok(lock);
            }
            if Instant::now() >= deadline {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    format!(
                        "Timed out waiting for the advisory lock at {}.",
                        path.display()
                    ),
                ));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

/// The lock every writer of a workspace's shared files (`config`, `state`)
/// takes for the span in which it decides and writes.
pub fn shared_write_lock(workspace: &Path) -> std::io::Result<FileLock> {
    let path = workspace.join(".guildbotics/local/run/shared-write.lock");
    FileLock::hold(&path, SHARED_WRITE_TIMEOUT)
}

/// Replaces `path` with `data` so that a reader never sees part of it.
pub fn atomic_write(path: &Path, data: &[u8]) -> std::io::Result<()> {
    let parent = path.parent().expect("a file has a parent");
    std::fs::create_dir_all(parent)?;
    let name = path
        .file_name()
        .expect("a file has a name")
        .to_string_lossy();
    let mut file = tempfile::Builder::new()
        .prefix(&format!("{name}."))
        .suffix(".tmp")
        .tempfile_in(parent)?;
    file.write_all(data)?;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|e| e.error)?;
    Ok(())
}

/// A shared record as every writer writes it: keys sorted, indented by two,
/// ending in a newline, so unchanged content stays byte for byte the same.
pub fn dump_shared_json(record: &impl serde::Serialize) -> String {
    let value = serde_json::to_value(record).expect("a record is JSON");
    let mut text = serde_json::to_string_pretty(&value).expect("JSON serializes");
    text.push('\n');
    text
}

/// Reads `path`, or `None` when there is no such file.
pub fn read_if_present(path: &Path) -> std::io::Result<Option<Vec<u8>>> {
    match std::fs::read(path) {
        Ok(bytes) => Ok(Some(bytes)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_held_lock_makes_the_next_holder_wait_until_it_is_dropped() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("run/x.lock");
        let held = FileLock::hold(&path, Duration::ZERO).unwrap();
        let refused = FileLock::hold(&path, Duration::from_millis(30))
            .err()
            .unwrap();
        assert_eq!(refused.kind(), std::io::ErrorKind::TimedOut);
        drop(held);
        FileLock::hold(&path, Duration::ZERO).unwrap();
    }

    #[test]
    fn a_write_replaces_the_file_whole_and_leaves_nothing_beside_it() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state/x.json");
        atomic_write(&path, b"one").unwrap();
        atomic_write(&path, b"two").unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"two");
        let names: Vec<_> = std::fs::read_dir(path.parent().unwrap())
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(names, ["x.json"]);
    }

    #[test]
    fn shared_json_is_sorted_indented_and_ends_in_a_newline() {
        #[derive(serde::Serialize)]
        struct Record {
            b: u8,
            a: &'static str,
        }
        assert_eq!(
            dump_shared_json(&Record { b: 1, a: "名" }),
            "{\n  \"a\": \"名\",\n  \"b\": 1\n}\n"
        );
    }
}
