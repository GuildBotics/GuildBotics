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
        Self::try_file(file)
    }

    /// Lock an already opened file without reopening any pathname.
    pub fn try_file(file: File) -> std::io::Result<Option<FileLock>> {
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            #[link(name = "kernel32")]
            unsafe extern "system" {
                fn LockFile(
                    handle: *mut std::ffi::c_void,
                    low: u32,
                    high: u32,
                    length_low: u32,
                    length_high: u32,
                ) -> i32;
            }
            // SAFETY: the handle belongs to this live File. Only the reserved
            // first byte is locked, allowing contenders to read the owner.
            if unsafe { LockFile(file.as_raw_handle(), 0, 0, 1, 0) } != 0 {
                return Ok(Some(FileLock(file)));
            }
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(33) {
                Ok(None)
            } else {
                Err(error)
            }
        }
        #[cfg(not(windows))]
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

/// Replaces `path` with `data` so that a reader never sees part of it.
pub fn atomic_write(path: &Path, data: &[u8]) -> std::io::Result<()> {
    atomic_write_mode(path, data, None)
}

/// [`atomic_write`], the file having the Unix `mode`, when one is given, from
/// the moment it replaces `path`.
pub fn atomic_write_mode(path: &Path, data: &[u8], mode: Option<u32>) -> std::io::Result<()> {
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
    #[cfg(unix)]
    if let Some(mode) = mode {
        use std::os::unix::fs::PermissionsExt;
        file.as_file()
            .set_permissions(std::fs::Permissions::from_mode(mode))?;
    }
    #[cfg(not(unix))]
    let _ = mode;
    file.as_file().sync_all()?;
    file.persist(path).map_err(|e| e.error)?;
    Ok(())
}

/// A shared record as every writer writes it: keys sorted, indented by two,
/// ending in a newline, so unchanged content stays byte for byte the same.
pub fn dump_shared_json(record: &impl serde::Serialize) -> String {
    let mut value = serde_json::to_value(record).expect("a record is JSON");
    value.sort_all_objects();
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
    fn a_contender_reads_owner_bytes_after_the_reserved_lock_byte() {
        use std::io::{Read, Seek, SeekFrom};
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("owner.lock");
        let held = FileLock::try_hold(&path).unwrap().unwrap();
        held.file().write_all(b" {\"pid\":42}\n").unwrap();
        assert!(FileLock::try_hold(&path).unwrap().is_none());
        let mut reader = File::open(&path).unwrap();
        reader.seek(SeekFrom::Start(1)).unwrap();
        let mut owner = String::new();
        reader.read_to_string(&mut owner).unwrap();
        assert_eq!(owner, "{\"pid\":42}\n");
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
