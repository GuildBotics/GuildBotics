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
    fn event_status(&self, status: &Status) -> serde_json::Value {
        let mut value = empty_events_status();
        value["listener_count"] = if status.running {
            self.listener_count()
        } else {
            0
        }
        .into();
        value["cycle_count"] = serde_json::json!(status.cycle_count);
        value
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
    events_state: Option<&'static str>,
    events_started_at: Option<String>,
    events_stopped_at: Option<String>,
    events_snapshot: Option<serde_json::Value>,
}

#[derive(Clone, Default)]
pub struct Service(
    Arc<Mutex<State>>,
    Arc<std::sync::OnceLock<Arc<crate::events::EventBus>>>,
    Arc<Mutex<()>>,
);

/// Settles every admitted start, including cancellation while preparing workers.
pub(crate) struct Starting {
    service: Service,
    attempt: u64,
    settled: bool,
}
impl Starting {
    pub(crate) async fn finish<T>(mut self, result: anyhow::Result<T>) -> anyhow::Result<T> {
        if result.is_err() {
            self.service.startup_failed(self.attempt).await;
        }
        self.settled = true;
        result
    }
}
impl Drop for Starting {
    fn drop(&mut self) {
        if !self.settled {
            let service = self.service.clone();
            let attempt = self.attempt;
            tokio::spawn(async move {
                service.startup_failed(attempt).await;
            });
        }
    }
}

impl Service {
    pub(crate) fn attach_events(&self, events: Arc<crate::events::EventBus>) {
        let _ = self.1.set(events);
    }
    fn publish(&self, state: &State, target: &'static str, payload: serde_json::Value) {
        if let Some(bus) = self.1.get() {
            bus.publish(
                &format!("{target}.{}", payload["state"].as_str().unwrap()),
                payload.as_object().unwrap().clone(),
                crate::events::Origin {
                    source: Some(target),
                    attributes: serde_json::json!({"service_run_id":state.service_run_id})
                        .as_object()
                        .unwrap()
                        .clone(),
                    ..Default::default()
                },
            );
        }
    }
    fn events_view(state: &State) -> serde_json::Value {
        if state.events_state.is_none() {
            return empty_events_status();
        }
        let mut payload = state
            .running
            .as_ref()
            .map(|r| r.worker.event_status(&state.status))
            .or_else(|| state.events_snapshot.clone())
            .unwrap_or_else(empty_events_status);
        let phase = state.events_state.unwrap_or("stopped");
        payload["target"] = "events".into();
        payload["state"] = phase.into();
        payload["running"] = matches!(phase, "starting" | "running" | "stopping").into();
        payload["started_at"] = serde_json::json!(state.events_started_at);
        payload["stopped_at"] = serde_json::json!(state.events_stopped_at);
        payload["error"] = if phase == "failed" {
            crate::i18n::t("en", "app_api.errors.service_events_failed", &[]).into()
        } else {
            serde_json::Value::Null
        };
        payload
    }
    pub(crate) async fn events_status(&self) -> serde_json::Value {
        let state = self.0.lock().await;
        Self::events_view(&state)
    }
    fn transition_events(&self, state: &mut State, phase: &'static str) {
        if state.events_state == Some(phase) {
            return;
        }
        state.events_state = Some(phase);
        if phase == "starting" {
            state.events_started_at = Some(chrono::Utc::now().to_rfc3339());
            state.events_stopped_at = None;
        } else if matches!(phase, "stopped" | "failed") {
            state.events_stopped_at = Some(chrono::Utc::now().to_rfc3339());
        }
        let payload = Self::events_view(state);
        state.events_snapshot = Some(payload.clone());
        self.publish(state, "events", payload);
    }
    pub(crate) async fn reception_failed(&self, run_id: &str) {
        let mut state = self.0.lock().await;
        if state.service_run_id.as_deref() != Some(run_id) {
            return;
        }
        if matches!(state.events_state, Some("running" | "starting")) {
            self.transition_events(&mut state, "failed");
        }
    }
    fn announce(&self, state: &mut State) {
        let mut payload = serde_json::to_value(&state.status).unwrap();
        payload["target"] = "scheduler".into();
        self.publish(state, "scheduler", payload);
        match state.status.state {
            "running"
                if state.events_state.is_none()
                    && state.status.event_queue_source_enabled == Some(true) =>
            {
                self.transition_events(state, "starting");
                let worker = &state.running.as_ref().unwrap().worker;
                let phase = if worker.reception_failed() {
                    "failed"
                } else if worker.listener_count() > 0 {
                    "running"
                } else {
                    "stopped"
                };
                self.transition_events(state, phase);
            }
            "stopping" if matches!(state.events_state, Some("running" | "starting")) => {
                self.transition_events(state, "stopping");
            }
            "stopped" | "failed"
                if !state.status.running
                    && matches!(
                        state.events_state,
                        Some("running" | "starting" | "stopping")
                    ) =>
            {
                self.transition_events(state, "stopped");
            }
            _ => (),
        }
    }
    pub(crate) async fn begin_start(&self, run_id: String) -> Starting {
        let attempt = self.starting_run(run_id).await;
        Starting {
            service: self.clone(),
            attempt,
            settled: false,
        }
    }
    pub async fn starting(&self) -> u64 {
        self.starting_run(uuid::Uuid::new_v4().simple().to_string())
            .await
    }
    pub(crate) async fn starting_run(&self, run_id: String) -> u64 {
        let mut state = self.0.lock().await;
        state.startup_attempt += 1;
        if state.running.is_none() {
            state.status = Status::default();
            state.service_run_id = Some(run_id);
            state.events_state = None;
            state.events_started_at = None;
            state.events_stopped_at = None;
            state.events_snapshot = None;
            state.status.state = "starting";
            state.status.running = true;
            state.status.error = None;
            state.status.started_at = Some(chrono::Utc::now().to_rfc3339());
            state.status.stopped_at = None;
            self.announce(&mut state);
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
            self.announce(&mut state);
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
        let _gate = self.2.lock().await;
        if self.status().await.running {
            return Ok(false);
        }
        let admitted = Self::admit(data, workspace, &settings).await?;
        let starting = self.begin_start(admitted.run_id.clone()).await;
        let result = self
            .start_admitted(admitted, workspace, members, settings, worker)
            .await;
        starting.finish(result).await
    }

    pub(crate) async fn admit(
        data: &Path,
        workspace: &Path,
        settings: &Settings,
    ) -> anyhow::Result<Admission> {
        settings.validate()?;
        let path = data.join("run/service.lock");
        let owner_path = path.clone();
        let lock = tokio::task::spawn_blocking(move || {
            retry_lock(|| gb_base::files::FileLock::try_hold(&path))
        })
        .await??
        .ok_or_else(|| AlreadyRunning {
            owner: read_owner(&owner_path),
        })?;
        let run_id = uuid::Uuid::new_v4().simple().to_string();
        let record = serde_json::json!({"pid":std::process::id(),
            "service_instance_id":run_id,"workspace":std::fs::canonicalize(workspace)?,
            "started_at":chrono::Utc::now().to_rfc3339()});
        {
            use std::io::{Seek, SeekFrom, Write};
            let mut file = lock.file();
            file.set_len(0)?;
            file.seek(SeekFrom::Start(0))?;
            file.write_all(format!(" {record}\n").as_bytes())?;
            file.sync_all()?;
        }
        Ok(Admission { lock, run_id })
    }
    pub(crate) async fn start_admitted(
        &self,
        admitted: Admission,
        workspace: &Path,
        members: Vec<String>,
        settings: Settings,
        worker: Arc<dyn Worker>,
    ) -> anyhow::Result<bool> {
        let mut state = self.0.lock().await;
        if state.running.is_some() {
            return Ok(false);
        }
        let Admission { lock, run_id } = admitted;
        let keep_awake = {
            let workspace = workspace.to_owned();
            let enabled = settings.keep_awake;
            tokio::task::spawn_blocking(move || {
                crate::keep_awake::Follow::start(workspace, enabled)
            })
            .await??
        };
        let now = chrono::Utc::now().to_rfc3339();
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
        state.service_run_id = Some(run_id);
        state.running = Some(Running {
            worker: worker.clone(),
            stop: stop.clone(),
            cancel: cancel.clone(),
            done: done.clone(),
        });
        self.announce(&mut state);
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
            service.announce(&mut state);
            state.events_snapshot = Some(Self::events_view(&state));
            state.running = None;
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
                    self.announce(&mut state);
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
                self.announce(&mut state);
            }
            Some(done)
        } else {
            None
        }
    }
}

pub(crate) struct Admission {
    lock: gb_base::files::FileLock,
    pub(crate) run_id: String,
}

fn empty_events_status() -> serde_json::Value {
    let mut status = serde_json::to_value(Status::default()).unwrap();
    status.as_object_mut().unwrap().extend(
        serde_json::json!({
            "target":"events", "subscription_count":null,"listener_count":null,
            "events_drained_count":null,"events_auth_failed_count":null,
            "events_auth_failed_persons":[],"events_chat_provider":"",
            "events_missing_channel_count":0,"events_missing_channel_persons":[],
            "events_error_count":0
        })
        .as_object()
        .unwrap()
        .clone(),
    );
    status
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
            let run_id = records[0]["attributes"]["service_run_id"].as_str().unwrap();
            assert_eq!(run_id.len(), 32);
            for record in &records {
                assert_eq!(record["attributes"]["service_run_id"], run_id);
            }
            assert_eq!(records[3]["payload"]["running"], false);
        }
    }

    fn event_types(bus: &crate::events::EventBus, target: &str) -> Vec<String> {
        bus.snapshot()
            .iter()
            .filter(|r| r["source"] == target)
            .map(|r| r["type"].as_str().unwrap().to_owned())
            .collect()
    }

    #[tokio::test]
    async fn no_listeners_stop_once_and_payloads_match_the_events_status() {
        let root = tempfile::tempdir().unwrap();
        let service = Service::default();
        let bus = Arc::new(crate::events::EventBus::default());
        service.attach_events(bus.clone());
        service
            .start(
                root.path(),
                root.path(),
                vec!["alice".into()],
                Settings::default(),
                HeldWorker::new(),
            )
            .await
            .unwrap();
        assert_eq!(
            event_types(&bus, "events"),
            ["events.starting", "events.stopped"]
        );
        let records = bus.snapshot();
        let payload = &records.last().unwrap()["payload"];
        assert_eq!(payload, &service.events_status().await);
        assert_eq!(payload["listener_count"], 0);
        assert!(payload["worker_count"].is_null());
        service.stop(true).await;
        assert_eq!(
            event_types(&bus, "events"),
            ["events.starting", "events.stopped"]
        );
        assert_eq!(payload, &service.events_status().await);
    }

    #[tokio::test]
    async fn receiver_failures_transition_once_and_ignore_other_service_runs() {
        let root = tempfile::tempdir().unwrap();
        let service = Service::default();
        let bus = Arc::new(crate::events::EventBus::default());
        service.attach_events(bus.clone());
        service
            .start(
                root.path(),
                root.path(),
                vec!["alice".into()],
                Settings::default(),
                Arc::new(Listening),
            )
            .await
            .unwrap();
        assert_eq!(
            bus.snapshot().last().unwrap()["payload"],
            service.events_status().await
        );
        let records = bus.snapshot();
        let id = records[0]["attributes"]["service_run_id"].as_str().unwrap();
        service.reception_failed("previous-service-run").await;
        assert_eq!(
            event_types(&bus, "events"),
            ["events.starting", "events.running"]
        );
        tokio::join!(service.reception_failed(id), service.reception_failed(id));
        assert_eq!(
            event_types(&bus, "events"),
            ["events.starting", "events.running", "events.failed"]
        );
        assert_eq!(
            bus.snapshot().last().unwrap()["payload"],
            service.events_status().await
        );
        service.stop(true).await;
        assert_eq!(
            event_types(&bus, "events"),
            ["events.starting", "events.running", "events.failed"]
        );
    }

    #[tokio::test]
    async fn restarts_allocate_one_fresh_identity_before_every_first_transition() {
        let root = tempfile::tempdir().unwrap();
        let service = Service::default();
        let bus = Arc::new(crate::events::EventBus::default());
        service.attach_events(bus.clone());
        let mut ids = vec![];
        for _ in 0..2 {
            let offset = bus.snapshot().len();
            service
                .start(
                    root.path(),
                    root.path(),
                    vec!["alice".into()],
                    Settings::default(),
                    Arc::new(Listening),
                )
                .await
                .unwrap();
            let owner = read_owner(&root.path().join("run/service.lock")).unwrap();
            service.stop(true).await;
            let records = bus.snapshot();
            let records = &records[offset..];
            assert_eq!(records[0]["type"], "scheduler.starting");
            let id = records[0]["attributes"]["service_run_id"].as_str().unwrap();
            assert_eq!(id.len(), 32);
            assert_eq!(owner["service_instance_id"], id);
            assert!(
                records
                    .iter()
                    .all(|r| r["attributes"]["service_run_id"] == id)
            );
            ids.push(id.to_owned());
        }
        assert_ne!(ids[0], ids[1]);
    }

    fn blocking_barrier() -> (
        CancellationToken,
        std::sync::mpsc::Sender<()>,
        tokio::task::JoinHandle<()>,
    ) {
        let entered = CancellationToken::new();
        let (release, wait) = std::sync::mpsc::channel();
        let task = tokio::task::spawn_blocking({
            let entered = entered.clone();
            move || {
                entered.cancel();
                wait.recv_timeout(Duration::from_secs(30)).unwrap();
            }
        });
        (entered, release, task)
    }

    #[test]
    fn cancelling_direct_start_during_keep_awake_preparation_settles_and_allows_restart() {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .max_blocking_threads(1)
            .build()
            .unwrap()
            .block_on(async {
                let root = tempfile::tempdir().unwrap();
                let service = Service::default();
                let bus = Arc::new(crate::events::EventBus::default());
                service.attach_events(bus.clone());
                let (entered, release, first) = blocking_barrier();
                entered.cancelled().await;
                let mut start = Box::pin(service.start(
                    root.path(),
                    root.path(),
                    vec!["alice".into()],
                    Settings::default(),
                    Arc::new(Listening),
                ));
                // Queue admission behind the first barrier, then a second barrier
                // behind admission. Keep-awake preparation cannot pass the second.
                std::future::poll_fn(|cx| {
                    assert!(start.as_mut().poll(cx).is_pending());
                    std::task::Poll::Ready(())
                })
                .await;
                let (entered, release_second, second) = blocking_barrier();
                release.send(()).unwrap();
                tokio::time::timeout(Duration::from_secs(5), entered.cancelled())
                    .await
                    .unwrap();
                std::future::poll_fn(|cx| {
                    assert!(start.as_mut().poll(cx).is_pending());
                    std::task::Poll::Ready(())
                })
                .await;
                assert_eq!(event_types(&bus, "scheduler"), ["scheduler.starting"]);
                drop(start);
                release_second.send(()).unwrap();
                first.await.unwrap();
                second.await.unwrap();
                tokio::time::timeout(Duration::from_secs(5), async {
                    while service.status().await.state != "failed" {
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .unwrap();
                assert!(!service.status().await.running);
                assert_eq!(
                    event_types(&bus, "scheduler"),
                    ["scheduler.starting", "scheduler.failed"]
                );
                assert!(event_types(&bus, "events").is_empty());
                assert!(
                    gb_base::files::FileLock::try_hold(&root.path().join("run/service.lock"))
                        .unwrap()
                        .is_some()
                );
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
                assert_eq!(service.stop(true).await.state, "stopped");
            });
    }

    #[tokio::test]
    async fn direct_start_admission_io_error_publishes_nothing_and_releases_ownership() {
        let root = tempfile::tempdir().unwrap();
        let service = Service::default();
        let bus = Arc::new(crate::events::EventBus::default());
        service.attach_events(bus.clone());
        assert!(
            service
                .start(
                    root.path(),
                    &root.path().join("missing-workspace"),
                    vec![],
                    Settings::default(),
                    Arc::new(Listening)
                )
                .await
                .is_err()
        );
        assert!(bus.snapshot().is_empty());
        assert_eq!(service.status().await.state, "stopped");
        assert!(!service.status().await.running);
        assert!(
            gb_base::files::FileLock::try_hold(&root.path().join("run/service.lock"))
                .unwrap()
                .is_some()
        );
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
