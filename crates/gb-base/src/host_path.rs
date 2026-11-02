//! Names of the host's own files, checked as the Python version checks them
//! (`guildbotics/utils/safe_paths.py`): spelled without `..`, and reached
//! without following a link at any component.

use std::fmt;
use std::path::{Component, Path, PathBuf};

/// Why a host path is not used.
#[derive(Debug)]
pub enum PathProblem {
    /// Not a plain absolute name: it climbs with `..` or holds a NUL.
    Invalid(PathBuf),
    /// A component is a link.
    Link(PathBuf),
    /// It does not exist, where it has to.
    Missing(PathBuf),
    /// A component cannot be inspected, or is not a directory.
    Unavailable(PathBuf, std::io::Error),
}

impl fmt::Display for PathProblem {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PathProblem::Invalid(path) => write!(f, "{} is not a valid path", path.display()),
            PathProblem::Link(path) => write!(f, "{} passes through a link", path.display()),
            PathProblem::Missing(path) => write!(f, "{} does not exist", path.display()),
            PathProblem::Unavailable(path, reason) => {
                write!(f, "{} cannot be inspected: {reason}", path.display())
            }
        }
    }
}

impl std::error::Error for PathProblem {}

/// Expands `~` and makes the name absolute, without touching the file
/// system. The links the OS itself puts at the top (`/tmp`, `/var`, `/etc` on
/// macOS) are spelled as what they name.
pub fn normalize(path: &Path) -> Result<PathBuf, PathProblem> {
    let expanded = match path.strip_prefix("~") {
        Ok(rest) => match std::env::var_os("HOME") {
            Some(home) if !home.is_empty() => PathBuf::from(home).join(rest),
            _ => path.to_path_buf(),
        },
        Err(_) => path.to_path_buf(),
    };
    // Before it is made absolute: on Windows that resolves `..` by spelling.
    if expanded.as_os_str().as_encoded_bytes().contains(&0)
        || expanded.components().any(|c| c == Component::ParentDir)
    {
        return Err(PathProblem::Invalid(expanded));
    }
    let absolute =
        std::path::absolute(&expanded).map_err(|_| PathProblem::Invalid(expanded.clone()))?;
    let plain: PathBuf = absolute.components().collect();
    Ok(os_alias(plain))
}

#[cfg(target_os = "macos")]
fn os_alias(path: PathBuf) -> PathBuf {
    for alias in ["/tmp", "/var", "/etc"] {
        if path.starts_with(alias) {
            return Path::new("/private").join(path.strip_prefix("/").expect("absolute"));
        }
    }
    path
}

#[cfg(not(target_os = "macos"))]
fn os_alias(path: PathBuf) -> PathBuf {
    path
}

/// The normalized name of a directory whose every component opens as a
/// directory without following a link. With `missing`, the name may end in
/// components that do not exist yet.
pub fn directory(path: &Path, missing: bool) -> Result<PathBuf, PathProblem> {
    let path = normalize(path)?;
    let mut checked = PathBuf::new();
    for component in path.components() {
        checked.push(component);
        match open_directory(&checked) {
            Opened::Directory => {}
            Opened::Missing if missing => return Ok(path),
            Opened::Missing => return Err(PathProblem::Missing(path)),
            Opened::Link => return Err(PathProblem::Link(path)),
            Opened::Unavailable(e) => return Err(PathProblem::Unavailable(path, e)),
        }
    }
    Ok(path)
}

/// What one component turned out to be.
enum Opened {
    Directory,
    Missing,
    Link,
    Unavailable(std::io::Error),
}

/// Looks at the component without following it, then opens it as a
/// directory, so one its owner cannot read is refused as the Python version
/// refuses it.
fn open_directory(path: &Path) -> Opened {
    match std::fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_symlink() => return Opened::Link,
        Ok(meta) if !meta.is_dir() => {
            return Opened::Unavailable(std::io::ErrorKind::NotADirectory.into());
        }
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Opened::Missing,
        Err(e) => return Opened::Unavailable(e),
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        let opened = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_DIRECTORY | libc::O_NONBLOCK)
            .open(path);
        if let Err(e) = opened {
            return Opened::Unavailable(e);
        }
    }
    Opened::Directory
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_name_is_absolute_plain_and_climbs_nowhere() {
        let cwd = std::env::current_dir().unwrap();
        assert_eq!(
            normalize(Path::new("a/./b//c")).unwrap(),
            os_alias(cwd.join("a/b/c"))
        );
        assert!(matches!(
            normalize(Path::new("/x/../y")),
            Err(PathProblem::Invalid(_))
        ));
        let home = PathBuf::from(std::env::var_os("HOME").unwrap());
        assert_eq!(
            normalize(Path::new("~/w")).unwrap(),
            os_alias(home.join("w"))
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn the_os_links_at_the_top_are_spelled_as_what_they_name() {
        assert_eq!(
            normalize(Path::new("/var/a")).unwrap(),
            Path::new("/private/var/a")
        );
        assert_eq!(normalize(Path::new("/tmpx")).unwrap(), Path::new("/tmpx"));
    }

    #[cfg(unix)]
    #[test]
    fn a_directory_is_reached_through_directories_only() {
        let root = tempfile::tempdir().unwrap();
        let root = normalize(root.path()).unwrap();
        std::fs::create_dir(root.join("real")).unwrap();
        std::fs::write(root.join("file"), "").unwrap();
        std::os::unix::fs::symlink(root.join("real"), root.join("link")).unwrap();

        assert_eq!(
            directory(&root.join("real"), false).unwrap(),
            root.join("real")
        );
        assert_eq!(
            directory(&root.join("real/new/deeper"), true).unwrap(),
            root.join("real/new/deeper")
        );
        assert!(matches!(
            directory(&root.join("real/new"), false),
            Err(PathProblem::Missing(_))
        ));
        assert!(matches!(
            directory(&root.join("link/new"), true),
            Err(PathProblem::Link(_))
        ));
        assert!(matches!(
            directory(&root.join("file"), true),
            Err(PathProblem::Unavailable(..))
        ));
        assert!(matches!(
            directory(&root.join("link"), true),
            Err(PathProblem::Link(_))
        ));
        // A directory its owner cannot read is not opened, as in the Python
        // version; root reads it regardless.
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(root.join("real"), std::fs::Permissions::from_mode(0o300))
            .unwrap();
        let unreadable = directory(&root.join("real"), false);
        std::fs::set_permissions(root.join("real"), std::fs::Permissions::from_mode(0o700))
            .unwrap();
        if unsafe { libc::geteuid() } != 0 {
            assert!(matches!(unreadable, Err(PathProblem::Unavailable(..))));
        }
    }
}
