//! Where GuildBotics keeps what belongs to the machine rather than to a
//! workspace, as the Python version does: `~/.guildbotics`.

use std::ffi::OsString;
use std::path::{Path, PathBuf};

/// Names another machine root than `~/.guildbotics`, so a version under
/// development never touches the machine's own records.
pub const HOME_ENV: &str = "GUILDBOTICS_HOME";

/// The OS-owned home variable, independent of shell-specific HOME overrides.
#[cfg(windows)]
pub const USER_HOME_ENV: &str = "USERPROFILE";
#[cfg(not(windows))]
pub const USER_HOME_ENV: &str = "HOME";

pub fn user_home() -> Option<PathBuf> {
    std::env::var_os(USER_HOME_ENV)
        .filter(|home| !home.is_empty())
        .map(PathBuf::from)
}

/// `GUILDBOTICS_HOME`, else `~/.guildbotics`.
pub fn machine_root() -> anyhow::Result<PathBuf> {
    root_from(std::env::var_os(HOME_ENV), std::env::var_os(USER_HOME_ENV))
}

fn root_from(configured: Option<OsString>, home: Option<OsString>) -> anyhow::Result<PathBuf> {
    match configured {
        Some(root) if root.is_empty() => anyhow::bail!("{HOME_ENV} is empty"),
        Some(root) => Ok(root.into()),
        None => match home {
            Some(home) if !home.is_empty() => Ok(PathBuf::from(home).join(".guildbotics")),
            _ => anyhow::bail!("{USER_HOME_ENV} is not set; set {HOME_ENV}"),
        },
    }
}

/// The machine's own state: `<machine root>/data`.
pub fn data() -> anyhow::Result<PathBuf> {
    Ok(machine_root()?.join("data"))
}

/// Names the root of the members' identities, as the Python version reads it.
pub const IDENTITY_ROOT_ENV: &str = "GUILDBOTICS_IDENTITY_ROOT";

/// The root of the members' identities, which every workspace of the machine
/// shares: `GUILDBOTICS_IDENTITY_ROOT`, else `<machine root>/identity`.
pub fn identity_root() -> anyhow::Result<PathBuf> {
    match std::env::var(IDENTITY_ROOT_ENV) {
        Ok(root) if !root.trim().is_empty() => Ok(root.trim().into()),
        _ => Ok(machine_root()?.join("identity")),
    }
}

/// The Python package of the checkout this executable was built from, which
/// the server runs from: its bundled templates, and the code a command's
/// isolated environment runs.
pub fn python_package() -> PathBuf {
    let crates = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates/");
    crates.parent().expect("the checkout").join("guildbotics")
}

/// A file of every source tree, locked while a process reads the tree.
const SOURCE_MARK: &str = "Cargo.toml";

/// Marks this executable's source tree in use until the process exits: the
/// Hub update removes a build's source tree only when no process holds it.
/// Only advisory locks leave the tree's files writable, so Windows holds none.
#[cfg(unix)]
pub fn hold_source_tree() {
    static SOURCE: std::sync::OnceLock<Option<std::fs::File>> = std::sync::OnceLock::new();
    SOURCE.get_or_init(|| {
        let file = std::fs::File::open(python_package().parent()?.join(SOURCE_MARK)).ok()?;
        // Waits only while an update removes this very tree, which then fails anyway.
        file.lock_shared().ok()?;
        Some(file)
    });
}

/// Removes the source tree `tree` unless a process holds it, and says whether
/// it did. A tree without its mark is an interrupted build that never ran.
pub fn remove_unused_tree(tree: &Path) -> std::io::Result<bool> {
    let _lock = match std::fs::File::open(tree.join(SOURCE_MARK)) {
        Ok(file) => match file.try_lock() {
            Ok(()) => Some(file),
            Err(std::fs::TryLockError::WouldBlock) => return Ok(false),
            Err(std::fs::TryLockError::Error(error)) => return Err(error),
        },
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(error),
    };
    std::fs::remove_dir_all(tree)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_root_is_the_configured_one_or_under_home() {
        assert_eq!(
            root_from(Some("/x".into()), Some("/home/o".into())).unwrap(),
            PathBuf::from("/x")
        );
        assert_eq!(
            root_from(None, Some("/home/o".into())).unwrap(),
            PathBuf::from("/home/o/.guildbotics")
        );
        assert!(root_from(Some("".into()), Some("/home/o".into())).is_err());
        assert!(root_from(None, None).is_err());
        assert!(root_from(None, Some("".into())).is_err());
    }

    #[test]
    fn a_source_tree_is_removed_only_once_no_process_holds_it() {
        let temp = tempfile::tempdir().unwrap();
        let tree = temp.path().join("build");
        std::fs::create_dir_all(tree.join("guildbotics")).unwrap();
        std::fs::write(tree.join(SOURCE_MARK), "").unwrap();
        let holder = std::fs::File::open(tree.join(SOURCE_MARK)).unwrap();
        holder.lock_shared().unwrap();
        assert!(!remove_unused_tree(&tree).unwrap());
        assert!(tree.join("guildbotics").exists());
        drop(holder);
        assert!(remove_unused_tree(&tree).unwrap());
        assert!(!tree.exists());

        let interrupted = temp.path().join("interrupted");
        std::fs::create_dir_all(interrupted.join("node_modules")).unwrap();
        assert!(remove_unused_tree(&interrupted).unwrap());
        assert!(!interrupted.exists());
    }
}
