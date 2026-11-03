//! Compare, apply and observe config revisions in one shared-write span.
use gb_base::{
    files::read_if_present,
    shared_writes::{SharedWrites, Writer, resolved},
};
use sha1::{Digest, Sha1};
use std::{
    collections::BTreeMap,
    fmt, fs, io,
    path::{Component, Path, PathBuf},
};

pub type Revisions = BTreeMap<String, String>;
pub fn blob_id(data: &[u8]) -> String {
    let mut hash = Sha1::new();
    hash.update(format!("blob {}\0", data.len()));
    hash.update(data);
    hash.finalize().iter().map(|b| format!("{b:02x}")).collect()
}
#[derive(Debug)]
pub struct StaleConfigWriteError {
    pub relative_path: String,
    pub revisions: Revisions,
}
impl fmt::Display for StaleConfigWriteError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} changed since it was read", self.relative_path)
    }
}
impl std::error::Error for StaleConfigWriteError {}
pub struct ConfigSnapshot {
    pub relative_path: String,
    pub path: PathBuf,
    pub blob_id: String,
    pub content: String,
}
impl ConfigSnapshot {
    pub fn value(&self) -> anyhow::Result<serde_json::Value> {
        crate::yaml::value(&self.content)
            .map_err(|_| anyhow::anyhow!("invalid config YAML: {}", self.relative_path))
    }
}
pub struct ConfigWriteReceipt<T> {
    pub result: T,
    pub revisions: Revisions,
}
pub struct ConfigRepository {
    writes: SharedWrites,
    view: ConfigView,
}
/// Reads within an existing transaction do not acquire another lock.
pub struct ConfigView {
    config: PathBuf,
}
impl ConfigRepository {
    pub fn workspace_secrets<'a>(
        &'a self,
        device: crate::secrets::DeviceKey<'a>,
    ) -> anyhow::Result<crate::secrets::SecretStore<'a>> {
        crate::secrets::SecretStore::new(&self.writes, crate::secrets::Scope::Workspace, device)
    }
    pub fn identity_secrets<'a>(
        &'a self,
        device: crate::secrets::DeviceKey<'a>,
    ) -> anyhow::Result<crate::secrets::SecretStore<'a>> {
        crate::secrets::SecretStore::new(&self.writes, crate::secrets::Scope::Identity, device)
    }
    /// The identity repository shares the same outer lock as this workspace.
    pub fn identity_view(&self) -> ConfigView {
        ConfigView {
            config: self.writes.identity.join(".guildbotics/config"),
        }
    }
    pub fn new(writes: SharedWrites) -> anyhow::Result<Self> {
        let workspace = writes
            .workspace
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("No workspace selected"))?;
        let config = workspace.join(".guildbotics/config");
        Ok(Self {
            writes,
            view: ConfigView { config },
        })
    }
    pub fn read_config(&self, path: &str) -> anyhow::Result<Option<ConfigSnapshot>> {
        self.view.read_config(path)
    }
    pub fn revisions(&self, paths: &[&str]) -> anyhow::Result<Revisions> {
        self.writes.transaction(|_| self.view.revisions(paths))
    }
    pub fn tree_revisions(&self, dir: &str) -> anyhow::Result<Revisions> {
        self.writes.transaction(|_| self.view.tree_revisions(dir))
    }
    pub fn write<T>(
        &self,
        expected: &Revisions,
        apply: impl FnOnce(&ConfigView, &mut Writer<'_>) -> anyhow::Result<T>,
        report: impl FnOnce(&ConfigView) -> anyhow::Result<Revisions>,
    ) -> anyhow::Result<ConfigWriteReceipt<T>> {
        self.writes.transaction(|writer| {
            let paths: Vec<_> = expected.keys().map(String::as_str).collect();
            let current = self.view.revisions(&paths)?;
            for (path, revision) in expected {
                if current.get(path) != Some(revision) {
                    return Err(StaleConfigWriteError {
                        relative_path: format!("config/{}", path.trim_end_matches('/')),
                        revisions: current,
                    }
                    .into());
                }
            }
            let result = apply(&self.view, writer)?;
            Ok(ConfigWriteReceipt {
                result,
                revisions: report(&self.view)?,
            })
        })
    }
}
impl ConfigView {
    /// Validate both the spelling and resolved location, including symlinks.
    pub fn path(&self, name: &str) -> anyhow::Result<PathBuf> {
        let path = Path::new(name);
        anyhow::ensure!(
            !name.trim().is_empty()
                && !path.is_absolute()
                && !name.starts_with(['/', '\\'])
                && name.as_bytes().get(1).is_none_or(|b| *b != b':')
                && !path.components().any(|p| matches!(p, Component::ParentDir)),
            "invalid config path: {name}"
        );
        let joined = self.config.join(path);
        anyhow::ensure!(
            resolved(&joined)?.starts_with(resolved(&self.config)?),
            "config path escapes config/: {name}"
        );
        Ok(joined)
    }
    pub fn read_config(&self, name: &str) -> anyhow::Result<Option<ConfigSnapshot>> {
        let path = self.path(name)?;
        let Some(data) = read_if_present(&path)? else {
            return Ok(None);
        };
        Ok(Some(ConfigSnapshot {
            relative_path: format!("config/{name}"),
            path,
            blob_id: blob_id(&data),
            content: String::from_utf8(data)?,
        }))
    }
    fn tree_paths(&self, dir: &str) -> anyhow::Result<Vec<String>> {
        fn walk(view: &ConfigView, name: &str, paths: &mut Vec<String>) -> anyhow::Result<()> {
            let path = view.path(name)?;
            let entries = match fs::read_dir(path) {
                Ok(entries) => entries,
                Err(e)
                    if matches!(
                        e.kind(),
                        io::ErrorKind::NotFound | io::ErrorKind::NotADirectory
                    ) =>
                {
                    return Ok(());
                }
                Err(e) => return Err(e.into()),
            };
            for entry in entries {
                let entry = entry?;
                let child = Path::new(name)
                    .join(entry.file_name())
                    .components()
                    .filter(|part| *part != Component::CurDir)
                    .map(|part| {
                        part.as_os_str()
                            .to_str()
                            .ok_or_else(|| anyhow::anyhow!("non UTF-8 config path"))
                    })
                    .collect::<anyhow::Result<Vec<_>>>()?
                    .join("/");
                // Python's rglob does not recurse into symlink directories.
                let kind = entry.file_type()?;
                if kind.is_dir() {
                    walk(view, &child, paths)?;
                } else if entry.path().is_file() {
                    // Membership follows file links; content reads below still
                    // enforce containment. Directory links are never traversed.
                    paths.push(child);
                }
            }
            Ok(())
        }
        let mut paths = vec![];
        walk(self, dir, &mut paths)?;
        // pathlib compares components, not the slash-joined spelling.
        paths.sort_by(|a, b| a.split('/').cmp(b.split('/')));
        Ok(paths)
    }
    pub fn revisions(&self, paths: &[&str]) -> anyhow::Result<Revisions> {
        paths
            .iter()
            .map(|name| {
                let revision = if let Some(dir) = name.strip_suffix('/') {
                    blob_id(
                        self.tree_paths(dir)?
                            .iter()
                            .map(|p| format!("{p}\n"))
                            .collect::<String>()
                            .as_bytes(),
                    )
                } else {
                    self.read_config(name)?
                        .map(|s| s.blob_id)
                        .unwrap_or_default()
                };
                Ok((name.to_string(), revision))
            })
            .collect()
    }
    pub fn tree_revisions(&self, dir: &str) -> anyhow::Result<Revisions> {
        let mut paths = self.tree_paths(dir)?;
        paths.push(format!("{dir}/"));
        self.revisions(&paths.iter().map(String::as_str).collect::<Vec<_>>())
    }
}
