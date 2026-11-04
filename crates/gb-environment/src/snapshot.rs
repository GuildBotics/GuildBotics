//! Compare the current recipe with the device's snapshots, without booting.

use crate::Sentence;
use gb_base::files::FileLock;
use std::path::{Path, PathBuf};

const PREFIX: &str = "guildbotics-";
const LOCK_FILE: &str = "build.lock";
const FAILED_SUFFIX: &str = ".failed";

static BUILD_TASKS: std::sync::LazyLock<tokio_util::task::TaskTracker> =
    std::sync::LazyLock::new(tokio_util::task::TaskTracker::new);

/// Allow owned build tasks to stop before draining late VM cleanup.
pub async fn drain_builds() {
    BUILD_TASKS.close();
    let _ = tokio::time::timeout(std::time::Duration::from_secs(5), BUILD_TASKS.wait()).await;
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
    timeout_detail: String,
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
        timeout_detail,
        perform,
        remove,
        std::time::Duration::from_secs(30 * 60),
    )
    .await
}

async fn owned_build<F, Fut, R, Removed>(
    workspace: &Path,
    name: &str,
    timeout_detail: String,
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
    // The task observes caller cancellation and releases its lock independently.
    let result = BUILD_TASKS
        .spawn(async move {
            build_with_timeout(
                &workspace,
                &name,
                timeout_detail,
                perform,
                remove,
                timeout,
                cancelled,
            )
            .await
        })
        .await
        .map_err(|error| error.to_string())?;
    cancel_on_drop.disarm();
    result
}

async fn build_with_timeout<F, Fut, R, Removed>(
    workspace: &Path,
    name: &str,
    timeout_detail: String,
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
    enum End {
        Cancelled,
        TimedOut,
        Finished(Result<Status, String>),
    }
    let work = async {
        let path = perform(directory.clone(), cancelled.clone()).await?;
        if path.parent() != Some(directory.as_path())
            || !std::fs::symlink_metadata(&path)
                .map_err(|e| e.to_string())?
                .is_dir()
        {
            return Err("Build returned an invalid snapshot".into());
        }
        Ok(Status {
            path,
            state: State::Ready,
        })
    };
    let deadline = tokio::time::Instant::now() + timeout;
    let ended = tokio::select! {
        biased;
        _ = cancelled.cancelled() => End::Cancelled,
        _ = tokio::time::sleep_until(deadline) => End::TimedOut,
        result = work => End::Finished(result),
    };
    let reason = match ended {
        End::Cancelled => return Err("Environment build cancelled".into()),
        End::TimedOut => {
            cancelled.cancel();
            timeout_detail
        }
        End::Finished(Ok(status)) => {
            // Readiness is committed before optional garbage collection starts.
            let path = &status.path;
            let prune = async {
                for entry in std::fs::read_dir(&directory).map_err(|e| e.to_string())? {
                    let entry = entry.map_err(|e| e.to_string())?;
                    let other = entry.path();
                    let name = entry.file_name().to_string_lossy().into_owned();
                    if other != *path && name.starts_with(PREFIX) {
                        let kind = entry.file_type().map_err(|e| e.to_string())?;
                        if kind.is_dir() {
                            remove(other).await?;
                        } else if name.ends_with(FAILED_SUFFIX) {
                            std::fs::remove_file(other).map_err(|e| e.to_string())?;
                        }
                    }
                }

                Ok::<(), String>(())
            };
            tokio::select! {
                biased;
                _ = cancelled.cancelled() => tracing::warn!("Snapshot pruning cancelled after a successful build"),
                _ = tokio::time::sleep_until(deadline) => tracing::warn!("Snapshot pruning exceeded the build deadline"),
                result = prune => if let Err(error) = result {
                    tracing::warn!("Snapshot pruning failed after a successful build: {error}");
                },
            }
            return Ok(status);
        }
        End::Finished(Err(reason)) => reason,
    };
    gb_base::files::atomic_write(&marker, format!("{reason}\n").as_bytes())
        .map_err(|e| e.to_string())?;
    Err(reason)
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
    async fn hanging_builds_release_the_lock_on_timeout_or_caller_abort_and_allow_retry() {
        for abort in [false, true] {
            for cleanup in [false, true] {
                let root = tempfile::tempdir().unwrap();
                let name = "guildbotics-0123456789abcdef";
                let directory = snapshots_dir(root.path());
                let (started, running) = tokio::sync::oneshot::channel();
                let workspace = root.path().to_owned();
                let waiter = tokio::spawn(async move {
                    owned_build(
                        &workspace,
                        name,
                        "localized timeout".into(),
                        move |_, token| async move {
                            if cleanup {
                                crate::runtime::build_lifecycle(
                                    async move {
                                        Ok(((), move || async move {
                                            let _ = started.send(());
                                            std::future::pending().await
                                        }))
                                    },
                                    token,
                                    |_| async { Ok(PathBuf::from("unused")) },
                                )
                                .await
                            } else {
                                let _ = started.send(());
                                std::future::pending().await
                            }
                        },
                        |_| async { panic!("unfinished builds must not prune") },
                        if abort {
                            std::time::Duration::from_secs(60)
                        } else {
                            std::time::Duration::from_millis(20)
                        },
                    )
                    .await
                });
                running.await.unwrap();
                if abort {
                    waiter.abort();
                }
                let result = tokio::time::timeout(std::time::Duration::from_secs(1), waiter)
                    .await
                    .expect("build must finish within its deadline");
                if abort {
                    assert!(result.unwrap_err().is_cancelled());
                } else {
                    assert_eq!(result.unwrap(), Err("localized timeout".into()));
                }
                tokio::time::timeout(std::time::Duration::from_secs(1), async {
                    loop {
                        if FileLock::try_hold(&directory.join(LOCK_FILE))
                            .unwrap()
                            .is_some()
                        {
                            break;
                        }
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .unwrap();
                let state = status(root.path(), name).unwrap().state;
                if abort {
                    assert!(needs_build(&state));
                    assert!(!directory.join(format!("{name}{FAILED_SUFFIX}")).exists());
                } else {
                    assert_eq!(state, State::Failed("localized timeout".into()));
                }
                let retried = build(
                    root.path(),
                    name,
                    "timeout".into(),
                    move |directory, _| async move {
                        let path = directory.join(name);
                        std::fs::create_dir(&path).unwrap();
                        Ok(path)
                    },
                    |_| async { Ok(()) },
                )
                .await
                .unwrap();
                assert_eq!(retried.state, State::Ready);
            }
        }
    }
    #[tokio::test]
    async fn cancellation_after_save_preserves_the_valid_snapshot_without_a_failure_marker() {
        let root = tempfile::tempdir().unwrap();
        let name = "guildbotics-0123456789abcdef";
        let directory = snapshots_dir(root.path());
        std::fs::create_dir_all(directory.join("guildbotics-1111111111111111")).unwrap();
        let (pruning, started) = tokio::sync::oneshot::channel();
        let mut pruning = Some(pruning);
        let workspace = root.path().to_owned();
        let waiter = tokio::spawn(async move {
            owned_build(
                &workspace,
                name,
                "timeout".into(),
                move |directory, _| async move {
                    let path = directory.join(name);
                    std::fs::create_dir(&path).unwrap();
                    Ok(path)
                },
                move |_| {
                    let _ = pruning.take().unwrap().send(());
                    std::future::pending()
                },
                std::time::Duration::from_secs(60),
            )
            .await
        });
        started.await.unwrap();
        waiter.abort();
        let _ = waiter.await;
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            while status(root.path(), name).unwrap().state == State::Building {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(status(root.path(), name).unwrap().state, State::Ready);
        assert!(!directory.join(format!("{name}{FAILED_SUFFIX}")).exists());
    }
    #[tokio::test]
    async fn pruning_is_covered_by_the_build_deadline() {
        for failed in [false, true] {
            let root = tempfile::tempdir().unwrap();
            let name = "guildbotics-0123456789abcdef";
            let old = snapshots_dir(root.path()).join("guildbotics-1111111111111111");
            std::fs::create_dir_all(old).unwrap();
            let result = tokio::time::timeout(
                std::time::Duration::from_secs(1),
                owned_build(
                    root.path(),
                    name,
                    "localized timeout".into(),
                    move |directory, _| async move {
                        let path = directory.join(name);
                        std::fs::create_dir(&path).unwrap();
                        Ok(path)
                    },
                    move |_| async move {
                        if failed {
                            Err("cannot prune".into())
                        } else {
                            std::future::pending().await
                        }
                    },
                    std::time::Duration::from_millis(10),
                ),
            )
            .await
            .expect("pruning must finish within the build deadline");
            assert_eq!(result.unwrap().state, State::Ready);
            assert_eq!(status(root.path(), name).unwrap().state, State::Ready);
            assert!(
                !snapshots_dir(root.path())
                    .join(format!("{name}{FAILED_SUFFIX}"))
                    .exists()
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
            "Environment build exceeded 30 minutes".into(),
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
