//! Host-owned run directories. Only the payload directory is exposed to a guest.
use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
use cap_std::fs::{Dir, OpenOptions};
use std::{
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};

pub const RETENTION: Duration = Duration::from_secs(7 * 24 * 60 * 60);

fn valid(id: &str) -> std::io::Result<()> {
    if id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit()) {
        Ok(())
    } else {
        Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Invalid run ID",
        ))
    }
}
pub fn directory(data: &Path, id: &str) -> std::io::Result<PathBuf> {
    valid(id)?;
    Ok(data.join("run/files").join(id).join("work"))
}
pub struct Lease {
    pub path: PathBuf,
    _lock: gb_base::files::FileLock,
}
impl Drop for Lease {
    fn drop(&mut self) {
        // The timestamp is host-owned and updated before the OS unlocks.
        let _ = self._lock.file().set_modified(SystemTime::now());
    }
}
pub struct Completed {
    dir: Dir,
    _lock: gb_base::files::FileLock,
}
impl std::ops::Deref for Completed {
    type Target = Dir;
    fn deref(&self) -> &Dir {
        &self.dir
    }
}
fn lock(dir: &Dir, create: bool) -> std::io::Result<Option<gb_base::files::FileLock>> {
    let file = dir.open_with(
        "lease",
        OpenOptions::new()
            .read(true)
            .write(true)
            .create(create)
            .follow(FollowSymlinks::No),
    )?;
    gb_base::files::FileLock::try_file(file.into_std())
}
pub fn prepare(data: &Path, id: &str) -> std::io::Result<Lease> {
    prune(data)?;
    let path = directory(data, id)?;
    let parent = gb_base::host_path::open_directory(path.parent().unwrap(), true)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    let held = lock(&parent, true)?.ok_or_else(|| std::io::Error::other("Run is active"))?;
    parent.create_dir("work")?;
    Ok(Lease { path, _lock: held })
}
pub fn completed(data: &Path, id: &str) -> std::io::Result<Completed> {
    let path = directory(data, id)?;
    let parent = gb_base::host_path::open_directory(path.parent().unwrap(), false)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    let held = lock(&parent, false)?
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::WouldBlock, "Run active"))?;
    let meta = parent.symlink_metadata("lease")?;
    if SystemTime::now()
        .duration_since(meta.modified()?.into_std())
        .unwrap_or_default()
        >= RETENTION
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "Run expired",
        ));
    }
    Ok(Completed {
        dir: parent.open_dir_nofollow("work")?,
        _lock: held,
    })
}

pub fn prune(data: &Path) -> std::io::Result<()> {
    let path = data.join("run/files");
    if !path.exists() {
        return Ok(());
    }
    let root = gb_base::host_path::open_directory(&path, false)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    let cleanup = root.open_with(
        ".cleanup",
        OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .follow(FollowSymlinks::No),
    )?;
    let Some(_cleanup) = gb_base::files::FileLock::try_file(cleanup.into_std())? else {
        return Ok(());
    };
    for entry in root.entries()? {
        let entry = entry?;
        let Ok(dir) = root.open_dir_nofollow(entry.file_name()) else {
            continue;
        };
        let orphan_time = if dir.symlink_metadata("lease").is_err() {
            Some(dir.dir_metadata()?.modified()?.into_std())
        } else {
            None
        };
        if let Some(held) = lock(&dir, true)? {
            if let Some(time) = orphan_time {
                held.file().set_modified(time)?;
            }
            let meta = dir.symlink_metadata("lease")?;
            if SystemTime::now()
                .duration_since(meta.modified()?.into_std())
                .unwrap_or_default()
                >= RETENTION
            {
                dir.remove_open_dir_all()?;
            }
        }
    }
    Ok(())
}
/// Open every ancestor without following links; refuse devices, FIFOs and symbolic links.
pub fn regular(root: &Dir, name: &str) -> std::io::Result<cap_std::fs::File> {
    use std::path::Component;
    let parts: Vec<_> = Path::new(name).components().collect();
    if parts.is_empty()
        || name.contains(['\\', '\0'])
        || parts.iter().any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Invalid artifact path",
        ));
    }
    let mut dir = root.try_clone()?;
    for part in &parts[..parts.len() - 1] {
        dir = dir.open_dir_nofollow(part.as_os_str())?;
    }
    let leaf = parts.last().unwrap().as_os_str();
    let meta = dir.symlink_metadata(leaf)?;
    if !meta.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Not a regular file",
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true).follow(FollowSymlinks::No);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NONBLOCK);
    }
    let file = dir.open_with(leaf, &options)?;
    if !file.metadata()?.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Not a regular file",
        ));
    }
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn run_directories_are_private_until_finished_and_reject_links() {
        let root = tempfile::tempdir().unwrap();
        let a = "a".repeat(32);
        let b = "b".repeat(32);
        let first_lease = prepare(root.path(), &a).unwrap();
        let first = first_lease.path.clone();
        let second_lease = prepare(root.path(), &b).unwrap();
        let second = second_lease.path.clone();
        assert_ne!(first, second);
        std::fs::write(first.join("output.txt"), "result").unwrap();
        assert!(completed(root.path(), &a).is_err());
        assert!(directory(root.path(), "../outside").is_err());
        drop(first_lease);
        let dir = completed(root.path(), &a).unwrap();
        assert_eq!(
            regular(&dir, "output.txt")
                .unwrap()
                .metadata()
                .unwrap()
                .len(),
            6
        );
        assert!(regular(&dir, "../finished").is_err());
        assert!(regular(&dir, "/etc/passwd").is_err());
        assert!(completed(root.path(), &b).is_err());
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink("output.txt", first.join("link")).unwrap();
            std::os::unix::fs::symlink(&second, first.join("outside")).unwrap();
            assert!(regular(&dir, "link").is_err());
            assert!(regular(&dir, "outside/output.txt").is_err());
        }
        let finished = first.parent().unwrap().join("lease");
        std::fs::File::open(&finished)
            .unwrap()
            .set_modified(SystemTime::now() - RETENTION - Duration::from_secs(1))
            .unwrap();
        assert!(completed(root.path(), &a).is_err());
        drop(dir);
        prune(root.path()).unwrap();
        assert!(!first.exists());
        assert!(second.exists());
    }
}

#[cfg(test)]
mod failure_tests {
    use super::*;
    #[tokio::test]
    async fn cancellation_and_pre_run_failures_release_the_lease() {
        let root = tempfile::tempdir().unwrap();
        for n in 0..3 {
            let id = format!("{n:032x}");
            let lease = prepare(root.path(), &id).unwrap();
            std::fs::write(lease.path.join("partial.txt"), "partial").unwrap();
            lease
                ._lock
                .file()
                .set_modified(SystemTime::now() - RETENTION - Duration::from_secs(1))
                .unwrap();
            prune(root.path()).unwrap();
            assert!(lease.path.exists());
            if n == 0 {
                let task = tokio::spawn(async move {
                    let _held = lease;
                    std::future::pending::<()>().await;
                });
                task.abort();
                let _ = task.await;
            } else {
                drop(lease);
            }
            let complete = completed(root.path(), &id).unwrap();
            assert!(regular(&complete, "partial.txt").is_ok());
        }
    }
    #[test]
    fn a_crashed_process_releases_the_lease_and_orphans_expire() {
        const KEY: &str = "GUILDBOTICS_TEST_ARTIFACT_CRASH";
        let id = "c".repeat(32);
        if let Some(root) = std::env::var_os(KEY) {
            let lease = prepare(Path::new(&root), &id).unwrap();
            std::fs::write(lease.path.join("partial.txt"), "partial").unwrap();
            std::process::exit(0);
        }
        let root = tempfile::tempdir().unwrap();
        assert!(std::process::Command::new(std::env::current_exe().unwrap()).args(["--exact", "run_files::failure_tests::a_crashed_process_releases_the_lease_and_orphans_expire"]).env(KEY, root.path()).status().unwrap().success());
        let complete = completed(root.path(), &id).unwrap();
        assert!(regular(&complete, "partial.txt").is_ok());
        drop(complete);
        let path = directory(root.path(), &id).unwrap();
        std::fs::File::open(path.parent().unwrap().join("lease"))
            .unwrap()
            .set_modified(SystemTime::now() - RETENTION - Duration::from_secs(1))
            .unwrap();
        prune(root.path()).unwrap();
        assert!(!path.exists());
    }
}
