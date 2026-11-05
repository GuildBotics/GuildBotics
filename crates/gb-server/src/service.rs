//! Service ownership and worker lifetime. Each member has one serial worker.
use std::{future::Future, path::Path, pin::Pin, sync::Arc, time::Duration};

use serde::Serialize;
use tokio::sync::Mutex;
use tokio_util::sync::CancellationToken;

use crate::service_settings::Settings;

pub type Pending<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

#[derive(Debug)]
pub struct AlreadyRunning {
    pub owner: Option<serde_json::Value>,
}
impl std::fmt::Display for AlreadyRunning {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("service_already_running")
    }
}
impl std::error::Error for AlreadyRunning {}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Outcome {
    Succeeded,
    Failed,
    Deferred,
}

/// The dispatcher owns command traces and run settlement; the service owns
/// serialization, error limits and draining. Temporary deferral is not failure.
pub trait Worker: Send + Sync {
    fn service_instance_id(&self) -> Option<&str> {
        None
    }
    fn cycle<'a>(&'a self, person: &'a str, settings: &'a Settings) -> Pending<'a, Outcome>;
    fn worker_failed(&self, person: &str, errors: u8);
    fn request_stop(&self, _force: bool) {}
    fn shutdown(&self) -> Pending<'_, ()> {
        Box::pin(async {})
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Status {
    pub state: &'static str,
    pub running: bool,
    pub started_at: Option<String>,
    pub stopped_at: Option<String>,
    pub worker_count: usize,
    pub active_member_count: usize,
    pub max_consecutive_errors: u8,
    pub routine_interval_minutes: u16,
    pub scheduled_source_enabled: bool,
    pub routine_source_enabled: bool,
    pub event_queue_source_enabled: bool,
}

impl Default for Status {
    fn default() -> Self {
        let settings = Settings::default();
        Self {
            state: "stopped",
            running: false,
            started_at: None,
            stopped_at: None,
            worker_count: 0,
            active_member_count: 0,
            max_consecutive_errors: settings.max_consecutive_errors,
            routine_interval_minutes: settings.routine_interval_minutes,
            scheduled_source_enabled: settings.scheduled,
            routine_source_enabled: settings.routine,
            event_queue_source_enabled: settings.event_queue,
        }
    }
}

struct Running {
    worker: Arc<dyn Worker>,
    stop: CancellationToken,
    cancel: CancellationToken,
    done: CancellationToken,
}

#[derive(Default)]
struct State {
    status: Status,
    running: Option<Running>,
}

#[derive(Clone, Default)]
pub struct Service(Arc<Mutex<State>>);

impl Service {
    pub async fn status(&self) -> Status {
        self.0.lock().await.status.clone()
    }

    /// Returns false for an already running instance. The machine lock is held
    /// until every worker has finished, including a graceful drain.
    pub async fn start(
        &self,
        data: &Path,
        workspace: &Path,
        members: Vec<String>,
        settings: Settings,
        worker: Arc<dyn Worker>,
    ) -> anyhow::Result<bool> {
        settings.validate()?;
        let mut state = self.0.lock().await;
        if state.running.is_some() {
            return Ok(false);
        }
        let path = data.join("run/service.lock");
        let owner_path = path.clone();
        let lock = tokio::task::spawn_blocking(move || {
            retry_lock(|| gb_base::files::FileLock::try_hold(&path))
        })
        .await??;
        let lock = lock.ok_or_else(|| AlreadyRunning {
            owner: read_owner(&owner_path),
        })?;
        let keep_awake = {
            let workspace = workspace.to_owned();
            let enabled = settings.keep_awake;
            tokio::task::spawn_blocking(move || {
                crate::keep_awake::Follow::start(workspace, enabled)
            })
            .await??
        };
        let now = chrono::Utc::now().to_rfc3339();
        let record = serde_json::json!({"pid":std::process::id(),
            "service_instance_id":worker.service_instance_id().map(str::to_owned).unwrap_or_else(||uuid::Uuid::new_v4().simple().to_string()),
            "workspace":std::fs::canonicalize(workspace)?,"started_at":now});
        {
            use std::io::{Seek, SeekFrom, Write};
            let mut file = lock.file();
            file.set_len(0)?;
            file.seek(SeekFrom::Start(0))?;
            file.write_all(format!(" {record}\n").as_bytes())?;
            file.sync_all()?;
        }
        let (stop, cancel, done) = (
            CancellationToken::new(),
            CancellationToken::new(),
            CancellationToken::new(),
        );
        state.status = Status {
            state: "running",
            running: true,
            started_at: Some(now),
            stopped_at: None,
            worker_count: members.len(),
            active_member_count: members.len(),
            max_consecutive_errors: settings.max_consecutive_errors,
            routine_interval_minutes: settings.routine_interval_minutes,
            scheduled_source_enabled: settings.scheduled,
            routine_source_enabled: settings.routine,
            event_queue_source_enabled: settings.event_queue,
        };
        state.running = Some(Running {
            worker: worker.clone(),
            stop: stop.clone(),
            cancel: cancel.clone(),
            done: done.clone(),
        });
        let service = self.clone();
        tokio::spawn(async move {
            let mut tasks = tokio::task::JoinSet::new();
            for person in members {
                let (worker, settings, stop, cancel, service) = (
                    worker.clone(),
                    settings.clone(),
                    stop.clone(),
                    cancel.clone(),
                    service.clone(),
                );
                tasks.spawn(async move {
                    let mut errors = 0;
                    while !stop.is_cancelled() {
                        let outcome = tokio::select! {
                            biased;
                            () = cancel.cancelled() => break,
                            outcome = worker.cycle(&person, &settings) => outcome,
                        };
                        if !stop.is_cancelled() {
                            match outcome {
                                Outcome::Succeeded => errors = 0,
                                Outcome::Failed => errors += 1,
                                Outcome::Deferred => (),
                            }
                            if errors >= settings.max_consecutive_errors {
                                worker.worker_failed(&person, errors);
                                break;
                            }
                        }
                        tokio::select! {
                            () = stop.cancelled() => break,
                            () = tokio::time::sleep(Duration::from_secs(5)) => (),
                        }
                    }
                    let mut state = service.0.lock().await;
                    state.status.worker_count = state.status.worker_count.saturating_sub(1);
                });
            }
            let mut panicked = false;
            while let Some(result) = tasks.join_next().await {
                panicked |= result.is_err();
            }
            worker.request_stop(false);
            worker.shutdown().await;
            let _ = tokio::task::spawn_blocking(move || drop(keep_awake)).await;
            drop(lock);
            let mut state = service.0.lock().await;
            let failed = panicked || (!stop.is_cancelled() && state.status.active_member_count > 0);
            state.status.state = if failed { "failed" } else { "stopped" };
            state.status.running = false;
            state.status.worker_count = 0;
            state.status.stopped_at = Some(chrono::Utc::now().to_rfc3339());
            state.running = None;
            done.cancel();
        });
        Ok(true)
    }

    /// A force stop can escalate an ongoing graceful stop without waiting on
    /// its lock. The same completion token serves every concurrent stopper.
    pub async fn stop(&self, force: bool) -> Status {
        if let Some(done) = self.request_stop(force).await {
            done.cancelled().await;
        }
        self.status().await
    }

    pub async fn request_stop(&self, force: bool) -> Option<CancellationToken> {
        let mut state = self.0.lock().await;
        if let Some(running) = &state.running {
            running.worker.request_stop(force);
            running.stop.cancel();
            if force {
                running.cancel.cancel();
            }
            let done = running.done.clone();
            state.status.state = "stopping";
            Some(done)
        } else {
            None
        }
    }
}

fn retry_lock(
    mut acquire: impl FnMut() -> std::io::Result<Option<gb_base::files::FileLock>>,
) -> std::io::Result<Option<gb_base::files::FileLock>> {
    let first = acquire()?;
    if first.is_some() {
        return Ok(first);
    }
    std::thread::sleep(Duration::from_millis(10));
    acquire()
}

fn read_owner(path: &Path) -> Option<serde_json::Value> {
    let value: serde_json::Value = serde_json::from_slice(&std::fs::read(path).ok()?).ok()?;
    if value["pid"].as_u64()? == 0 || value["service_instance_id"].as_str()?.is_empty() {
        return None;
    }
    Some(
        serde_json::json!({"pid":value["pid"],"service_instance_id":value["service_instance_id"],
        "workspace":value["workspace"].as_str()?,"started_at":value["started_at"].as_str()?}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn one_transient_lock_conflict_retries_but_io_failures_do_not() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("service.lock");
        let mut held = gb_base::files::FileLock::try_hold(&path).unwrap();
        let mut attempts = 0;
        let acquired = retry_lock(|| {
            attempts += 1;
            let lock = gb_base::files::FileLock::try_hold(&path)?;
            if attempts == 1 {
                assert!(lock.is_none());
                drop(held.take());
            }
            Ok(lock)
        })
        .unwrap()
        .unwrap();
        assert_eq!(attempts, 2);
        assert!(gb_base::files::FileLock::try_hold(&path).unwrap().is_none());
        drop(acquired);
        let mut attempts = 0;
        let error = retry_lock(|| {
            attempts += 1;
            Err(std::io::ErrorKind::PermissionDenied.into())
        })
        .err()
        .unwrap();
        assert_eq!(attempts, 1);
        assert_eq!(error.kind(), std::io::ErrorKind::PermissionDenied);
    }

    struct HeldWorker {
        entered: CancellationToken,
        release: CancellationToken,
        calls: AtomicUsize,
        fail: std::sync::atomic::AtomicBool,
    }
    impl HeldWorker {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                entered: CancellationToken::new(),
                release: CancellationToken::new(),
                calls: AtomicUsize::new(0),
                fail: std::sync::atomic::AtomicBool::new(false),
            })
        }
    }
    impl Worker for HeldWorker {
        fn cycle<'a>(&'a self, _: &'a str, _: &'a Settings) -> Pending<'a, Outcome> {
            Box::pin(async move {
                self.calls.fetch_add(1, Ordering::SeqCst);
                self.entered.cancel();
                self.release.cancelled().await;
                if self.fail.load(Ordering::SeqCst) {
                    Outcome::Failed
                } else {
                    Outcome::Succeeded
                }
            })
        }
        fn worker_failed(&self, _: &str, _: u8) {
            panic!("unexpected failure");
        }
    }

    struct Outcomes {
        outcomes: std::sync::Mutex<std::collections::VecDeque<Outcome>>,
        stopped: CancellationToken,
        failures: AtomicUsize,
    }
    impl Worker for Outcomes {
        fn cycle<'a>(&'a self, _: &'a str, _: &'a Settings) -> Pending<'a, Outcome> {
            Box::pin(async move { self.outcomes.lock().unwrap().pop_front().unwrap() })
        }
        fn worker_failed(&self, _: &str, errors: u8) {
            self.failures.store(usize::from(errors), Ordering::SeqCst);
            self.stopped.cancel();
        }
    }

    #[tokio::test(start_paused = true)]
    async fn deferral_preserves_errors_success_resets_them_and_limit_stops_the_worker() {
        let dir = tempfile::tempdir().unwrap();
        let service = Service::default();
        let worker = Arc::new(Outcomes {
            outcomes: std::sync::Mutex::new(
                [
                    Outcome::Failed,
                    Outcome::Deferred,
                    Outcome::Succeeded,
                    Outcome::Failed,
                    Outcome::Deferred,
                    Outcome::Failed,
                ]
                .into(),
            ),
            stopped: CancellationToken::new(),
            failures: AtomicUsize::new(0),
        });
        service
            .start(
                dir.path(),
                dir.path(),
                vec!["alice".into()],
                Settings {
                    max_consecutive_errors: 2,
                    ..Settings::default()
                },
                worker.clone(),
            )
            .await
            .unwrap();
        worker.stopped.cancelled().await;
        while service.status().await.running {
            tokio::task::yield_now().await;
        }
        assert_eq!(worker.failures.load(Ordering::SeqCst), 2);
        assert!(worker.outcomes.lock().unwrap().is_empty());
        assert_eq!(service.status().await.state, "failed");
        assert_eq!(service.status().await.worker_count, 0);
    }

    #[tokio::test]
    async fn graceful_stop_finishes_current_work_and_prevents_the_next_cycle() {
        for failed in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let service = Service::default();
            let worker = HeldWorker::new();
            worker.fail.store(failed, Ordering::SeqCst);
            assert!(
                service
                    .start(
                        dir.path(),
                        dir.path(),
                        vec!["alice".into()],
                        Settings {
                            max_consecutive_errors: 1,
                            ..Default::default()
                        },
                        worker.clone()
                    )
                    .await
                    .unwrap()
            );
            worker.entered.cancelled().await;
            assert!(
                !service
                    .start(
                        dir.path(),
                        dir.path(),
                        vec!["alice".into()],
                        Settings {
                            max_consecutive_errors: 1,
                            ..Default::default()
                        },
                        worker.clone()
                    )
                    .await
                    .unwrap()
            );
            let stopper = tokio::spawn({
                let service = service.clone();
                async move { service.stop(false).await }
            });
            loop {
                if service.status().await.state == "stopping" {
                    break;
                }
                tokio::task::yield_now().await;
            }
            assert!(!stopper.is_finished());
            worker.release.cancel();
            let stopped = stopper.await.unwrap();
            assert_eq!(stopped.state, "stopped");
            assert_eq!(stopped.worker_count, 0);
            assert!(!stopped.running);
            assert_eq!(worker.calls.load(Ordering::SeqCst), 1);
        }
    }

    #[tokio::test]
    async fn force_escalates_a_graceful_stop_and_releases_machine_ownership() {
        let dir = tempfile::tempdir().unwrap();
        let service = Service::default();
        let worker = HeldWorker::new();
        service
            .start(
                dir.path(),
                dir.path(),
                vec!["alice".into()],
                Settings::default(),
                worker.clone(),
            )
            .await
            .unwrap();
        worker.entered.cancelled().await;
        let competing = Service::default();
        let owner = read_owner(&dir.path().join("run/service.lock")).unwrap();
        assert_eq!(owner["pid"], std::process::id());
        assert_eq!(
            owner["workspace"],
            std::fs::canonicalize(dir.path())
                .unwrap()
                .to_string_lossy()
                .as_ref()
        );
        assert_eq!(owner["service_instance_id"].as_str().unwrap().len(), 32);
        assert!(
            chrono::DateTime::parse_from_rfc3339(owner["started_at"].as_str().unwrap()).is_ok()
        );
        let error = competing
            .start(
                dir.path(),
                dir.path(),
                vec!["alice".into()],
                Settings::default(),
                worker.clone(),
            )
            .await
            .unwrap_err();
        assert_eq!(
            error
                .downcast_ref::<AlreadyRunning>()
                .unwrap()
                .owner
                .as_ref(),
            Some(&owner)
        );
        let stopper = tokio::spawn({
            let service = service.clone();
            async move { service.stop(false).await }
        });
        while service.status().await.state != "stopping" {
            tokio::task::yield_now().await;
        }
        assert_eq!(service.stop(true).await.state, "stopped");
        assert_eq!(stopper.await.unwrap().state, "stopped");
        assert!(!worker.release.is_cancelled());
        let other_workspace = dir.path().join("second");
        std::fs::create_dir(&other_workspace).unwrap();
        assert!(
            competing
                .start(
                    dir.path(),
                    &other_workspace,
                    vec!["alice".into()],
                    Settings::default(),
                    worker
                )
                .await
                .unwrap()
        );
        competing.stop(true).await;
        assert!(dir.path().join("run/service.lock").exists());
        let next = read_owner(&dir.path().join("run/service.lock")).unwrap();
        assert_eq!(
            next["workspace"],
            std::fs::canonicalize(other_workspace)
                .unwrap()
                .to_string_lossy()
                .as_ref()
        );
        assert_ne!(next["service_instance_id"], owner["service_instance_id"]);
    }
}
