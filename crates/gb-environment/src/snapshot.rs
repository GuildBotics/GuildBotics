//! Compare the current recipe with the device's snapshots, without booting.

use crate::Sentence;
use gb_base::files::FileLock;
use std::path::{Path, PathBuf};

const PREFIX: &str = "guildbotics-";
const LOCK_FILE: &str = "build.lock";
const FAILED_SUFFIX: &str = ".failed";

static BUILD_TASKS: std::sync::LazyLock<tokio_util::task::TaskTracker> =
    std::sync::LazyLock::new(tokio_util::task::TaskTracker::new);

/// Wait for cancelled builds to finish VM cleanup before draining the runtime.
pub async fn drain_builds() {
    BUILD_TASKS.close();
    BUILD_TASKS.wait().await;
}

#[derive(Debug, Clone, PartialEq)]
pub enum State {
    Missing,
    Stale,
    Building,
    Failed(String),
    Ready,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Status {
    pub path: PathBuf,
    pub state: State,
}

pub fn snapshots_dir(workspace: &Path) -> PathBuf {
    workspace.join(".guildbotics/local/agent_environment/snapshots")
}

/// Only the current recipe's failure and directory affect readiness.
/// A running build takes precedence; filesystem errors are never "missing".
pub fn status(workspace: &Path, name: &str) -> std::io::Result<Status> {
    if !name.strip_prefix(PREFIX).is_some_and(|digest| {
        digest.len() == 16
            && digest
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    }) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "Invalid snapshot recipe name",
        ));
    }
    let directory = snapshots_dir(workspace);
    let path = directory.join(name);
    let lock = directory.join(LOCK_FILE);
    if lock.try_exists()? && FileLock::try_hold(&lock)?.is_none() {
        return Ok(Status {
            path,
            state: State::Building,
        });
    }
    let failed = directory.join(format!("{name}{FAILED_SUFFIX}"));
    let state = match std::fs::read_to_string(&failed) {
        Ok(detail) => State::Failed(detail.trim().to_owned()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            if path.try_exists()? && std::fs::symlink_metadata(&path)?.file_type().is_dir() {
                State::Ready
            } else {
                let mut stale = false;
                match std::fs::read_dir(&directory) {
                    Ok(entries) => {
                        for entry in entries {
                            let entry = entry?;
                            if entry.file_name().to_string_lossy().starts_with(PREFIX)
                                && entry.file_type()?.is_dir()
                            {
                                stale = true;
                            }
                        }
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    Err(e) => return Err(e),
                }
                if stale { State::Stale } else { State::Missing }
            }
        }
        Err(e) => return Err(e),
    };
    Ok(Status { path, state })
}

pub fn ready(workspace: &Path, name: &str) -> Result<PathBuf, Sentence> {
    let status = status(workspace, name).map_err(|e| Sentence::Text(e.to_string()))?;
    let key = match status.state {
        State::Ready => return Ok(status.path),
        State::Missing => "intelligences.agent_environment.snapshot.missing",
        State::Stale => "intelligences.agent_environment.snapshot.stale",
        State::Building => "intelligences.agent_environment.snapshot.building",
        State::Failed(detail) => {
            return Err(Sentence::Key(
                "intelligences.agent_environment.snapshot.failed",
                vec![("detail", detail)],
            ));
        }
    };
    Err(Sentence::key(key))
}

/// Whether automatic upkeep may build: never retry the current failed recipe.
pub fn needs_build(state: &State) -> bool {
    matches!(state, State::Missing | State::Stale)
}

/// Remove only environment snapshots and their failure markers under the build
/// lock. The caller removes each snapshot through its runtime's metadata API.
pub async fn remove<F, Fut>(workspace: &Path, mut perform: F) -> Result<(), String>
where
    F: FnMut(PathBuf) -> Fut,
    Fut: Future<Output = Result<(), String>>,
{
    let directory = snapshots_dir(workspace);
    let _held = FileLock::try_hold(&directory.join(LOCK_FILE))
        .map_err(|e| e.to_string())?
        .ok_or("An environment build is already running")?;
    for entry in std::fs::read_dir(&directory).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with(PREFIX) {
            continue;
        }
        let kind = entry.file_type().map_err(|e| e.to_string())?;
        if kind.is_dir() {
            perform(entry.path()).await?;
        } else if kind.is_file() && name.ends_with(FAILED_SUFFIX) {
            std::fs::remove_file(entry.path()).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Serialize builds across processes, remember failure only for this recipe,
/// and prune old artifacts only after a successful replacement.
pub async fn build<F, Fut, R, Removed>(
    workspace: &Path,
    name: &str,
    perform: F,
    remove: R,
) -> Result<Status, String>
where
    F: FnOnce(PathBuf, tokio_util::sync::CancellationToken) -> Fut + Send + 'static,
    Fut: Future<Output = Result<PathBuf, String>> + Send,
    R: FnMut(PathBuf) -> Removed + Send + 'static,
    Removed: Future<Output = Result<(), String>> + Send,
{
    owned_build(
        workspace,
        name,
        perform,
        remove,
        std::time::Duration::from_secs(30 * 60),
    )
    .await
}

async fn owned_build<F, Fut, R, Removed>(
    workspace: &Path,
    name: &str,
    perform: F,
    remove: R,
    timeout: std::time::Duration,
) -> Result<Status, String>
where
    F: FnOnce(PathBuf, tokio_util::sync::CancellationToken) -> Fut + Send + 'static,
    Fut: Future<Output = Result<PathBuf, String>> + Send,
    R: FnMut(PathBuf) -> Removed + Send + 'static,
    Removed: Future<Output = Result<(), String>> + Send,
{
    let workspace = workspace.to_owned();
    let name = name.to_owned();
    let cancelled = tokio_util::sync::CancellationToken::new();
    let cancel_on_drop = cancelled.clone().drop_guard();
    // The task keeps the lock through cleanup even if its caller goes away.
    let result = BUILD_TASKS
        .spawn(async move {
            build_with_timeout(&workspace, &name, perform, remove, timeout, cancelled).await
        })
        .await
        .map_err(|error| error.to_string())?;
    cancel_on_drop.disarm();
    result
}

async fn build_with_timeout<F, Fut, R, Removed>(
    workspace: &Path,
    name: &str,
    perform: F,
    mut remove: R,
    timeout: std::time::Duration,
    cancelled: tokio_util::sync::CancellationToken,
) -> Result<Status, String>
where
    F: FnOnce(PathBuf, tokio_util::sync::CancellationToken) -> Fut + Send + 'static,
    Fut: Future<Output = Result<PathBuf, String>> + Send,
    R: FnMut(PathBuf) -> Removed + Send + 'static,
    Removed: Future<Output = Result<(), String>> + Send,
{
    status(workspace, name).map_err(|e| e.to_string())?;
    let directory = snapshots_dir(workspace);
    let _held = FileLock::try_hold(&directory.join(LOCK_FILE))
        .map_err(|e| e.to_string())?
        .ok_or("An environment build is already running")?;
    let marker = directory.join(format!("{name}{FAILED_SUFFIX}"));
    match std::fs::remove_file(&marker) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.to_string()),
    }
    let work = perform(directory.clone(), cancelled.clone());
    tokio::pin!(work);
    let built = tokio::select! {
        biased;
        _ = cancelled.cancelled() => {
            let _ = work.await;
            Err("Environment build cancelled".into())
        }
        _ = tokio::time::sleep(timeout) => {
            cancelled.cancel();
            let _ = work.await;
            Err("Environment build exceeded 30 minutes".into())
        }
        result = &mut work => result,
    };
    match built {
        Err(reason) => {
            gb_base::files::atomic_write(&marker, format!("{reason}\n").as_bytes())
                .map_err(|e| e.to_string())?;
            Err(reason)
        }
        Ok(path) => {
            if path.parent() != Some(directory.as_path())
                || !std::fs::symlink_metadata(&path)
                    .map_err(|e| e.to_string())?
                    .is_dir()
            {
                return Err("Build returned an invalid snapshot".into());
            }
            for entry in std::fs::read_dir(&directory).map_err(|e| e.to_string())? {
                let entry = entry.map_err(|e| e.to_string())?;
                let other = entry.path();
                let name = entry.file_name().to_string_lossy().into_owned();
                if other != path && name.starts_with(PREFIX) {
                    let kind = entry.file_type().map_err(|e| e.to_string())?;
                    if kind.is_dir() {
                        remove(other).await?;
                    } else if name.ends_with(FAILED_SUFFIX) {
                        std::fs::remove_file(other).map_err(|e| e.to_string())?;
                    }
                }
            }
            Ok(Status {
                path,
                state: State::Ready,
            })
        }
    }
}

/// One interval loop owned by the server; aborting the task stops upkeep.
pub async fn upkeep<F, Fut>(mut once: F)
where
    F: FnMut() -> Fut,
    Fut: Future<Output = ()>,
{
    let mut interval = tokio::time::interval(std::time::Duration::from_secs(30));
    loop {
        interval.tick().await;
        once().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn timeouts_and_aborted_callers_keep_the_lock_through_late_boot_and_cleanup() {
        for abort in [false, true] {
            let root = tempfile::tempdir().unwrap();
            let name = "guildbotics-0123456789abcdef";
            let directory = snapshots_dir(root.path());
            let (started, boot_started) = tokio::sync::oneshot::channel();
            let (cancelled, cancellation_seen) = tokio::sync::oneshot::channel();
            let (release_boot, boot_released) = tokio::sync::oneshot::channel();
            let (cleaning, cleanup_started) = tokio::sync::oneshot::channel();
            let (release_cleanup, cleanup_released) = tokio::sync::oneshot::channel();
            let workspace = root.path().to_owned();
            let waiter = tokio::spawn(async move {
                owned_build(
                    &workspace,
                    name,
                    move |_, token| async move {
                        let cancellation = token.clone();
                        tokio::spawn(async move {
                            cancellation.cancelled().await;
                            let _ = cancelled.send(());
                        });
                        crate::runtime::build_lifecycle(
                            async move {
                                let _ = started.send(());
                                let _ = boot_released.await;
                                Ok(((), move || async move {
                                    let _ = cleaning.send(());
                                    let _ = cleanup_released.await;
                                }))
                            },
                            token,
                            |_| async { panic!("cancelled late boots must not run build steps") },
                        )
                        .await
                    },
                    |_| async { panic!("cancelled builds must not prune") },
                    if abort {
                        std::time::Duration::from_secs(60)
                    } else {
                        std::time::Duration::from_millis(10)
                    },
                )
                .await
            });
            boot_started.await.unwrap();
            if abort {
                waiter.abort();
            }
            tokio::time::timeout(std::time::Duration::from_secs(2), cancellation_seen)
                .await
                .unwrap()
                .unwrap();
            assert!(
                FileLock::try_hold(&directory.join(LOCK_FILE))
                    .unwrap()
                    .is_none()
            );
            assert_eq!(status(root.path(), name).unwrap().state, State::Building);
            release_boot.send(()).unwrap();
            cleanup_started.await.unwrap();
            assert!(
                FileLock::try_hold(&directory.join(LOCK_FILE))
                    .unwrap()
                    .is_none()
            );
            assert!(!directory.join(format!("{name}{FAILED_SUFFIX}")).exists());
            release_cleanup.send(()).unwrap();
            if abort {
                assert!(waiter.await.unwrap_err().is_cancelled());
            } else {
                assert_eq!(
                    waiter.await.unwrap(),
                    Err("Environment build exceeded 30 minutes".into())
                );
            }
            tokio::time::timeout(std::time::Duration::from_secs(2), async {
                loop {
                    if let Some(_held) = FileLock::try_hold(&directory.join(LOCK_FILE)).unwrap() {
                        break;
                    }
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            assert_eq!(
                status(root.path(), name).unwrap().state,
                State::Failed(
                    if abort {
                        "Environment build cancelled"
                    } else {
                        "Environment build exceeded 30 minutes"
                    }
                    .into()
                )
            );
        }
    }
    #[tokio::test]
    async fn a_timed_out_build_records_failure_releases_its_lock_and_keeps_old_snapshots() {
        let root = tempfile::tempdir().unwrap();
        let name = "guildbotics-0123456789abcdef";
        let directory = snapshots_dir(root.path());
        let old = directory.join("guildbotics-1111111111111111");
        std::fs::create_dir_all(&old).unwrap();
        let result = owned_build(
            root.path(),
            name,
            |_, cancelled| async move {
                cancelled.cancelled().await;
                Err("cancelled".into())
            },
            |_| async { panic!("timed-out build must not prune") },
            std::time::Duration::from_millis(10),
        )
        .await;
        assert_eq!(result, Err("Environment build exceeded 30 minutes".into()));
        assert_eq!(
            status(root.path(), name).unwrap().state,
            State::Failed("Environment build exceeded 30 minutes".into())
        );
        assert!(old.is_dir());
        assert!(
            FileLock::try_hold(&directory.join("build.lock"))
                .unwrap()
                .is_some()
        );
    }
}
