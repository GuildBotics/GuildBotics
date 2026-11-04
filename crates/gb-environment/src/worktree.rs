//! Apply a guest's regular-file changes under a held directory descriptor.
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use unicode_normalization::UnicodeNormalization;

pub const SOURCE: &str = "/run/guildbotics/worktree";
#[derive(Debug, PartialEq)]
pub struct Copy {
    pub host: PathBuf,
    pub guest: String,
    pub excluded: Vec<String>,
    pub lock: PathBuf,
    identity: same_file::Handle,
}
impl Copy {
    pub fn validate(&self) -> Result<(), String> {
        gb_base::host_path::directory(&self.host, false).map_err(|e| e.to_string())?;
        if identity(&open_root(&self.host)?)? != self.identity {
            return Err("Working directory was replaced".into());
        }
        Ok(())
    }
    pub fn new(
        host: PathBuf,
        guest: String,
        excluded: Vec<String>,
        lock: PathBuf,
    ) -> Result<Self, String> {
        gb_base::host_path::directory(&host, false).map_err(|e| e.to_string())?;
        let file = open_root(&host)?;
        Ok(Self {
            host,
            guest,
            excluded,
            lock,
            identity: identity(&file)?,
        })
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Copied {
    path: String,
    sha256: String,
    executable: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Change {
    path: String,
    content: Option<String>,
    deleted: Option<bool>,
    executable: Option<bool>,
}
fn folded(value: &str) -> String {
    unicase::UniCase::unicode(value.nfd().collect::<String>()).to_folded_case()
}
fn name(value: &str, excluded: &[String], sensitive: bool) -> Result<PathBuf, String> {
    let p = Path::new(value);
    if value.is_empty()
        || value.contains(['\\', '\0', ':'])
        || p.is_absolute()
        || p.components().any(|c| !matches!(c, Component::Normal(_)))
        || p.to_string_lossy() != value
        || value
            .split('/')
            .any(|s| s.eq_ignore_ascii_case(".git") || s.is_empty() || s == "." || s == "..")
        || excluded.iter().any(|e| {
            let target = if sensitive {
                value.to_owned()
            } else {
                folded(value)
            };
            let excluded = if sensitive { e.clone() } else { folded(e) };
            target == excluded
                || target
                    .strip_prefix(&excluded)
                    .is_some_and(|tail| tail.starts_with('/'))
        })
    {
        return Err(format!("Unsafe worktree path '{value}'"));
    }
    Ok(p.to_owned())
}
fn open_root(path: &Path) -> Result<File, String> {
    portable::open_root(path).map(cap_std::fs::Dir::into_std_file)
}
fn identity(file: &File) -> Result<same_file::Handle, String> {
    same_file::Handle::from_file(file.try_clone().map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}
fn case_sensitive(root: &File) -> Result<bool, String> {
    let directory = cap_std::fs::Dir::from_std_file(root.try_clone().map_err(|e| e.to_string())?);
    let name = format!(".guildbotics-case-{}", uuid::Uuid::new_v4().simple());
    let file = directory
        .open_with(
            &name,
            cap_std::fs::OpenOptions::new().write(true).create_new(true),
        )
        .map_err(|e| e.to_string())?;
    let original = identity(&file.into_std())?;
    let result = match directory.open(name.to_uppercase()) {
        Ok(file) => identity(&file.into_std()).map(|other| other != original),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(true),
        Err(e) => Err(e.to_string()),
    };
    directory.remove_file(&name).map_err(|e| e.to_string())?;
    result
}
type DecodedChange = (PathBuf, Change, Option<Vec<u8>>);
fn records(
    copy: &Copy,
    copied: &[u8],
    changes: &[u8],
    sensitive: bool,
) -> Result<(BTreeMap<String, Copied>, Vec<DecodedChange>), String> {
    if copied.len() > 64 * 1024 * 1024 || changes.len() > 1024 * 1024 * 1024 {
        return Err("Worktree response exceeds its limit".into());
    }
    let mut originals = BTreeMap::new();
    for line in copied.split(|b| *b == b'\n').filter(|s| !s.is_empty()) {
        let record: Copied = serde_json::from_slice(line).map_err(|e| e.to_string())?;
        name(&record.path, &copy.excluded, sensitive)?;
        if originals.insert(record.path.clone(), record).is_some() {
            return Err("Copied file repeated".into());
        }
    }
    let mut seen = HashSet::new();
    let mut changes_decoded = Vec::new();
    for line in changes.split(|b| *b == b'\n').filter(|s| !s.is_empty()) {
        let change: Change = serde_json::from_slice(line).map_err(|e| e.to_string())?;
        let path = name(&change.path, &copy.excluded, sensitive)?;
        if !seen.insert(if sensitive {
            change.path.clone()
        } else {
            folded(&change.path)
        }) || change.deleted.unwrap_or(false) == change.content.is_some()
        {
            return Err("Ambiguous or repeated worktree change".into());
        }
        let bytes = change
            .content
            .as_ref()
            .map(|s| STANDARD.decode(s).map_err(|e| e.to_string()))
            .transpose()?;
        changes_decoded.push((path, change, bytes));
    }
    Ok((originals, changes_decoded))
}
/// Apply only regular-file data through held directory capabilities.
pub use portable::write_back;

mod portable {
    use super::*;
    use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
    use cap_std::fs::{Dir, OpenOptions as CapOpen};
    pub(super) fn open_root(path: &Path) -> Result<Dir, String> {
        let mut anchor = PathBuf::new();
        let mut parts = Vec::new();
        for part in path.components() {
            match part {
                Component::Prefix(_) | Component::RootDir => anchor.push(part),
                Component::Normal(name) => parts.push(name),
                _ => return Err("Invalid working directory".into()),
            }
        }
        if anchor.as_os_str().is_empty() {
            return Err("Working directory must be absolute".into());
        }
        let mut dir = Dir::open_ambient_dir(anchor, cap_std::ambient_authority())
            .map_err(|e| e.to_string())?;
        for part in parts {
            dir = dir.open_dir_nofollow(part).map_err(|e| e.to_string())?;
        }
        Ok(dir)
    }
    fn parent(root: &Dir, path: &Path, create: bool) -> Result<Option<Dir>, String> {
        let mut dir = root.try_clone().map_err(|e| e.to_string())?;
        for part in path.parent().unwrap().components() {
            if create {
                match dir.create_dir(part.as_os_str()) {
                    Ok(()) => {}
                    Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
                    Err(e) => return Err(e.to_string()),
                }
            }
            match dir.open_dir_nofollow(part.as_os_str()) {
                Ok(next) => dir = next,
                Err(e) if !create && e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
                Err(e) => return Err(e.to_string()),
            }
        }
        Ok(Some(dir))
    }
    fn state(
        dir: Option<&Dir>,
        path: &Path,
    ) -> Result<Option<(String, bool, std::fs::Permissions)>, String> {
        let Some(dir) = dir else { return Ok(None) };
        let mut options = CapOpen::new();
        options.read(true).follow(FollowSymlinks::No);
        #[cfg(unix)]
        {
            use cap_std::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NONBLOCK);
        }
        let file = match dir.open_with(path.file_name().unwrap(), &options) {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e.to_string()),
        };
        let metadata = file.metadata().map_err(|e| e.to_string())?;
        if !metadata.is_file() {
            return Err("Not a regular file".into());
        }
        let mut file = file.into_std();
        let permissions = file.metadata().map_err(|e| e.to_string())?.permissions();
        let mut hash = Sha256::new();
        let mut buffer = [0; 65536];
        loop {
            let n = file.read(&mut buffer).map_err(|e| e.to_string())?;
            if n == 0 {
                break;
            }
            hash.update(&buffer[..n]);
        }
        #[cfg(unix)]
        let executable = {
            use std::os::unix::fs::PermissionsExt;
            permissions.mode() & 0o111 != 0
        };
        #[cfg(not(unix))]
        let executable = false;
        Ok(Some((
            hash.finalize().iter().map(|b| format!("{b:02x}")).collect(),
            executable,
            permissions,
        )))
    }
    pub fn write_back(copy: &Copy, copied: &[u8], changes: &[u8]) -> Result<(), String> {
        let _held = gb_base::files::FileLock::hold(&copy.lock, std::time::Duration::from_secs(600))
            .map_err(|e| e.to_string())?;
        let root = open_root(&copy.host)?;
        if identity(&root.try_clone().map_err(|e| e.to_string())?.into_std_file())? != copy.identity
        {
            return Err("Working directory was replaced".into());
        }
        let (originals, changes) = records(
            copy,
            copied,
            changes,
            case_sensitive(&root.try_clone().map_err(|e| e.to_string())?.into_std_file())?,
        )?;
        let mut planned = Vec::new();
        for (path, change, bytes) in changes {
            if cfg!(windows) {
                for part in path.components() {
                    let value = part.as_os_str().to_str().ok_or("Invalid worktree name")?;
                    let stem = value
                        .split('.')
                        .next()
                        .unwrap_or_default()
                        .trim_end_matches(' ')
                        .to_uppercase();
                    if value.ends_with(['.', ' '])
                        || ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
                            .contains(&stem.as_str())
                        || ["COM", "LPT"].iter().any(|prefix| {
                            stem.strip_prefix(prefix).is_some_and(|n| {
                                ["1", "2", "3", "4", "5", "6", "7", "8", "9", "¹", "²", "³"]
                                    .contains(&n)
                            })
                        })
                    {
                        return Err("Reserved Windows worktree name".into());
                    }
                }
            }
            let directory = parent(&root, &path, false)?;
            let current = state(directory.as_ref(), &path)?;
            match (originals.get(&change.path), &current) {
                (None, None) => {}
                (Some(before), Some((hash, executable, _)))
                    if before.sha256 == *hash
                        && (cfg!(windows)
                            || change.executable.is_none()
                            || before.executable == *executable) => {}
                _ => return Err(format!("Worktree conflict: {}", change.path)),
            }
            if change.deleted.unwrap_or(false) && current.is_none() {
                continue;
            }
            planned.push((path, bytes, change.executable, current.map(|s| s.2)));
        }
        let mut staged: Vec<(Dir, PathBuf, Option<PathBuf>)> = Vec::new();
        let stage = (|| -> Result<(), String> {
            for (path, bytes, executable, permissions) in planned {
                let dir =
                    parent(&root, &path, bytes.is_some())?.ok_or("Deleted parent disappeared")?;
                let destination = PathBuf::from(path.file_name().unwrap());
                if let Some(bytes) = bytes {
                    let temporary =
                        PathBuf::from(format!(".guildbotics-{}", uuid::Uuid::new_v4().simple()));
                    let mut options = CapOpen::new();
                    options
                        .write(true)
                        .create_new(true)
                        .follow(FollowSymlinks::No);
                    let mut file = dir
                        .open_with(&temporary, &options)
                        .map_err(|e| e.to_string())?
                        .into_std();
                    staged.push((
                        dir.try_clone().map_err(|e| e.to_string())?,
                        destination,
                        Some(temporary),
                    ));
                    file.write_all(&bytes).map_err(|e| e.to_string())?;
                    let permissions = permissions
                        .unwrap_or(file.metadata().map_err(|e| e.to_string())?.permissions());
                    #[cfg(unix)]
                    let permissions = {
                        use std::os::unix::fs::PermissionsExt;
                        let mode = permissions.mode();
                        std::fs::Permissions::from_mode(match executable {
                            Some(true) => mode | ((mode & 0o444) >> 2),
                            Some(false) => mode & !0o111,
                            None => mode,
                        })
                    };
                    #[cfg(not(unix))]
                    let _ = executable;
                    file.set_permissions(permissions)
                        .map_err(|e| e.to_string())?;
                    file.sync_all().map_err(|e| e.to_string())?;
                } else {
                    staged.push((dir, destination, None));
                }
            }
            Ok(())
        })();
        let commit = if stage.is_ok() {
            staged.iter().try_for_each(|(dir, destination, temporary)| {
                match temporary {
                    Some(temporary) => dir.rename(temporary, dir, destination),
                    None => dir.remove_file(destination),
                }
                .map_err(|e| format!("Partial worktree write: {e}"))
            })
        } else {
            Ok(())
        };
        for (dir, _, temporary) in &staged {
            if let Some(temporary) = temporary {
                let _ = dir.remove_file(temporary);
            }
        }
        stage.and(commit)
    }
    #[test]
    fn case_folding_matches_host_aliases_without_collapsing_sensitive_names() {
        let temp = tempfile::tempdir().unwrap();
        let copy = Copy::new(
            temp.path().to_owned(),
            "/work".into(),
            vec!["Straße".into()],
            temp.path().join("lock"),
        )
        .unwrap();
        let aliases =
            b"{\"path\":\"a\",\"content\":\"eA==\"}\n{\"path\":\"A\",\"content\":\"eQ==\"}";
        assert!(records(&copy, b"", aliases, true).is_ok());
        assert!(records(&copy, b"", aliases, false).is_err());
        assert!(name("STRASSE/file", &copy.excluded, false).is_err());
        assert!(name("STRASSE/file", &copy.excluded, true).is_ok());
        let unicode_aliases =
            "{\"path\":\"é\",\"content\":\"eA==\"}\n{\"path\":\"é\",\"content\":\"eQ==\"}";
        assert!(records(&copy, b"", unicode_aliases.as_bytes(), false).is_err());
    }
    #[test]
    fn portable_writeback_is_confined_and_validates_population() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("root");
        std::fs::create_dir(&root).unwrap();
        let copy = Copy::new(
            root.clone(),
            "/work".into(),
            vec!["excluded".into()],
            temp.path().join("lock"),
        )
        .unwrap();
        write_back(&copy, b"", br#"{"path":"new/nested","content":"b2s="}"#).unwrap();
        assert_eq!(std::fs::read(root.join("new/nested")).unwrap(), b"ok");
        for bad in ["../outside", "excluded/a", "C:/outside", ".git/config"] {
            let changes = format!(
                "{{\"path\":\"good\",\"content\":\"eA==\"}}\n{}",
                serde_json::json!({"path":bad,"content":"eA=="})
            );
            assert!(write_back(&copy, b"", changes.as_bytes()).is_err());
            assert!(!root.join("good").exists());
        }
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(temp.path(), root.join("link")).unwrap();
            assert!(write_back(&copy, b"", br#"{"path":"link/out","content":"eA=="}"#).is_err());
            assert!(!temp.path().join("out").exists());
        }
    }
}
