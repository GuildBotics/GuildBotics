//! Apply a guest's regular-file changes under a held directory descriptor.
use crate::Sentence;
use base64::{Engine, engine::general_purpose::STANDARD};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use unicode_normalization::UnicodeNormalization;

pub mod transfer;

pub const SOURCE: &str = "/run/guildbotics/worktree";
#[derive(Debug, Clone, PartialEq)]
pub struct Copy {
    pub host: PathBuf,
    pub guest: String,
    pub excluded: Vec<String>,
    pub lock: PathBuf,
    identity: Option<Arc<same_file::Handle>>,
    pub upload: Option<Arc<transfer::Upload>>,
    pub returned: transfer::Returned,
}
impl Copy {
    pub fn received(
        guest: String,
        upload: Arc<transfer::Upload>,
        returned: transfer::Returned,
    ) -> Self {
        Self {
            host: PathBuf::new(),
            guest,
            excluded: vec![".guildbotics".into()],
            lock: PathBuf::new(),
            identity: None,
            upload: Some(upload),
            returned,
        }
    }
    pub fn validate(&self) -> Result<(), String> {
        if self.upload.is_some() {
            return Ok(());
        }
        gb_base::host_path::directory(&self.host, false).map_err(|e| e.to_string())?;
        if identity(&open_root(&self.host)?)? != **self.identity.as_ref().unwrap() {
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
            identity: Some(Arc::new(identity(&file)?)),
            upload: None,
            returned: transfer::Returned::default(),
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
pub(crate) fn validate_received_name(value: &str, excluded: &[String]) -> Result<(), String> {
    name(value, excluded, true).map(|_| ())
}
fn name(value: &str, excluded: &[String], sensitive: bool) -> Result<PathBuf, String> {
    let p = Path::new(value);
    if value.is_empty()
        || value.contains(['\\', '\0'])
        || (cfg!(windows) && value.contains(':'))
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
fn check_size(copied: usize, changes: usize) -> Result<(), String> {
    if copied > 64 * 1024 * 1024 || changes > 1024 * 1024 * 1024 {
        return Err("Worktree response exceeds its limit".into());
    }
    Ok(())
}
type DecodedChange = (PathBuf, Change, Option<Vec<u8>>);
fn records(
    copy: &Copy,
    copied: &[u8],
    changes: &[u8],
    sensitive: bool,
) -> Result<(BTreeMap<String, Copied>, Vec<DecodedChange>), String> {
    check_size(copied.len(), changes.len())?;
    let mut originals = BTreeMap::new();
    for line in copied.split(|b| *b == b'\n').filter(|s| !s.is_empty()) {
        let record: Copied = serde_json::from_slice(line).map_err(|e| e.to_string())?;
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
pub use portable::{drain_writebacks, write_back, write_back_async};

fn message(copy: &Copy, key: &'static str, mut params: Vec<(&'static str, String)>) -> Sentence {
    params.insert(0, ("path", copy.host.display().to_string()));
    Sentence::Key(key, params)
}
fn refused(copy: &Copy, reason: String) -> Sentence {
    message(
        copy,
        "intelligences.agent_environment.runtime.worktree_refused",
        vec![("reason", reason)],
    )
}

fn feedback_text(error: &Sentence) -> String {
    match error {
        Sentence::Text(text) => text.clone(),
        Sentence::AtPath(path, reason) => format!("{}: {}", path.display(), feedback_text(reason)),
        Sentence::Key(key, params) => {
            static LOCALE: std::sync::LazyLock<serde_json::Value> = std::sync::LazyLock::new(
                || {
                    gb_config::yaml_value(include_str!("../../../guildbotics/templates/locales/intelligences/agent_environment.en.yml")).expect("bundled environment locale")
                },
            );
            let mut text = LOCALE["en"]["runtime"][key.rsplit('.').next().unwrap()]
                .as_str()
                .expect("worktree error translation")
                .to_owned();
            for (name, value) in params {
                text = text.replace(&format!("%{{{name}}}"), value);
            }
            text
        }
    }
}

#[derive(Default)]
struct Cancellation {
    requested: AtomicBool,
    stopped: AtomicBool,
    outcome: Mutex<Option<Result<(), Sentence>>>,
}
impl Cancellation {
    fn check(&self) -> Result<(), String> {
        if self.requested.load(Ordering::Acquire) {
            self.stopped.store(true, Ordering::Release);
            Err("Worktree write-back cancelled".into())
        } else {
            Ok(())
        }
    }
    fn log(&self, copy: &Copy, result: &Result<(), Sentence>) {
        match result {
            Ok(()) => tracing::warn!(
                "The cancelled command's changes were written back to {}.",
                copy.host.display()
            ),
            Err(error) if !self.stopped.load(Ordering::Acquire) => {
                tracing::error!(
                    "The cancelled command's write-back failed: {}",
                    feedback_text(error)
                )
            }
            Err(_) => {}
        }
    }
}
struct CancelOnDrop {
    state: Arc<Cancellation>,
    copy: Copy,
    armed: bool,
}
impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        if self.armed {
            let outcome = self.state.outcome.lock().unwrap();
            self.state.requested.store(true, Ordering::Release);
            if let Some(result) = outcome.as_ref() {
                self.state.log(&self.copy, result);
            }
        }
    }
}

mod portable {
    use super::*;
    use cap_fs_ext::{DirExt, FollowSymlinks, OpenOptionsFollowExt};
    use cap_std::fs::{Dir, OpenOptions as CapOpen};
    static WRITERS: std::sync::LazyLock<tokio_util::task::TaskTracker> =
        std::sync::LazyLock::new(tokio_util::task::TaskTracker::new);
    /// Finish cancelled commands' write-back cleanup before the host shuts down.
    pub async fn drain_writebacks() {
        WRITERS.close();
        WRITERS.wait().await;
    }
    pub(super) fn open_root(path: &Path) -> Result<Dir, String> {
        if !path.is_absolute() {
            return Err("Working directory must be absolute".into());
        }
        gb_base::host_path::open_directory(path, false).map_err(|e| e.to_string())
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
        cancelled: &Cancellation,
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
            cancelled.check()?;
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
    pub fn write_back(copy: &Copy, copied: &[u8], changes: &[u8]) -> Result<(), Sentence> {
        let _held = gb_base::files::FileLock::hold(&copy.lock, std::time::Duration::from_secs(600))
            .map_err(|e| refused(copy, e.to_string()))?;
        write_back_held(copy, copied, changes)
    }
    pub async fn write_back_async(
        copy: &Copy,
        copied: &[u8],
        changes: &[u8],
    ) -> Result<(), Sentence> {
        write_back_async_with(copy, copied, changes, || {}, || {}, |_| {}).await
    }
    async fn write_back_async_with(
        copy: &Copy,
        copied: &[u8],
        changes: &[u8],
        before_stage: impl FnOnce() + Send + 'static,
        before_commit: impl FnOnce() + Send + 'static,
        before_put: impl FnMut(usize) + Send + 'static,
    ) -> Result<(), Sentence> {
        let held = tokio::time::timeout(std::time::Duration::from_secs(600), async {
            loop {
                if let Some(held) =
                    gb_base::files::FileLock::try_hold(&copy.lock).map_err(|e| e.to_string())?
                {
                    return Ok::<_, String>(held);
                }
                tokio::time::sleep(std::time::Duration::from_millis(25)).await;
            }
        })
        .await
        .map_err(|_| refused(copy, "Worktree write-back lock timed out".into()))?
        .map_err(|reason| refused(copy, reason))?;
        let state = Arc::new(Cancellation::default());
        let mut guard = CancelOnDrop {
            state: state.clone(),
            copy: copy.clone(),
            armed: true,
        };
        let owned = copy.clone();
        let copied = copied.to_vec();
        let changes = changes.to_vec();
        let dispatch = tracing::dispatcher::get_default(Clone::clone);
        let result = WRITERS
            .spawn_blocking(move || {
                let _held = held;
                let _dispatch = tracing::dispatcher::set_default(&dispatch);
                let result = transaction_cancelled(
                    &owned,
                    &copied,
                    &changes,
                    &state,
                    before_stage,
                    before_commit,
                    before_put,
                );
                let mut outcome = state.outcome.lock().unwrap();
                if state.requested.load(Ordering::Acquire) {
                    state.log(&owned, &result);
                }
                *outcome = Some(result.clone());
                result
            })
            .await
            .map_err(|error| refused(copy, error.to_string()))?;
        guard.armed = false;
        result
    }
    fn write_back_held(copy: &Copy, copied: &[u8], changes: &[u8]) -> Result<(), Sentence> {
        transaction(copy, copied, changes, || {}, || {})
    }
    fn transaction(
        copy: &Copy,
        copied: &[u8],
        changes: &[u8],
        before_stage: impl FnOnce(),
        before_commit: impl FnOnce(),
    ) -> Result<(), Sentence> {
        transaction_cancelled(
            copy,
            copied,
            changes,
            &Cancellation::default(),
            before_stage,
            before_commit,
            |_| {},
        )
    }
    fn transaction_cancelled(
        copy: &Copy,
        copied: &[u8],
        changes: &[u8],
        cancelled: &Cancellation,
        before_stage: impl FnOnce(),
        before_commit: impl FnOnce(),
        mut before_put: impl FnMut(usize),
    ) -> Result<(), Sentence> {
        let (root, planned, conflicts) = (|| -> Result<_, String> {
            cancelled.check()?;
            let root = open_root(&copy.host)?;
            if identity(&root.try_clone().map_err(|e| e.to_string())?.into_std_file())?
                != **copy.identity.as_ref().unwrap()
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
            let mut conflicts = Vec::new();
            for (path, change, bytes) in changes {
                cancelled.check()?;
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
                let current = state(directory.as_ref(), &path, cancelled)?;
                match (originals.get(&change.path), &current) {
                    (None, None) => {}
                    (Some(before), Some((hash, executable, _)))
                        if before.sha256 == *hash
                            && (cfg!(windows)
                                || change.executable.is_none()
                                || before.executable == *executable) => {}
                    _ => {
                        conflicts.push(change.path);
                        continue;
                    }
                }
                if change.deleted.unwrap_or(false) && current.is_none() {
                    continue;
                }
                planned.push((path, bytes, change.executable, current.map(|s| s.2)));
            }
            Ok((root, planned, conflicts))
        })()
        .map_err(|reason| refused(copy, reason))?;
        if !conflicts.is_empty() {
            return Err(message(
                copy,
                "intelligences.agent_environment.runtime.worktree_conflict",
                vec![("paths", conflicts.join(", "))],
            ));
        }
        before_stage();
        let mut staged: Vec<(Dir, PathBuf, Option<PathBuf>, PathBuf)> = Vec::new();
        let stage = (|| -> Result<(), String> {
            for (path, bytes, executable, permissions) in planned {
                cancelled.check()?;
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
                        path,
                    ));
                    for chunk in bytes.chunks(65536) {
                        cancelled.check()?;
                        file.write_all(chunk).map_err(|e| e.to_string())?;
                    }
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
                    staged.push((dir, destination, None, path));
                }
            }
            before_commit();
            cancelled.check()?;
            Ok(())
        })();
        let mut written = Vec::new();
        let commit = if stage.is_ok() {
            staged
                .iter()
                .try_for_each(|(dir, destination, temporary, path)| {
                    before_put(written.len());
                    match temporary {
                        Some(temporary) => dir.rename(temporary, dir, destination),
                        None => dir.remove_file(destination),
                    }
                    .map_err(|e| {
                        message(
                            copy,
                            "intelligences.agent_environment.runtime.worktree_partial",
                            vec![("reason", e.to_string()), ("paths", written.join(", "))],
                        )
                    })?;
                    written.push(
                        path.to_string_lossy()
                            .replace(std::path::MAIN_SEPARATOR, "/"),
                    );
                    Ok(())
                })
        } else {
            Ok(())
        };
        for (dir, _, temporary, _) in &staged {
            if let Some(temporary) = temporary {
                let _ = dir.remove_file(temporary);
            }
        }
        stage.map_err(|reason| refused(copy, reason)).and(commit)
    }
    #[cfg(test)]
    #[derive(Clone, Default)]
    struct Logs(Arc<Mutex<Vec<(tracing::Level, String)>>>);
    #[cfg(test)]
    impl tracing::Subscriber for Logs {
        fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
            tracing::span::Id::from_u64(1)
        }
        fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
        fn event(&self, event: &tracing::Event<'_>) {
            struct Text(String);
            impl tracing::field::Visit for Text {
                fn record_debug(&mut self, _: &tracing::field::Field, value: &dyn std::fmt::Debug) {
                    self.0.push_str(&format!("{value:?}"));
                }
            }
            let mut text = Text(String::new());
            event.record(&mut text);
            self.0
                .lock()
                .unwrap()
                .push((*event.metadata().level(), text.0));
        }
        fn enter(&self, _: &tracing::span::Id) {}
        fn exit(&self, _: &tracing::span::Id) {}
    }
    #[tokio::test]
    async fn staged_cancellation_keeps_the_loop_free_discards_files_and_drains_worker() {
        cancellation_case("staged").await;
    }
    #[tokio::test]
    async fn commit_after_cancellation_finishes_and_logs_written_changes() {
        cancellation_case("committing").await;
    }
    #[tokio::test]
    async fn commit_failure_after_cancellation_logs_reason_and_written_paths() {
        cancellation_case("failing").await;
    }
    #[cfg(test)]
    async fn cancellation_case(phase: &'static str) {
        let root = tempfile::tempdir().unwrap();
        let copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        let logs = Logs::default();
        let _subscriber = tracing::subscriber::set_default(logs.clone());
        let (reached, waiting) = tokio::sync::oneshot::channel();
        let reached = Arc::new(Mutex::new(Some(reached)));
        let (release, held) = std::sync::mpsc::channel();
        let held = Arc::new(Mutex::new(held));
        let pause = Arc::new(move || {
            reached.lock().unwrap().take().unwrap().send(()).unwrap();
            held.lock()
                .unwrap()
                .recv_timeout(std::time::Duration::from_secs(5))
                .unwrap();
        });
        let stage_pause = pause.clone();
        let owned = copy.clone();
        let blocked = root.path().join("blocked");
        let task = tokio::spawn(async move {
            write_back_async_with(
                &owned,
                b"",
                br#"{"path":"good","content":"eA=="}
{"path":"blocked","content":"eA=="}"#,
                || {},
                move || {
                    if phase == "staged" {
                        stage_pause();
                    }
                },
                move |index| {
                    if phase != "staged" && index == 0 {
                        pause();
                    }
                    if phase == "failing" && index == 1 {
                        std::fs::create_dir(&blocked).unwrap();
                    }
                },
            )
            .await
        });
        tokio::time::timeout(std::time::Duration::from_secs(2), waiting)
            .await
            .unwrap()
            .unwrap();
        // This test uses a single-thread runtime: a blocked worker must not block this timer.
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        let mut draining = Box::pin(drain_writebacks());
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(5), &mut draining)
                .await
                .is_err()
        );
        release.send(()).unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(2), draining)
            .await
            .unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            loop {
                if gb_base::files::FileLock::try_hold(&copy.lock)
                    .unwrap()
                    .is_some()
                {
                    break;
                }
                tokio::time::sleep(std::time::Duration::from_millis(2)).await;
            }
        })
        .await
        .unwrap();
        assert!(!std::fs::read_dir(root.path()).unwrap().any(|entry| {
            entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".guildbotics-")
        }));
        let logs = logs.0.lock().unwrap();
        if phase == "staged" {
            assert!(!root.path().join("good").exists());
            assert!(!root.path().join("blocked").exists());
            assert!(logs.is_empty(), "{logs:?}");
        } else {
            assert_eq!(std::fs::read(root.path().join("good")).unwrap(), b"x");
            assert_eq!(logs.len(), 1, "{logs:?}");
            let (level, message) = &logs[0];
            if phase == "failing" {
                assert_eq!(*level, tracing::Level::ERROR);
                assert!(message.contains("write-back failed"));
                assert!(message.contains("stopped partway"));
                assert!(!message.contains("worktree_partial"));
                assert!(message.contains("good"));
                assert!(
                    !message
                        .split("stopped partway (")
                        .nth(1)
                        .unwrap()
                        .split(");")
                        .next()
                        .unwrap()
                        .is_empty()
                );
                assert!(message.contains("these were written back: good"));
            } else {
                assert_eq!(*level, tracing::Level::WARN);
                assert!(message.contains("were written back"));
                assert!(message.contains(&root.path().display().to_string()));
                assert_eq!(std::fs::read(root.path().join("blocked")).unwrap(), b"x");
            }
        }
    }
    #[cfg(test)]
    fn assert_feedback(error: &Sentence, copy: &Copy, kind: &str, paths: &str) {
        let Sentence::Key(key, params) = error else {
            panic!("Expected translated feedback: {error:?}")
        };
        assert_eq!(
            *key,
            format!("intelligences.agent_environment.runtime.worktree_{kind}")
        );
        let params: BTreeMap<_, _> = params
            .iter()
            .map(|(key, value)| (*key, value.as_str()))
            .collect();
        let root = copy.host.display().to_string();
        assert_eq!(params["path"], root);
        let reason = params.get("reason").copied().unwrap_or_default();
        if kind != "conflict" {
            assert!(!reason.is_empty());
        }
        if kind != "refused" {
            assert_eq!(params["paths"], paths);
        }
        for (language, source) in [
            (
                "en",
                include_str!(
                    "../../../guildbotics/templates/locales/intelligences/agent_environment.en.yml"
                ),
            ),
            (
                "ja",
                include_str!(
                    "../../../guildbotics/templates/locales/intelligences/agent_environment.ja.yml"
                ),
            ),
        ] {
            let locale = gb_config::yaml_value(source).unwrap();
            let mut rendered = locale[language]["runtime"][format!("worktree_{kind}")]
                .as_str()
                .unwrap()
                .to_owned();
            for (name, value) in &params {
                rendered = rendered.replace(&format!("%{{{name}}}"), value);
            }
            let expected = match (language, kind) {
                ("en", "conflict") => format!(
                    "The command's changes were not written back to {root}, because these files changed there while it ran: {paths}"
                ),
                ("ja", "conflict") => format!(
                    "コマンドの実行中に次のファイルが {root} で変更されたため、コマンドの変更を書き戻しませんでした: {paths}"
                ),
                ("en", "partial") => format!(
                    "Writing the command's changes back to {root} stopped partway ({reason}); these were written back: {paths}"
                ),
                ("ja", "partial") => format!(
                    "コマンドの変更を {root} に書き戻す途中で失敗しました（{reason}）。書き戻したファイル: {paths}"
                ),
                ("en", "refused") => {
                    format!("The command's changes were not written back to {root}: {reason}")
                }
                ("ja", "refused") => {
                    format!("コマンドの変更を {root} に書き戻しませんでした: {reason}")
                }
                _ => unreachable!(),
            };
            assert_eq!(rendered, expected);
        }
    }
    #[test]
    fn conflicts_report_every_changed_created_and_deleted_path_before_writing() {
        let root = tempfile::tempdir().unwrap();
        let copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        for name in ["changed", "created", "deleted"] {
            std::fs::write(root.path().join(name), b"host").unwrap();
        }
        let copied = ["changed", "deleted", "missing"]
            .map(|path| {
                serde_json::json!({"path":path,"sha256":"old hash","executable":false}).to_string()
            })
            .join("\n");
        let changes = br#"{"path":"good","content":"eA=="}
{"path":"changed","content":"eA=="}
{"path":"created","content":"eA=="}
{"path":"deleted","deleted":true}
{"path":"missing","content":"eA=="}"#;
        let error = write_back(&copy, copied.as_bytes(), changes).unwrap_err();
        assert_feedback(
            &error,
            &copy,
            "conflict",
            "changed, created, deleted, missing",
        );
        assert!(!root.path().join("good").exists());
        for name in ["changed", "created", "deleted"] {
            assert_eq!(std::fs::read(root.path().join(name)).unwrap(), b"host");
        }
    }
    #[cfg(unix)]
    #[test]
    fn executable_conflicts_and_non_regular_files_keep_their_diagnostic_kind() {
        use std::os::unix::fs::PermissionsExt;
        let root = tempfile::tempdir().unwrap();
        let copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        for name in ["first", "second"] {
            std::fs::write(root.path().join(name), b"host").unwrap();
            std::fs::set_permissions(
                root.path().join(name),
                std::fs::Permissions::from_mode(0o700),
            )
            .unwrap();
        }
        let copied = ["first", "second"].map(|path| serde_json::json!({"path":path,"sha256":Sha256::digest(b"host").iter().map(|byte| format!("{byte:02x}")).collect::<String>(),"executable":false}).to_string()).join("\n");
        let changes = br#"{"path":"first","content":"eA==","executable":true}
{"path":"second","content":"eA==","executable":true}"#;
        assert_feedback(
            &write_back(&copy, copied.as_bytes(), changes).unwrap_err(),
            &copy,
            "conflict",
            "first, second",
        );
        std::fs::create_dir(root.path().join("directory")).unwrap();
        std::os::unix::fs::symlink(root.path().join("first"), root.path().join("link")).unwrap();
        for path in ["directory", "link"] {
            let changes = serde_json::json!({"path":path,"content":"eA=="}).to_string();
            assert_feedback(
                &write_back(&copy, b"", changes.as_bytes()).unwrap_err(),
                &copy,
                "refused",
                "",
            );
        }
    }
    #[tokio::test]
    async fn asynchronous_lock_errors_are_translated_refusals() {
        let root = tempfile::tempdir().unwrap();
        let mut copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        std::fs::write(root.path().join("not-directory"), b"x").unwrap();
        copy.lock = root.path().join("not-directory/lock");
        assert_feedback(
            &write_back_async(&copy, b"", b"").await.unwrap_err(),
            &copy,
            "refused",
            "",
        );
    }
    #[test]
    fn malformed_payload_unsafe_paths_replaced_root_and_lock_errors_are_refused() {
        let root = tempfile::tempdir().unwrap();
        let mut copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        for (copied, changes) in [
            (b"".as_slice(), b"invalid".as_slice()),
            (b"invalid", b""),
            (b"", br#"{"path":"../escape","content":"eA=="}"#),
            (b"", br#"{"path":"a","content":"invalid base64"}"#),
        ] {
            assert_feedback(
                &write_back(&copy, copied, changes).unwrap_err(),
                &copy,
                "refused",
                "",
            );
        }
        std::fs::write(root.path().join("not-directory"), b"x").unwrap();
        copy.lock = root.path().join("not-directory/lock");
        assert_feedback(
            &write_back(&copy, b"", b"").unwrap_err(),
            &copy,
            "refused",
            "",
        );
        let outer = tempfile::tempdir().unwrap();
        let host = outer.path().join("root");
        std::fs::create_dir(&host).unwrap();
        let copy = Copy::new(
            host.clone(),
            "/work".into(),
            vec![],
            outer.path().join("lock"),
        )
        .unwrap();
        #[cfg(unix)]
        {
            std::fs::rename(&host, outer.path().join("moved")).unwrap();
            std::fs::create_dir(&host).unwrap();
            assert_feedback(
                &write_back(&copy, b"", b"").unwrap_err(),
                &copy,
                "refused",
                "",
            );
        }
        #[cfg(windows)]
        {
            // The held root capability prevents replacement on Windows.
            let moved = outer.path().join("moved");
            let error = std::fs::rename(&host, &moved).unwrap_err();
            assert_eq!(error.raw_os_error(), Some(32), "{error}");
            assert!(host.is_dir());
            assert!(!moved.exists());
            write_back(&copy, b"", b"").unwrap();
            drop(copy);
            std::fs::rename(&host, moved).unwrap();
        }
    }
    #[test]
    fn worktree_payload_limits_include_the_exact_boundary() {
        assert!(check_size(64 * 1024 * 1024, 1024 * 1024 * 1024).is_ok());
        assert!(check_size(64 * 1024 * 1024 + 1, 0).is_err());
        assert!(check_size(0, 1024 * 1024 * 1024 + 1).is_err());
    }
    #[test]
    fn staging_failure_cleans_temporary_files_without_committing_good_changes() {
        let root = tempfile::tempdir().unwrap();
        let copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        let error = transaction(&copy, b"", b"{\"path\":\"good\",\"content\":\"eA==\"}\n{\"path\":\"parent/file\",\"content\":\"eA==\"}",
            || std::fs::write(root.path().join("parent"), b"not a directory").unwrap(), || {}).unwrap_err();
        assert_feedback(&error, &copy, "refused", "");
        assert!(!root.path().join("good").exists());
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 1);
    }
    #[test]
    fn partial_commit_failure_is_reported_and_unused_staged_files_are_removed() {
        let root = tempfile::tempdir().unwrap();
        let copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        std::fs::write(root.path().join("deleted"), b"old").unwrap();
        let baseline = serde_json::to_vec(&serde_json::json!({"path":"deleted","sha256":Sha256::digest(b"old").iter().map(|byte| format!("{byte:02x}")).collect::<String>(),"executable":false})).unwrap();
        let error = transaction(&copy, &baseline, b"{\"path\":\"nested/good\",\"content\":\"eA==\"}\n{\"path\":\"deleted\",\"deleted\":true}\n{\"path\":\"blocked\",\"content\":\"eA==\"}",
            || {}, || std::fs::create_dir(root.path().join("blocked")).unwrap()).unwrap_err();
        assert_feedback(&error, &copy, "partial", "nested/good, deleted");
        assert_eq!(
            std::fs::read(root.path().join("nested/good")).unwrap(),
            b"x"
        );
        assert!(root.path().join("blocked").is_dir());
        assert!(!root.path().join("deleted").exists());
        assert_eq!(
            std::fs::read_dir(root.path().join("nested"))
                .unwrap()
                .count(),
            1
        );
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 2);
    }
    #[cfg(unix)]
    #[test]
    fn directory_swapped_after_preflight_is_never_followed() {
        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        std::fs::create_dir(root.path().join("parent")).unwrap();
        let copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        assert!(
            transaction(
                &copy,
                b"",
                br#"{"path":"parent/file","content":"eA=="}"#,
                || {
                    std::fs::rename(root.path().join("parent"), root.path().join("moved")).unwrap();
                    std::os::unix::fs::symlink(outside.path(), root.path().join("parent")).unwrap();
                },
                || {}
            )
            .is_err()
        );
        assert!(!outside.path().join("file").exists());
    }
    #[tokio::test]
    async fn a_waiting_writer_detects_the_earlier_writers_change_after_lock_release() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("file"), b"old").unwrap();
        let copy = Copy::new(
            root.path().to_owned(),
            "/work".into(),
            vec![],
            root.path().join("lock"),
        )
        .unwrap();
        let baseline = serde_json::to_vec(&serde_json::json!({"path":"file","sha256":Sha256::digest(b"old").iter().map(|byte|format!("{byte:02x}")).collect::<String>(),"executable":false})).unwrap();
        let held = gb_base::files::FileLock::try_hold(&copy.lock)
            .unwrap()
            .unwrap();
        let mut waiting = Box::pin(write_back_async(
            &copy,
            &baseline,
            br#"{"path":"file","content":"c2Vjb25k"}"#,
        ));
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(10), &mut waiting)
                .await
                .is_err()
        );
        transaction(
            &copy,
            &baseline,
            br#"{"path":"file","content":"Zmlyc3Q="}"#,
            || {},
            || {},
        )
        .unwrap();
        drop(held);
        assert_feedback(&waiting.await.unwrap_err(), &copy, "conflict", "file");
        assert_eq!(std::fs::read(root.path().join("file")).unwrap(), b"first");
    }
    #[test]
    fn colon_names_follow_the_host_platform() {
        for path in ["notes/12:00.txt", "C:/relative", "a:stream"] {
            assert_eq!(name(path, &[], true).is_err(), cfg!(windows), "{path}");
        }
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
        for bad in ["../outside", "excluded/a", ".git/config"] {
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
