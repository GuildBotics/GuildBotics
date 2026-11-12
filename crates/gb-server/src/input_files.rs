//! Uploads are retained independently and selected explicitly by each run.
use crate::api::{ApiError, Shared};
use axum::{
    Json,
    extract::{Multipart, State, multipart::MultipartRejection},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
use cap_std::fs::{Dir, OpenOptions};
use serde_json::json;
use std::{
    io::Write,
    path::{Path, PathBuf},
    time::SystemTime,
};
pub const MAX_BYTES: usize = 20 * 1024 * 1024;
pub const REQUEST_LIMIT: usize = MAX_BYTES + 64 * 1024;
const NAME_CHARS: usize = 60;
#[derive(Clone)]
pub struct InputFileStore {
    root: PathBuf,
}
struct Pending {
    file: Option<std::fs::File>,
    directory: Dir,
    temporary: String,
}
impl Write for Pending {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.file.as_mut().unwrap().write(bytes)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.file.as_mut().unwrap().flush()
    }
}
impl Pending {
    fn persist(mut self, destination: &Path) -> std::io::Result<()> {
        drop(self.file.take());
        let name = destination.file_name().ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "Invalid upload name")
        })?;
        self.directory
            .rename(&self.temporary, &self.directory, name)
    }
}
impl Drop for Pending {
    fn drop(&mut self) {
        drop(self.file.take());
        let _ = self.directory.remove_file(&self.temporary);
    }
}

impl InputFileStore {
    #[cfg(test)]
    pub fn new(root: &Path) -> anyhow::Result<Self> {
        let store = Self::deferred(root);
        store.ensure()?;
        Ok(store)
    }
    pub fn deferred(root: &Path) -> Self {
        Self { root: root.into() }
    }
    fn ensure(&self) -> anyhow::Result<Dir> {
        Ok(gb_base::host_path::open_directory(&self.root, true)?)
    }
    pub async fn problem(&self) -> String {
        let store = self.clone();
        blocking(move || store.prune())
            .await
            .err()
            .map(|e| e.to_string())
            .unwrap_or_default()
    }
    pub async fn mounts(&self, ids: &[String]) -> anyhow::Result<Vec<gb_environment::Mount>> {
        if ids.is_empty() {
            return Ok(vec![]);
        }
        anyhow::ensure!(ids.len() <= 100, "Too many uploads");
        let store = self.clone();
        let ids = ids.to_vec();
        blocking(move || {
            let root = store.ensure()?;
            let mut mounts = Vec::new();
            for id in ids {
                anyhow::ensure!(
                    id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit()),
                    "Invalid upload ID"
                );
                let dir = root.open_dir_nofollow(&id)?;
                anyhow::ensure!(
                    SystemTime::now()
                        .duration_since(dir.dir_metadata()?.modified()?.into_std())
                        .unwrap_or_default()
                        < gb_environment::run_files::RETENTION,
                    "Upload expired"
                );
                let host = gb_base::host_path::normalize(&store.root)?.join(id);
                mounts.push(gb_environment::Mount {
                    guest: gb_environment::guest_path(&host)
                        .map_err(|_| anyhow::anyhow!("Input directory has no guest path"))?,
                    host: Some(host),
                    readonly: true,
                    quota_mib: None,
                });
            }
            Ok(mounts)
        })
        .await
    }
    pub fn prune(&self) -> anyhow::Result<()> {
        let root = self.ensure()?;
        for entry in root.entries()? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.len() != 32 || !name.bytes().all(|b| b.is_ascii_hexdigit()) {
                continue;
            }
            let Ok(dir) = root.open_dir_nofollow(&name) else {
                continue;
            };
            if SystemTime::now()
                .duration_since(dir.dir_metadata()?.modified()?.into_std())
                .unwrap_or_default()
                >= gb_environment::run_files::RETENTION
            {
                dir.remove_open_dir_all()?;
            }
        }
        Ok(())
    }
    fn pending(&self, filename: Option<&str>) -> anyhow::Result<(Pending, PathBuf)> {
        self.prune()?;
        let root = self.ensure()?;
        let id = uuid::Uuid::new_v4().simple().to_string();
        root.create_dir(&id)?;
        let directory = root.open_dir_nofollow(&id)?;
        let raw = filename
            .unwrap_or("")
            .rsplit(['/', '\\'])
            .next()
            .unwrap_or("")
            .trim();
        let name: String = raw
            .chars()
            .filter(|c| !c.is_control())
            .take(NAME_CHARS)
            .collect();
        let name = if name.is_empty() || name == "." || name == ".." {
            "file"
        } else {
            &name
        };
        let temporary = ".upload".to_owned();
        let file = directory
            .open_with(
                &temporary,
                OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .follow(FollowSymlinks::No),
            )?
            .into_std();
        let destination = gb_base::host_path::normalize(&self.root)?
            .join(id)
            .join(name);
        Ok((
            Pending {
                file: Some(file),
                directory,
                temporary,
            },
            destination,
        ))
    }
}
async fn blocking<T: Send + 'static>(
    work: impl FnOnce() -> anyhow::Result<T> + Send + 'static,
) -> anyhow::Result<T> {
    tokio::task::spawn_blocking(work).await?
}

/// A cancelled request releases its temporary file on the blocking pool too.
struct Upload(Option<Pending>);
impl Drop for Upload {
    fn drop(&mut self) {
        if let Some(pending) = self.0.take() {
            tokio::task::spawn_blocking(move || drop(pending));
        }
    }
}
impl Upload {
    async fn write(mut self, chunk: axum::body::Bytes) -> anyhow::Result<Self> {
        let mut pending = self.0.take().expect("upload owns its pending file");
        blocking(move || {
            pending.write_all(&chunk)?;
            Ok(Self(Some(pending)))
        })
        .await
    }
    async fn persist(mut self, destination: PathBuf) -> anyhow::Result<()> {
        let pending = self.0.take().expect("upload owns its pending file");
        blocking(move || Ok(pending.persist(&destination)?)).await
    }
    async fn discard(mut self) {
        let pending = self.0.take();
        let _ = blocking(move || {
            drop(pending);
            Ok(())
        })
        .await;
    }
}

pub(crate) async fn upload(
    State(api): State<Shared>,
    headers: HeaderMap,
    multipart: Result<Multipart, MultipartRejection>,
) -> Response {
    let invalid = |reason: &str| {
        ApiError::new(
            StatusCode::BAD_REQUEST,
            "command_input_file_invalid",
            &headers,
        )
        .reason(reason.into())
        .into_response()
    };
    let failed = || {
        ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "command_input_file_save_failed",
            &headers,
        )
        .into_response()
    };
    let Ok(mut multipart) = multipart else {
        return ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .into_response();
    };
    loop {
        let field = match multipart.next_field().await {
            Ok(Some(field)) => field,
            Ok(None) => {
                return ApiError::new(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "validation_error",
                    &headers,
                )
                .into_response();
            }
            Err(_) => return invalid("Could not read the uploaded file."),
        };
        if field.name() != Some("file") {
            continue;
        }
        let store = api.inputs.clone();
        let filename = field.file_name().map(str::to_owned);
        let (pending, destination) =
            match blocking(move || store.pending(filename.as_deref())).await {
                Ok(upload) => upload,
                Err(error) => {
                    tracing::error!("Cannot create command input file: {error}");
                    return failed();
                }
            };
        let mut pending = Upload(Some(pending));
        let mut field = field;
        let mut size = 0;
        loop {
            let chunk = match field.chunk().await {
                Ok(Some(chunk)) => chunk,
                Ok(None) => break,
                Err(_) => {
                    pending.discard().await;
                    return invalid(
                        "Could not read the uploaded file, or the request exceeded its size limit.",
                    );
                }
            };
            size += chunk.len();
            if size > MAX_BYTES {
                pending.discard().await;
                return invalid("File is too large (max 20 MB).");
            }
            pending = match pending.write(chunk).await {
                Ok(pending) => pending,
                Err(error) => {
                    tracing::error!("Cannot write command input file: {error}");
                    return failed();
                }
            };
        }
        if size == 0 {
            pending.discard().await;
            return invalid("File is empty.");
        }
        if let Err(error) = pending.persist(destination.clone()).await {
            tracing::error!("Cannot save command input file: {error}");
            return failed();
        }
        let guest = match gb_environment::guest_path(&destination) {
            Ok(path) => path,
            Err(_) => return failed(),
        };
        return Json(json!({"id":destination.parent().unwrap().file_name().unwrap(),"path":destination,"guest_path":guest})).into_response();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn uploads_are_independent_persistent_and_explicitly_selected() {
        let root = tempfile::tempdir().unwrap();
        let store = InputFileStore::new(root.path()).unwrap();
        let (mut first, a) = store.pending(Some("../../a.txt")).unwrap();
        first.write_all(b"a").unwrap();
        first.persist(&a).unwrap();
        let (mut second, b) = store.pending(Some("b.txt")).unwrap();
        second.write_all(b"b").unwrap();
        second.persist(&b).unwrap();
        assert_ne!(a.parent(), b.parent());
        assert!(store.mounts(&[]).await.unwrap().is_empty());
        let id = a
            .parent()
            .unwrap()
            .file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned();
        let mounts = store.mounts(std::slice::from_ref(&id)).await.unwrap();
        assert_eq!(mounts.len(), 1);
        assert!(mounts[0].readonly);
        assert_eq!(mounts[0].host.as_deref(), a.parent());
        drop(store);
        let reopened = InputFileStore::new(root.path()).unwrap();
        assert_eq!(reopened.mounts(&[id]).await.unwrap().len(), 1);
        assert!(reopened.mounts(&["../outside".into()]).await.is_err());
    }
}
