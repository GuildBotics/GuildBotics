//! Shared writes use explicit capabilities, never thread-local reentrancy.
//! Blocking operations belong on a blocking worker, not a Tokio executor.

use crate::files::{self, FileLock, SHARED_WRITE_TIMEOUT};
use std::{
    fmt, fs,
    io::{self, Write},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

/// Lock contention is deliberately not an I/O error (HTTP callers use 503).
#[derive(Debug)]
pub struct SharedWriteBusyError(pub PathBuf);
impl fmt::Display for SharedWriteBusyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Another writer holds {}", self.0.display())
    }
}
impl std::error::Error for SharedWriteBusyError {}

fn hold(root: &Path, timeout: Duration) -> anyhow::Result<FileLock> {
    let path = root.join(".guildbotics/local/run/shared-write.lock");
    let started = Instant::now();
    loop {
        if let Some(lock) = FileLock::try_hold(&path)? {
            return Ok(lock);
        }
        if started.elapsed() >= timeout {
            return Err(SharedWriteBusyError(path).into());
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// Resolve links in existing ancestors, even when the leaf is not created yet.
pub fn resolved(path: &Path) -> io::Result<PathBuf> {
    resolve_links(path, 0)
}

fn resolve_links(path: &Path, links: usize) -> io::Result<PathBuf> {
    if links > 40 {
        return Err(io::Error::other("too many symbolic links"));
    }
    let mut resolved = PathBuf::new();
    for part in std::path::absolute(path)?.components() {
        match part {
            std::path::Component::ParentDir => {
                resolved.pop();
            }
            std::path::Component::CurDir => (),
            part => {
                resolved.push(part);
                match fs::symlink_metadata(&resolved) {
                    Ok(meta) if meta.file_type().is_symlink() => {
                        // Preserve filesystem errors (e.g. a target `file/..`).
                        // Only dangling targets need the non-strict resolver.
                        match fs::canonicalize(&resolved) {
                            Ok(real) => {
                                resolved = real;
                                continue;
                            }
                            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
                            Err(e) => return Err(e),
                        }
                        let target = fs::read_link(&resolved)?;
                        let target = if target.is_absolute() {
                            target
                        } else {
                            resolved
                                .parent()
                                .ok_or_else(|| io::Error::other("link has no parent"))?
                                .join(target)
                        };
                        resolved = resolve_links(&target, links + 1)?;
                    }
                    Ok(_) => (),
                    Err(e) if e.kind() == io::ErrorKind::NotFound => (),
                    Err(e) => return Err(e),
                }
            }
        }
    }
    Ok(resolved)
}

/// The shared name, or None for local files and an unselected workspace.
pub fn shared_relative_path(path: &Path, root: Option<&Path>) -> io::Result<Option<PathBuf>> {
    let Some(root) = root else {
        return Ok(None);
    };
    let base = resolved(&root.join(".guildbotics"))?;
    let path = resolved(path)?;
    Ok(path
        .strip_prefix(base)
        .ok()
        .filter(|p| p.starts_with("config") || p.starts_with("state"))
        .map(Path::to_path_buf))
}

/// Only this guard can acquire a workspace guard. A mutable borrow prevents
/// holding two workspace locks or reacquiring the identity through this guard.
pub struct IdentityLock {
    root: PathBuf,
    _lock: FileLock,
}
pub struct WorkspaceLock<'a> {
    identity: &'a mut IdentityLock,
    root: PathBuf,
    _lock: Option<FileLock>,
}

impl IdentityLock {
    pub fn acquire(root: &Path, timeout: Duration) -> anyhow::Result<Self> {
        let root = resolved(root)?;
        Ok(Self {
            _lock: hold(&root, timeout)?,
            root,
        })
    }
    /// A workspace cannot be acquired without an identity capability.
    ///
    /// ```compile_fail
    /// use gb_base::shared_writes::WorkspaceLock;
    /// WorkspaceLock::acquire(std::path::Path::new("/workspace"));
    /// ```
    pub fn workspace(
        &mut self,
        root: &Path,
        timeout: Duration,
    ) -> anyhow::Result<WorkspaceLock<'_>> {
        let root = resolved(root)?;
        let lock = if root == self.root {
            None
        } else {
            Some(hold(&root, timeout)?)
        };
        Ok(WorkspaceLock {
            identity: self,
            root,
            _lock: lock,
        })
    }
    pub fn writer(&mut self) -> Writer<'_> {
        Writer {
            roots: vec![&self.root],
            unlocked: None,
        }
    }
}
impl WorkspaceLock<'_> {
    pub fn writer(&mut self) -> Writer<'_> {
        Writer {
            roots: vec![&self.identity.root, &self.root],
            unlocked: None,
        }
    }
}

/// A borrowed write capability. Helpers within a larger operation reuse it.
pub struct Writer<'a> {
    roots: Vec<&'a Path>,
    unlocked: Option<&'a SharedWrites>,
}
impl Writer<'_> {
    pub fn check(&self, path: &Path) -> anyhow::Result<()> {
        if let Some(writes) = self.unlocked {
            anyhow::ensure!(
                shared_relative_path(path, Some(&writes.identity))?.is_none()
                    && shared_relative_path(path, writes.workspace.as_deref())?.is_none(),
                "a shared path requires a held lock"
            );
            return Ok(());
        }
        let path = resolved(path)?;
        for root in &self.roots {
            if path.starts_with(resolved(&root.join(".guildbotics"))?) {
                return Ok(());
            }
        }
        anyhow::bail!("write lies outside the held roots")
    }
    pub fn write_bytes(&mut self, path: &Path, data: &[u8]) -> anyhow::Result<()> {
        self.check(path)?;
        Ok(files::atomic_write(path, data)?)
    }
    pub fn write_text(&mut self, path: &Path, text: &str) -> anyhow::Result<()> {
        self.write_bytes(path, text.as_bytes())
    }
    pub fn write_json(&mut self, path: &Path, value: &impl serde::Serialize) -> anyhow::Result<()> {
        self.write_text(path, &files::dump_shared_json(value))
    }
    pub fn delete(&mut self, path: &Path) -> anyhow::Result<()> {
        self.check(path)?;
        match fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.into()),
        }
    }
    /// Remove a dedicated configuration directory without following child links.
    pub fn remove_tree(&mut self, path: &Path) -> anyhow::Result<()> {
        self.check(path)?;
        Ok(fs::remove_dir_all(path)?)
    }
    pub fn append_text(&mut self, path: &Path, text: &str) -> anyhow::Result<()> {
        self.check(path)?;
        fs::create_dir_all(
            path.parent()
                .ok_or_else(|| io::Error::other("missing parent"))?,
        )?;
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)?;
        Ok(file.write_all(text.as_bytes())?)
    }
    pub fn update_text(
        &mut self,
        path: &Path,
        apply: impl FnOnce(Option<&str>) -> anyhow::Result<Option<String>>,
    ) -> anyhow::Result<Option<String>> {
        self.check(path)?;
        let current = files::read_if_present(path)?
            .map(String::from_utf8)
            .transpose()?;
        let updated = apply(current.as_deref())?;
        match &updated {
            None => self.delete(path)?,
            Some(text) if Some(text) != current.as_ref() => self.write_text(path, text)?,
            _ => (),
        }
        Ok(updated)
    }
    pub fn update_json(
        &mut self,
        path: &Path,
        apply: impl FnOnce(Option<serde_json::Value>) -> anyhow::Result<Option<serde_json::Value>>,
    ) -> anyhow::Result<Option<serde_json::Value>> {
        let mut result = None;
        self.update_text(path, |current| {
            result = apply(current.map(serde_json::from_str).transpose()?)?;
            Ok(result.as_ref().map(files::dump_shared_json))
        })?;
        Ok(result)
    }
}

/// Roots are explicit: None means no workspace is selected. Identity files
/// remain shared independently of that selection.
pub struct SharedWrites {
    pub identity: PathBuf,
    pub workspace: Option<PathBuf>,
    pub timeout: Duration,
}
impl SharedWrites {
    pub fn new(identity: PathBuf, workspace: Option<PathBuf>) -> Self {
        Self {
            identity,
            workspace,
            timeout: SHARED_WRITE_TIMEOUT,
        }
    }
    pub fn transaction<T>(
        &self,
        apply: impl FnOnce(&mut Writer<'_>) -> anyhow::Result<T>,
    ) -> anyhow::Result<T> {
        let mut identity = IdentityLock::acquire(&self.identity, self.timeout)?;
        match &self.workspace {
            Some(root) => apply(&mut identity.workspace(root, self.timeout)?.writer()),
            None => apply(&mut identity.writer()),
        }
    }
    /// Local and unselected paths acquire no lock. The callback still gets
    /// the same writing API, so its read and write cannot straddle the lock.
    pub fn at<T>(
        &self,
        path: &Path,
        apply: impl FnOnce(&mut Writer<'_>) -> anyhow::Result<T>,
    ) -> anyhow::Result<T> {
        if shared_relative_path(path, Some(&self.identity))?.is_some() {
            return apply(&mut IdentityLock::acquire(&self.identity, self.timeout)?.writer());
        }
        if shared_relative_path(path, self.workspace.as_deref())?.is_some() {
            return self.transaction(apply);
        }
        apply(&mut Writer {
            roots: vec![],
            unlocked: Some(self),
        })
    }
}
