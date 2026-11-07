//! Multipart uploads owned by one server session and mounted read-only.
use std::{
    io::Write,
    path::{Path, PathBuf},
    time::Duration,
};

use axum::{
    Json,
    extract::{Multipart, State, multipart::MultipartRejection},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use cap_fs_ext::{DirExt, FollowSymlinks, MetadataExt, OpenOptionsFollowExt};
use cap_std::fs::{Dir, OpenOptions};
use gb_base::files::FileLock;
use serde_json::json;

use crate::api::{ApiError, Shared};

pub const MAX_BYTES: usize = 20 * 1024 * 1024;
pub const REQUEST_LIMIT: usize = MAX_BYTES + 64 * 1024;
const NAME_CHARS: usize = 60;
const LOCK_TIMEOUT: Duration = Duration::from_secs(5);

/// Session ownership uses held directories so replacing an ancestor redirects no IO.
#[derive(Clone)]
pub struct InputFileStore {
    root_path: PathBuf,
    directory: PathBuf,
    name: String,
    session: std::sync::Arc<std::sync::OnceLock<Session>>,
    initializing: std::sync::Arc<std::sync::Mutex<()>>,
}
struct Session {
    directory: PathBuf,
    lock: Option<FileLock>,
    session: Option<Dir>,
}
impl Drop for Session {
    fn drop(&mut self) {
        let lock = self.lock.take();
        let session = self.session.take().expect("session owns its directory");
        let cleanup = move || {
            drop(lock);
            let _ = session.remove_open_dir_all();
        };
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn_blocking(cleanup);
        } else {
            cleanup();
        }
    }
}
fn try_lock(directory: &Dir, name: &str) -> std::io::Result<Option<FileLock>> {
    let mut options = OpenOptions::new();
    options
        .read(true)
        .write(true)
        .create(true)
        .follow(FollowSymlinks::No);
    #[cfg(unix)]
    {
        use cap_std::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    FileLock::try_file(directory.open_with(name, &options)?.into_std())
}
fn hold_lock(directory: &Dir, name: &str) -> std::io::Result<FileLock> {
    let deadline = std::time::Instant::now() + LOCK_TIMEOUT;
    loop {
        if let Some(lock) = try_lock(directory, name)? {
            return Ok(lock);
        }
        if std::time::Instant::now() >= deadline {
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "Input session lock is busy",
            ));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
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
    /// Upload storage is optional at startup and retried on demand.
    pub fn deferred(root: &Path) -> Self {
        let name = format!("session-{}", uuid::Uuid::new_v4().simple());
        Self {
            root_path: root.into(),
            directory: root.join(&name),
            name,
            session: std::sync::Arc::default(),
            initializing: std::sync::Arc::default(),
        }
    }
    fn ensure(&self) -> anyhow::Result<&Session> {
        if let Some(session) = self.session.get() {
            return Ok(session);
        }
        let _held = self
            .initializing
            .lock()
            .map_err(|_| anyhow::anyhow!("Input session initialization failed"))?;
        if let Some(session) = self.session.get() {
            return Ok(session);
        }
        let root_path = gb_base::host_path::normalize(&self.root_path)?;
        let root = gb_base::host_path::open_directory(&root_path, true)?;
        let _cleanup = hold_lock(&root, ".cleanup.lock")?;
        for entry in root.entries()? {
            let entry = entry?;
            if !entry.file_name().to_string_lossy().starts_with("session-")
                || !entry.file_type()?.is_dir()
            {
                continue;
            }
            let Ok(session) = root.open_dir_nofollow(entry.file_name()) else {
                continue;
            };
            if let Ok(Some(lock)) = try_lock(&session, ".session.lock") {
                drop(lock);
                drop(session);
                root.remove_dir_all(entry.file_name())?;
            }
        }
        let name = self.name.clone();
        let mut options = cap_std::fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use cap_std::fs::DirBuilderExt;
            options.mode(0o700);
        }
        root.create_dir_with(&name, &options)?;
        let session = root.open_dir_nofollow(&name)?;
        let lock = hold_lock(&session, ".session.lock")?;
        let _ = self.session.set(Session {
            directory: root_path.join(&name),
            lock: Some(lock),
            session: Some(session),
        });
        Ok(self
            .session
            .get()
            .expect("input session initialized under lock"))
    }

    fn current_session(&self) -> anyhow::Result<&Session> {
        let session = self.ensure()?;
        let current = gb_base::host_path::open_directory(&session.directory, false)?;
        let current = current.dir_metadata()?;
        let held = session
            .session
            .as_ref()
            .expect("session owns its directory")
            .dir_metadata()?;
        anyhow::ensure!(
            current.dev() == held.dev() && current.ino() == held.ino(),
            "Input session directory was replaced"
        );
        Ok(session)
    }

    pub fn directory(&self) -> &Path {
        self.session
            .get()
            .map(|session| session.directory.as_path())
            .unwrap_or(&self.directory)
    }

    /// Retry optional storage without making command startup depend on it.
    pub async fn problem(&self) -> String {
        let store = self.clone();
        blocking(move || store.mount().map(|_| ()))
            .await
            .err()
            .map(|error| error.to_string())
            .unwrap_or_default()
    }
    /// Never accepts a caller's source directory or guest destination.
    pub async fn mount_if_available(&self) -> Option<gb_environment::Mount> {
        self.session.get()?;
        let store = self.clone();
        match blocking(move || store.mount()).await {
            Ok(mount) => Some(mount),
            Err(error) => {
                tracing::warn!(
                    "Input upload storage is unavailable; commands continue without its mount: {error}"
                );
                None
            }
        }
    }
    fn mount(&self) -> anyhow::Result<gb_environment::Mount> {
        let directory = self.current_session()?.directory.clone();
        Ok(gb_environment::Mount {
            guest: gb_environment::guest_path(&directory)
                .map_err(|_| anyhow::anyhow!("Input directory has no guest path"))?,
            host: Some(directory),
            readonly: true,
            quota_mib: None,
        })
    }

    fn pending(&self, filename: Option<&str>) -> anyhow::Result<(Pending, PathBuf)> {
        let session = self.current_session()?;
        let session_directory = session
            .session
            .as_ref()
            .expect("session owns its directory");
        let name = filename
            .unwrap_or("")
            .rsplit(['/', '\\'])
            .next()
            .unwrap_or("")
            .trim();
        let name = name
            .chars()
            .rev()
            .take(NAME_CHARS)
            .collect::<String>()
            .chars()
            .rev()
            .filter(|c| !c.is_control())
            .collect::<String>();
        let name = if name.is_empty() || name == "." || name == ".." {
            "file"
        } else {
            &name
        };
        let random = format!("upload-{}", uuid::Uuid::new_v4().simple());
        let temporary = format!("{random}.upload");
        let mut options = OpenOptions::new();
        options
            .write(true)
            .create_new(true)
            .follow(FollowSymlinks::No);
        #[cfg(unix)]
        {
            use cap_std::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = session_directory
            .open_with(&temporary, &options)?
            .into_std();
        let destination = self.directory().join(format!("{random}-{name}"));
        Ok((
            Pending {
                file: Some(file),
                directory: session_directory.try_clone()?,
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
        return Json(json!({"path":destination,"guest_path":guest})).into_response();
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn concurrent_deferred_uploads_share_one_live_session() {
        let root = tempfile::tempdir().unwrap();
        let store = InputFileStore::deferred(&root.path().join("inputs"));
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
        let threads: Vec<_> = (0..8)
            .map(|_| {
                let store = store.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    barrier.wait();
                    let (mut pending, path) = store.pending(Some("image.png")).unwrap();
                    pending.write_all(b"image").unwrap();
                    pending.persist(&path).unwrap();
                    path
                })
            })
            .collect();
        let files: Vec<_> = threads.into_iter().map(|t| t.join().unwrap()).collect();
        let parents: std::collections::BTreeSet<_> =
            files.iter().map(|p| p.parent().unwrap()).collect();
        assert_eq!(parents.len(), 1);
        assert_eq!(
            std::fs::read_dir(root.path().join("inputs"))
                .unwrap()
                .filter_map(Result::ok)
                .filter(|e| e.path().is_dir()
                    && e.file_name().to_string_lossy().starts_with("session-"))
                .count(),
            1
        );
        for path in &files {
            assert_eq!(std::fs::read(path).unwrap(), b"image");
        }
        let session = files[0].parent().unwrap().to_owned();
        drop(store);
        assert!(!session.exists());
    }
    use super::*;
    #[test]
    fn deferred_upload_destinations_use_the_held_normalized_directory() {
        let temp = tempfile::tempdir().unwrap();
        let alias = temp.path().join("parent");
        std::fs::create_dir(&alias).unwrap();
        let store = InputFileStore::deferred(&alias.join("./inputs"));
        let (_, destination) = store.pending(Some("file.txt")).unwrap();
        let mount = store.mount().unwrap();
        assert_eq!(destination.parent(), mount.host.as_deref());
        assert!(
            gb_environment::guest_path(&destination)
                .unwrap()
                .starts_with(&format!("{}/", mount.guest))
        );
    }
    #[test]
    fn input_store_problem_clears_after_successful_retry() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("inputs");
        std::fs::write(&root, b"blocked").unwrap();
        let store = InputFileStore::deferred(&root);
        assert!(store.pending(Some("first.txt")).is_err());
        assert!(store.session.get().is_none());
        std::fs::remove_file(&root).unwrap();
        let (mut pending, path) = store.pending(Some("retry.txt")).unwrap();
        pending.write_all(b"retained").unwrap();
        pending.persist(&path).unwrap();
        assert_eq!(std::fs::read(path).unwrap(), b"retained");
        assert!(store.mount().unwrap().readonly);
    }
    #[test]
    fn session_files_are_private_and_the_mount_cannot_be_caller_selected() {
        let root = tempfile::tempdir().unwrap();
        let first = InputFileStore::new(root.path()).unwrap();
        let first_path = first.directory().to_owned();
        let second = InputFileStore::new(root.path()).unwrap();
        assert!(first_path.exists());
        assert_ne!(first_path, second.directory());
        let mount = first.mount().unwrap();
        assert!(mount.readonly);
        assert_eq!(mount.host.as_deref(), Some(first.directory()));
        assert_eq!(Path::new(&mount.guest), first.directory());
        drop(first);
        assert!(!first_path.exists());
        assert!(second.directory().exists());
    }
    #[test]
    fn upload_names_are_leaf_names_and_concurrent_names_differ() {
        let root = tempfile::tempdir().unwrap();
        let store = InputFileStore::new(root.path()).unwrap();
        for name in [
            "../../outside.txt",
            r"C:\\Users\\user\\outside.txt",
            "",
            ".",
            "..",
            "bad\0name",
        ] {
            let (mut pending, path) = store.pending(Some(name)).unwrap();
            assert_eq!(path.parent(), Some(store.directory()));
            assert!(!path.file_name().unwrap().to_string_lossy().contains('\0'));
            pending.write_all(b"file body").unwrap();
            pending.persist(&path).unwrap();
            assert_eq!(std::fs::read(path).unwrap(), b"file body");
        }
        let (_, first) = store.pending(Some("same.txt")).unwrap();
        let (_, second) = store.pending(Some("same.txt")).unwrap();
        assert_ne!(first, second);
    }
    #[test]
    fn orphan_sessions_are_removed_and_links_are_not_followed() {
        let root = tempfile::tempdir().unwrap();
        let orphan = root.path().join("session-orphan");
        std::fs::create_dir(&orphan).unwrap();
        std::fs::write(orphan.join("old"), "old").unwrap();
        let _active = InputFileStore::new(root.path()).unwrap();
        assert!(!orphan.exists());
        #[cfg(unix)]
        {
            let outside = tempfile::tempdir().unwrap();
            std::os::unix::fs::symlink(outside.path(), root.path().join("session-link")).unwrap();
            let _another = InputFileStore::new(root.path()).unwrap();
            assert!(outside.path().exists());
        }
    }
    #[test]
    fn unusable_orphan_locks_do_not_block_a_new_session() {
        let root = tempfile::tempdir().unwrap();
        let orphan = root.path().join("session-unreadable");
        std::fs::create_dir_all(orphan.join(".session.lock")).unwrap();
        let sentinel = orphan.join("keep");
        std::fs::write(&sentinel, b"old session").unwrap();
        let store = InputFileStore::new(root.path()).unwrap();
        assert!(store.directory().is_dir());
        assert_eq!(std::fs::read(sentinel).unwrap(), b"old session");
        #[cfg(unix)]
        {
            let linked = root.path().join("session-linked");
            std::fs::create_dir(&linked).unwrap();
            let canary = root.path().join("canary");
            std::fs::write(&canary, b"do not lock or change").unwrap();
            std::os::unix::fs::symlink(&canary, linked.join(".session.lock")).unwrap();
            let _another = InputFileStore::new(root.path()).unwrap();
            assert!(linked.exists());
            assert_eq!(std::fs::read(canary).unwrap(), b"do not lock or change");
        }
    }

    #[cfg(unix)]
    #[test]
    fn uploads_and_cleanup_keep_the_held_session_when_ancestors_are_swapped() {
        let fixture = tempfile::tempdir().unwrap();
        let root = fixture.path().join("inputs");
        let store = InputFileStore::new(&root).unwrap();
        let session = store.directory().file_name().unwrap().to_owned();
        let (mut pending, destination) = store.pending(Some("result.txt")).unwrap();
        let original = fixture.path().join("original");
        std::fs::rename(&root, &original).unwrap();
        let outside = fixture.path().join("outside");
        let unrelated_session = outside.join(&session);
        std::fs::create_dir_all(&unrelated_session).unwrap();
        let sentinel = unrelated_session.join(destination.file_name().unwrap());
        std::fs::write(&sentinel, b"private outside content").unwrap();
        std::os::unix::fs::symlink(&outside, &root).unwrap();
        assert!(store.pending(Some("after-swap.txt")).is_err());
        pending.write_all(b"uploaded content").unwrap();
        pending.persist(&destination).unwrap();
        assert_eq!(
            std::fs::read(&sentinel).unwrap(),
            b"private outside content"
        );
        drop(store);
        assert!(!original.join(&session).exists());
        assert_eq!(
            std::fs::read(&sentinel).unwrap(),
            b"private outside content"
        );
    }

    #[tokio::test]
    async fn directory_replacement_is_refused_and_cleanup_removes_only_the_owned_session() {
        for replace_root in [false, true] {
            let fixture = tempfile::tempdir().unwrap();
            let root = fixture.path().join("inputs");
            let store = InputFileStore::new(&root).unwrap();
            let session = store.directory().file_name().unwrap().to_owned();
            let original = fixture.path().join("original");
            let replaced = if replace_root {
                &root
            } else {
                store.directory()
            };
            std::fs::rename(replaced, &original).unwrap();
            let replacement = root.join(&session);
            std::fs::create_dir_all(&replacement).unwrap();
            let sentinel = replacement.join("keep.txt");
            std::fs::write(&sentinel, b"unrelated replacement").unwrap();
            assert!(store.pending(Some("file.txt")).is_err());
            assert!(store.mount_if_available().await.is_none());
            assert!(!store.problem().await.is_empty());
            drop(store);
            // Await the blocking cleanup rather than relying on scheduling.
            tokio::time::timeout(Duration::from_secs(1), async {
                loop {
                    if !original
                        .join(if replace_root {
                            Path::new(&session)
                        } else {
                            Path::new("")
                        })
                        .exists()
                    {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            assert_eq!(std::fs::read(&sentinel).unwrap(), b"unrelated replacement");
        }
    }

    #[cfg(unix)]
    #[test]
    fn input_directories_and_files_have_owner_only_permissions() {
        use std::os::unix::fs::PermissionsExt;
        let fixture = tempfile::tempdir().unwrap();
        let store = InputFileStore::new(fixture.path()).unwrap();
        let (mut pending, destination) = store.pending(Some("file.txt")).unwrap();
        pending.write_all(b"content").unwrap();
        pending.persist(&destination).unwrap();
        assert_eq!(
            std::fs::metadata(store.directory())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(destination).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }

    fn api(root: &Path) -> Shared {
        std::sync::Arc::new(crate::api::Api::new(
            Default::default(),
            InputFileStore::deferred(&root.join("command_inputs")),
            crate::auth::ServerAuth::open(root, 8765).unwrap(),
            "fixture".into(),
            root.into(),
        ))
    }

    async fn request(
        api: Shared,
        name: &str,
        filename: &str,
        mime: &str,
        content: &[u8],
        authorized: bool,
    ) -> Response {
        use tower::ServiceExt as _;
        let mut body=format!("--fixture\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n").into_bytes();
        body.extend_from_slice(content);
        body.extend_from_slice(b"\r\n--fixture--\r\n");
        let mut builder = axum::http::Request::builder()
            .method("POST")
            .uri("/api/commands/input-files")
            .header("host", "127.0.0.1:8765")
            .header("content-type", "multipart/form-data; boundary=fixture");
        if authorized {
            let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
            builder = builder.header("authorization", format!("Bearer {}", token.trim()));
        }
        crate::api::router(api)
            .oneshot(builder.body(axum::body::Body::from(body)).unwrap())
            .await
            .unwrap()
    }

    async fn value(response: Response) -> serde_json::Value {
        serde_json::from_slice(
            &axum::body::to_bytes(response.into_body(), 1024 * 1024)
                .await
                .unwrap(),
        )
        .unwrap()
    }

    async fn get(api: Shared, path: &str) -> Response {
        use tower::ServiceExt as _;
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        crate::api::router(api)
            .oneshot(
                axum::http::Request::builder()
                    .uri(path)
                    .header("host", "127.0.0.1:8765")
                    .header("authorization", format!("Bearer {}", token.trim()))
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap()
    }

    #[tokio::test]
    async fn config_status_reports_input_store_problem_and_retries_after_repair() {
        let root = tempfile::tempdir().unwrap();
        let inputs = root.path().join("command_inputs");
        std::fs::write(&inputs, b"blocked").unwrap();
        let api = api(root.path());
        assert!(
            api.inputs.session.get().is_none(),
            "optional uploads must not prevent startup"
        );
        let response = get(api.clone(), "/api/config/status").await;
        assert_eq!(response.status(), StatusCode::OK);
        let status = value(response).await;
        assert!(!status["input_store_problem"].as_str().unwrap().is_empty());
        assert!(api.inputs.mount_if_available().await.is_none());
        std::fs::remove_file(&inputs).unwrap();
        let response = get(api.clone(), "/api/config/status").await;
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(value(response).await["input_store_problem"], "");
        assert!(api.inputs.mount_if_available().await.unwrap().readonly);
        let response = request(
            api.clone(),
            "file",
            "retry.txt",
            "text/plain",
            b"repaired",
            true,
        )
        .await;
        assert_eq!(response.status(), StatusCode::OK);
        let uploaded = value(response).await;
        assert_eq!(
            std::fs::read(uploaded["path"].as_str().unwrap()).unwrap(),
            b"repaired"
        );
        let original = root.path().join("original-inputs");
        std::fs::rename(&inputs, &original).unwrap();
        std::fs::write(&inputs, b"replaced initialized storage").unwrap();
        let status = value(get(api.clone(), "/api/config/status").await).await;
        assert!(!status["input_store_problem"].as_str().unwrap().is_empty());
        assert!(api.inputs.mount_if_available().await.is_none());
        std::fs::remove_file(&inputs).unwrap();
        std::fs::rename(&original, &inputs).unwrap();
        assert_eq!(
            value(get(api, "/api/config/status").await).await["input_store_problem"],
            ""
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn input_initialization_lock_does_not_block_other_requests() {
        for upload in [false, true] {
            let root = tempfile::tempdir().unwrap();
            let inputs = root.path().join("command_inputs");
            std::fs::create_dir(&inputs).unwrap();
            let lock = FileLock::hold(&inputs.join(".cleanup.lock"), Duration::ZERO).unwrap();
            let api = api(root.path());
            let waiting_api = api.clone();
            let mut waiting = tokio::spawn(async move {
                if upload {
                    request(
                        waiting_api,
                        "file",
                        "file.txt",
                        "text/plain",
                        b"uploaded",
                        true,
                    )
                    .await
                } else {
                    get(waiting_api, "/api/config/status").await
                }
            });
            assert!(
                tokio::time::timeout(Duration::from_millis(30), &mut waiting)
                    .await
                    .is_err(),
                "initialization must wait for the lock without occupying Tokio"
            );
            let response =
                tokio::time::timeout(Duration::from_millis(100), get(api, "/api/health"))
                    .await
                    .unwrap();
            assert_eq!(response.status(), StatusCode::OK);
            drop(lock);
            let response = tokio::time::timeout(Duration::from_secs(1), waiting)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(response.status(), StatusCode::OK);
        }
    }

    #[tokio::test]
    async fn owner_upload_preserves_arbitrary_mime_and_returns_only_session_paths() {
        let root = tempfile::tempdir().unwrap();
        let api = api(root.path());
        let response = request(
            api.clone(),
            "file",
            r"C:\Users\someone\outside.bin",
            "application/octet-stream",
            b"\0binary body",
            true,
        )
        .await;
        assert_eq!(response.status(), StatusCode::OK);
        let result = value(response).await;
        let path = Path::new(result["path"].as_str().unwrap());
        assert_eq!(
            result["guest_path"],
            gb_environment::guest_path(path).unwrap()
        );
        assert_eq!(path.parent(), Some(api.inputs.directory()));
        assert!(
            path.file_name()
                .unwrap()
                .to_string_lossy()
                .ends_with("outside.bin")
        );
        assert_eq!(std::fs::read(path).unwrap(), b"\0binary body");
        let disguised = request(
            api.clone(),
            "file",
            "../../image.png",
            "text/plain",
            b"not actually a png",
            true,
        )
        .await;
        assert_eq!(disguised.status(), StatusCode::OK);
        let result = value(disguised).await;
        assert_eq!(
            Path::new(result["path"].as_str().unwrap()).parent(),
            Some(api.inputs.directory())
        );
    }

    #[tokio::test]
    async fn empty_oversized_missing_and_unauthenticated_uploads_are_rejected() {
        let root = tempfile::tempdir().unwrap();
        let api = api(root.path());
        let unauthorized = request(
            api.clone(),
            "file",
            "file",
            "text/plain",
            b"secret-shaped fixture",
            false,
        )
        .await;
        assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);
        let missing = request(
            api.clone(),
            "directory",
            "/outside",
            "text/plain",
            b"ignored",
            true,
        )
        .await;
        assert_eq!(missing.status(), StatusCode::UNPROCESSABLE_ENTITY);
        for bytes in [vec![], vec![b'x'; MAX_BYTES + 1]] {
            let response =
                request(api.clone(), "file", "large.dat", "image/png", &bytes, true).await;
            assert_eq!(response.status(), StatusCode::BAD_REQUEST);
            let error = value(response).await;
            assert_eq!(error["code"], "command_input_file_invalid");
        }
        // Failed uploads leave no partial or complete data file.
        assert_eq!(
            std::fs::read_dir(api.inputs.directory()).unwrap().count(),
            1
        );
    }
}
