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
pub fn prepare(data: &Path, id: &str) -> std::io::Result<PathBuf> {
    prune(data)?;
    let path = directory(data, id)?;
    gb_base::host_path::open_directory(&path, true)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    Ok(path)
}
pub fn finish(data: &Path, id: &str) -> std::io::Result<()> {
    let path = directory(data, id)?;
    let parent = gb_base::host_path::open_directory(path.parent().unwrap(), false)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    parent.write("finished", [])
}
pub fn completed(data: &Path, id: &str) -> std::io::Result<Dir> {
    let path = directory(data, id)?;
    let parent = gb_base::host_path::open_directory(path.parent().unwrap(), false)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    let meta = parent.symlink_metadata("finished")?;
    if !meta.is_file()
        || SystemTime::now()
            .duration_since(meta.modified()?.into_std())
            .unwrap_or_default()
            >= RETENTION
    {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            "Run files expired or unavailable",
        ));
    }
    parent.open_dir_nofollow("work")
}
pub fn prune(data: &Path) -> std::io::Result<()> {
    let path = data.join("run/files");
    if !path.exists() {
        return Ok(());
    }
    let root = gb_base::host_path::open_directory(&path, false)
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    for entry in root.entries()? {
        let entry = entry?;
        let Ok(dir) = root.open_dir_nofollow(entry.file_name()) else {
            continue;
        };
        let Ok(meta) = dir.symlink_metadata("finished") else {
            continue;
        };
        if meta.is_file()
            && SystemTime::now()
                .duration_since(meta.modified()?.into_std())
                .unwrap_or_default()
                >= RETENTION
        {
            dir.remove_open_dir_all()?;
        }
    }
    Ok(())
}
/// Open every ancestor without following links; refuse devices, FIFOs and hard links.
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
        let first = prepare(root.path(), &a).unwrap();
        let second = prepare(root.path(), &b).unwrap();
        assert_ne!(first, second);
        std::fs::write(first.join("output.txt"), "result").unwrap();
        assert!(completed(root.path(), &a).is_err());
        assert!(directory(root.path(), "../outside").is_err());
        finish(root.path(), &a).unwrap();
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
        let finished = first.parent().unwrap().join("finished");
        std::fs::File::open(&finished)
            .unwrap()
            .set_modified(SystemTime::now() - RETENTION - Duration::from_secs(1))
            .unwrap();
        assert!(completed(root.path(), &a).is_err());
        prune(root.path()).unwrap();
        assert!(!first.exists());
        assert!(second.exists());
    }
}
