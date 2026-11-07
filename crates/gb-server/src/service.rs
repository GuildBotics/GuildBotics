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
    fn listener_count(&self) -> usize {
        0
    }
    fn reception_failed(&self) -> bool {
        false
    }
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
    pub worker_count: Option<usize>,
    pub active_member_count: Option<usize>,
    pub cycle_count: Option<u64>,
    pub cycle_failure_count: Option<u64>,
    pub max_consecutive_errors: Option<u8>,
    pub routine_interval_minutes: Option<u16>,
    pub scheduled_source_enabled: Option<bool>,
    pub routine_source_enabled: Option<bool>,
    pub event_queue_source_enabled: Option<bool>,
    pub error: Option<String>,
}

impl Default for Status {
    fn default() -> Self {
        Self {
            state: "stopped",
            running: false,
            started_at: None,
            stopped_at: None,
            worker_count: None,
            active_member_count: None,
            cycle_count: None,
            cycle_failure_count: None,
            max_consecutive_errors: None,
            routine_interval_minutes: None,
            scheduled_source_enabled: None,
            routine_source_enabled: None,
            event_queue_source_enabled: None,
            error: None,
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
    startup_attempt: u64,
    service_run_id: Option<String>,
}

#[derive(Clone, Default)]
pub struct Service(
    Arc<Mutex<State>>,
    Arc<std::sync::OnceLock<Arc<crate::events::EventBus>>>,
);

impl Service {
    pub(crate) fn event_bus(&self) -> Option<Arc<crate::events::EventBus>> {
        self.1.get().cloned()
    }
    pub(crate) async fn events_starting(&self, enabled: bool) {
        let mut state = self.0.lock().await;
        state.status.event_queue_source_enabled = Some(enabled);
        if enabled && let Some(bus) = self.1.get() {
            bus.publish(
                "events.starting",
                serde_json::json!({"target":"events","state":"starting","running":true})
                    .as_object()
                    .unwrap()
                    .clone(),
                crate::events::Origin {
                    source: Some("events"),
                    ..Default::default()
                },
            );
        }
    }
    pub(crate) fn attach_events(&self, events: Arc<crate::events::EventBus>) {
        let _ = self.1.set(events);
    }
    fn announce(&self, state: &State) {
        let status = &state.status;
        let Some(bus) = self.1.get() else {
            return;
        };
        for target in ["scheduler", "events"] {
            if target == "events"
                && (status.event_queue_source_enabled != Some(true) || status.state == "starting")
            {
                continue;
            }
            let mut payload = serde_json::to_value(status).unwrap();
            payload["target"] = target.into();
            if target == "events" && status.state == "running" {
                if state
                    .running
                    .as_ref()
                    .is_some_and(|r| r.worker.reception_failed())
                {
                    payload["state"] = "failed".into();
                    payload["running"] = false.into();
                } else if state
                    .running
                    .as_ref()
                    .is_none_or(|r| r.worker.listener_count() == 0)
                {
                    payload["state"] = "stopped".into();
                    payload["running"] = false.into();
                }
            }
            bus.publish(
                &format!("{target}.{}", payload["state"].as_str().unwrap()),
                payload.as_object().unwrap().clone(),
                crate::events::Origin {
                    source: Some(target),
                    attributes: state
                        .service_run_id
                        .as_ref()
                        .map(|id| {
                            serde_json::json!({"service_run_id":id})
                                .as_object()
                                .unwrap()
                                .clone()
                        })
                        .unwrap_or_default(),
                    ..Default::default()
                },
            );
        }
    }
    pub async fn starting(&self) -> u64 {
        let mut state = self.0.lock().await;
        state.startup_attempt += 1;
        if state.running.is_none() {
            state.status.state = "starting";
            state.status.running = true;
            state.status.error = None;
            state.status.started_at = Some(chrono::Utc::now().to_rfc3339());
            state.status.stopped_at = None;
            self.announce(&state);
        }
        state.startup_attempt
    }

    pub async fn startup_failed(&self, attempt: u64) {
        let mut state = self.0.lock().await;
        if state.running.is_none() && state.startup_attempt == attempt {
            state.status.state = "failed";
            state.status.running = false;
            state.status.stopped_at = Some(chrono::Utc::now().to_rfc3339());
            state.status.error = Some(crate::i18n::t(
                "en",
                "app_api.errors.service_start_failed",
                &[],
            ));
            self.announce(&state);
        }
    }

    pub async fn startup_rejected(&self, attempt: u64, previous: Status) {
        let mut state = self.0.lock().await;
        if state.running.is_none() && state.startup_attempt == attempt {
            state.status = previous;
            self.announce(&state);
        }
    }

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
            started_at: state.status.started_at.clone().or(Some(now)),
            stopped_at: None,
            worker_count: Some(members.len()),
            active_member_count: Some(members.len()),
            cycle_count: Some(0),
            cycle_failure_count: Some(0),
            max_consecutive_errors: Some(settings.max_consecutive_errors),
            routine_interval_minutes: Some(settings.routine_interval_minutes),
            scheduled_source_enabled: Some(settings.scheduled),
            routine_source_enabled: Some(settings.routine),
            event_queue_source_enabled: Some(settings.event_queue),
            error: None,
        };
        state.service_run_id = worker.service_instance_id().map(String::from);
        state.running = Some(Running {
            worker: worker.clone(),
            stop: stop.clone(),
            cancel: cancel.clone(),
            done: done.clone(),
        });
        self.announce(&state);
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
                        {
                            let mut state = service.0.lock().await;
                            state.status.cycle_count =
                                state.status.cycle_count.map(|count| count + 1);
                            if outcome == Outcome::Failed {
                                state.status.cycle_failure_count =
                                    state.status.cycle_failure_count.map(|count| count + 1);
                            }
                        }
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
                    state.status.worker_count = state
                        .status
                        .worker_count
                        .map(|count| count.saturating_sub(1));
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
            let failed = panicked;
            state.status.error = None;
            if panicked {
                state.status.error = Some(crate::i18n::t(
                    "en",
                    "app_api.errors.service_worker_failed",
                    &[],
                ));
            }
            state.status.state = if failed { "failed" } else { "stopped" };
            state.status.running = false;
            state.status.worker_count = Some(0);
            state.status.stopped_at = Some(chrono::Utc::now().to_rfc3339());
            state.running = None;
            service.announce(&state);
            done.cancel();
        });
        Ok(true)
    }

    /// A force stop can escalate an ongoing graceful stop without waiting on
    /// its lock. The same completion token serves every concurrent stopper.
    pub async fn stop(&self, force: bool) -> Status {
        if let Some(done) = self.request_stop(force).await {
            self.wait_stop(done, force).await;
        }
        self.status().await
    }

    pub async fn wait_stop(&self, done: CancellationToken, force: bool) {
        if force {
            if tokio::time::timeout(Duration::from_secs(5), done.cancelled())
                .await
                .is_err()
            {
                let mut state = self.0.lock().await;
                if state.running.is_some() {
                    state.status.state = "failed";
                    state.status.error = Some(crate::i18n::t(
                        "en",
                        "app_api.errors.service_stop_timeout",
                        &[],
                    ));
                    self.announce(&state);
                }
            }
        } else {
            done.cancelled().await;
        }
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
            if state.status.state != "stopping" {
                state.status.state = "stopping";
                self.announce(&state);
            }
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
    use std::io::{Read, Seek, SeekFrom};
    let mut file = std::fs::File::open(path).ok()?;
    file.seek(SeekFrom::Start(1)).ok()?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).ok()?;
    let value: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
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

    struct Listening;
    impl Worker for Listening {
        fn listener_count(&self) -> usize {
            1
        }
        fn service_instance_id(&self) -> Option<&str> {
            Some("service-run")
        }
        fn cycle<'a>(&'a self, _: &'a str, _: &'a Settings) -> Pending<'a, Outcome> {
            Box::pin(std::future::pending())
        }
        fn worker_failed(&self, _: &str, _: u8) {
            panic!("no cycle failure")
        }
    }
    #[tokio::test]
    async fn scheduler_and_listener_transitions_are_ordered_once_with_run_identity() {
        let root = tempfile::tempdir().unwrap();
        let service = Service::default();
        let bus = Arc::new(crate::events::EventBus::default());
        service.attach_events(bus.clone());
        service.starting().await;
        service.events_starting(true).await;
        assert!(
            service
                .start(
                    root.path(),
                    root.path(),
                    vec!["alice".into()],
                    Settings::default(),
                    Arc::new(Listening)
                )
                .await
                .unwrap()
        );
        assert!(
            !service
                .start(
                    root.path(),
                    root.path(),
                    vec!["alice".into()],
                    Settings::default(),
                    Arc::new(Listening)
                )
                .await
                .unwrap()
        );
        service.stop(true).await;
        for target in ["scheduler", "events"] {
            let records: Vec<_> = bus
                .snapshot()
                .into_iter()
                .filter(|r| r["source"] == target)
                .collect();
            let types: Vec<_> = records
                .iter()
                .map(|r| r["type"].as_str().unwrap())
                .collect();
            assert_eq!(
                types,
                [
                    format!("{target}.starting"),
                    format!("{target}.running"),
                    format!("{target}.stopping"),
                    format!("{target}.stopped")
                ]
            );
            assert_eq!(records[1]["payload"]["running"], true);
            assert_eq!(records[1]["payload"]["target"], target);
            assert_eq!(records[1]["attributes"]["service_run_id"], "service-run");
            assert_eq!(records[3]["payload"]["running"], false);
        }
    }

    #[test]
    fn never_started_status_has_no_configured_metadata() {
        let status = serde_json::to_value(Status::default()).unwrap();
        for key in [
            "started_at",
            "stopped_at",
            "max_consecutive_errors",
            "routine_interval_minutes",
            "scheduled_source_enabled",
            "routine_source_enabled",
            "event_queue_source_enabled",
            "worker_count",
            "active_member_count",
            "cycle_count",
            "cycle_failure_count",
            "error",
        ] {
            assert!(status[key].is_null(), "{key}: {}", status[key]);
        }
    }

    struct CleanupWorker {
        entered: CancellationToken,
        release: CancellationToken,
        panic: bool,
    }
    impl Worker for CleanupWorker {
        fn cycle<'a>(&'a self, _: &'a str, _: &'a Settings) -> Pending<'a, Outcome> {
            Box::pin(async move {
                assert!(!self.panic, "worker panic");
                Outcome::Failed
            })
        }
        fn worker_failed(&self, _: &str, _: u8) {}
        fn shutdown(&self) -> Pending<'_, ()> {
            Box::pin(async move {
                self.entered.cancel();
                self.release.cancelled().await;
            })
        }
    }

    #[tokio::test(start_paused = true)]
    async fn force_timeout_retains_ownership_until_cleanup_finishes() {
        let root = tempfile::tempdir().unwrap();
        let service = Service::default();
        let worker = Arc::new(CleanupWorker {
            entered: CancellationToken::new(),
            release: CancellationToken::new(),
            panic: false,
        });
        service
            .start(
                root.path(),
                root.path(),
                vec!["alice".into()],
                Settings {
                    max_consecutive_errors: 1,
                    ..Settings::default()
                },
                worker.clone(),
            )
            .await
            .unwrap();
        worker.entered.cancelled().await;
        let status = service.stop(true).await;
        assert_eq!(status.state, "failed");
        assert!(status.running);
        assert!(status.error.is_some());
        assert!(
            gb_base::files::FileLock::try_hold(&root.path().join("run/service.lock"))
                .unwrap()
                .is_none()
        );
        worker.release.cancel();
        while service.status().await.running {
            tokio::task::yield_now().await;
        }
        assert!(service.status().await.error.is_none());
        assert!(
            gb_base::files::FileLock::try_hold(&root.path().join("run/service.lock"))
                .unwrap()
                .is_some()
        );
    }

    #[tokio::test]
    async fn worker_panic_records_failed_status_after_cleanup() {
        let root = tempfile::tempdir().unwrap();
        let service = Service::default();
        let worker = Arc::new(CleanupWorker {
            entered: CancellationToken::new(),
            release: CancellationToken::new(),
            panic: true,
        });
        worker.release.cancel();
        service
            .start(
                root.path(),
                root.path(),
                vec!["alice".into()],
                Settings::default(),
                worker,
            )
            .await
            .unwrap();
        while service.status().await.running {
            tokio::task::yield_now().await;
        }
        let status = service.status().await;
        assert_eq!(status.state, "failed");
        assert!(status.error.is_some());
        assert_eq!(status.worker_count, Some(0));
    }

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
        let status = service.status().await;
        assert_eq!(status.state, "stopped");
        assert_eq!(status.worker_count, Some(0));
        assert_eq!(status.active_member_count, Some(1));
        assert_eq!(status.cycle_count, Some(6));
        assert_eq!(status.cycle_failure_count, Some(3));
        assert_eq!(status.max_consecutive_errors, Some(2));
        assert_eq!(status.routine_interval_minutes, Some(10));
        assert_eq!(status.scheduled_source_enabled, Some(true));
        assert_eq!(status.routine_source_enabled, Some(true));
        assert_eq!(status.event_queue_source_enabled, Some(true));
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
            assert_eq!(stopped.worker_count, Some(0));
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
