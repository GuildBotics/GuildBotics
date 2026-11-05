//! Assemble native member workers and shared chat reception for a service run.
use crate::{
    service::{Outcome, Pending, Worker},
    service_settings::Settings,
};
use anyhow::Context;
use std::{collections::BTreeMap, sync::Arc};
use tokio::sync::OnceCell;

#[derive(Debug)]
pub(crate) struct StartupConfig;
impl std::fmt::Display for StartupConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("service_config_invalid")
    }
}
impl std::error::Error for StartupConfig {}

#[derive(Debug)]
pub(crate) struct StartupSecrets;
impl std::fmt::Display for StartupSecrets {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("secret_store_unavailable")
    }
}
impl std::error::Error for StartupSecrets {}
use tokio_util::{sync::CancellationToken, task::TaskTracker};

struct Starting {
    service: crate::service::Service,
    attempt: u64,
    settled: bool,
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

#[derive(Default)]
pub struct Controller {
    pub execution: crate::execution::Coordinator,
    pub service: crate::service::Service,
    gate: tokio::sync::Mutex<()>,
    native: Option<crate::member::native::Native>,
    host: Option<Arc<gb_environment::Host>>,
    worker: tokio::sync::RwLock<Option<Arc<NativeWorker>>>,
}
impl Controller {
    #[cfg(test)]
    pub(crate) async fn hold_start_for_test(&self) -> tokio::sync::MutexGuard<'_, ()> {
        self.gate.lock().await
    }

    pub(crate) fn host(&self) -> Option<Arc<gb_environment::Host>> {
        self.host.clone()
    }
    pub(crate) fn workflow(
        &self,
        person: &str,
        cleanup: TaskTracker,
    ) -> anyhow::Result<crate::workflow_runtime::Runtime> {
        let native = self
            .native
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("config_not_found"))?;
        let generation = self.execution.generation();
        crate::workflow_runtime::Runtime::assemble(
            native,
            self.host.as_ref().unwrap().clone(),
            person,
            cleanup,
            generation.draining.clone(),
            generation.cancelled.clone(),
        )
        .map(|(runtime, _)| runtime)
    }
    pub fn new(native: crate::member::native::Native, host: Arc<gb_environment::Host>) -> Self {
        Self {
            native: Some(native),
            host: Some(host),
            ..Self::default()
        }
    }
    pub fn attach_branvil(&mut self, branvil: branvil::api::DataApi) {
        if let Some(native) = &mut self.native {
            native.branvil = Some(branvil);
        }
    }
    pub async fn start(&self) -> anyhow::Result<crate::service::Status> {
        let _gate = self.gate.lock().await;
        if self.service.status().await.running {
            return Ok(self.service.status().await);
        }
        if self.execution.generation().draining.is_cancelled() {
            self.execution.wait(self.execution.begin_drain(false)).await;
        }
        let attempt = self.service.starting().await;
        let mut starting = Starting {
            service: self.service.clone(),
            attempt,
            settled: false,
        };
        let result = self.start_worker().await;
        if result.is_err() {
            self.service.startup_failed(attempt).await;
        }
        starting.settled = true;
        result
    }
    async fn start_worker(&self) -> anyhow::Result<crate::service::Status> {
        let native = self
            .native
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("config_not_found"))?;
        let settings = Settings::read(&native.workspace)?;
        let (worker, members) = NativeWorker::assemble(
            native,
            self.host.as_ref().unwrap().clone(),
            &settings,
            self.execution.clone(),
        )
        .await
        .context(StartupConfig)?;
        // Publish the worker before starting its tasks: cancellation while waiting
        // for this lock must not leave an unobservable service running.
        *self.worker.write().await = Some(worker.clone());
        self.service
            .start(
                &native.data,
                &native.workspace,
                members,
                settings,
                worker.clone(),
            )
            .await?;
        Ok(self.service.status().await)
    }
    pub async fn stop(&self, force: bool) -> crate::service::Status {
        let (done, generation) = {
            let _gate = self.gate.lock().await;
            let generation = self.execution.begin_drain(force);
            (self.service.request_stop(force).await, generation)
        };
        let draining = {
            let execution = self.execution.clone();
            let done = done.clone();
            tokio::spawn(async move {
                if let Some(done) = done {
                    done.cancelled().await;
                }
                execution.wait(generation).await;
            })
        };
        if let Some(done) = done {
            self.service.wait_stop(done, force).await;
        }
        if !self.service.status().await.running {
            if force {
                let _ = tokio::time::timeout(std::time::Duration::from_secs(5), draining).await;
            } else {
                let _ = draining.await;
            }
        }
        self.service.status().await
    }
    pub async fn view(&self) -> serde_json::Value {
        let status = self.service.status().await;
        let mut scheduler = serde_json::to_value(&status).expect("service status");
        scheduler["target"] = serde_json::json!("scheduler");
        for key in [
            "subscription_count",
            "listener_count",
            "events_drained_count",
            "events_auth_failed_count",
        ] {
            scheduler[key] = serde_json::Value::Null;
        }
        scheduler["events_auth_failed_persons"] = serde_json::json!([]);
        scheduler["events_chat_provider"] = serde_json::json!("");
        let worker = self.worker.read().await;
        scheduler["member_routines"] = serde_json::json!(
            worker
                .as_ref()
                .filter(|_| status.running)
                .map(|worker| worker.scheduler.heartbeats())
                .unwrap_or_default()
        );
        let listeners = if status.running {
            worker.as_ref().map_or(0, |w| w.groups.len())
        } else {
            0
        };
        let failed = status.running
            && worker
                .as_ref()
                .is_some_and(|w| w.reception_failed.load(std::sync::atomic::Ordering::SeqCst));
        let events_running = status.running && listeners > 0 && !failed;
        let mut auth_people = vec![];
        let mut auth_count = 0;
        if let Some(worker) = worker.as_ref().filter(|_| status.running) {
            for group in &worker.groups {
                if group.auth_failed() {
                    auth_count += 1;
                    auth_people.extend(group.people());
                }
            }
            auth_people.extend(worker.auth_skipped.clone());
            auth_count += worker.auth_skipped.len();
        }
        let mut missing_people = worker
            .as_ref()
            .filter(|_| status.running)
            .map(|w| {
                w.groups
                    .iter()
                    .flat_map(|g| g.missing_channel_people())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default();
        missing_people.sort();
        missing_people.dedup();
        let event_errors: u64 = worker
            .as_ref()
            .map(|w| w.groups.iter().map(|g| g.error_count()).sum())
            .unwrap_or(0);
        auth_people.sort();
        auth_people.dedup();
        let mut events =
            serde_json::to_value(crate::service::Status::default()).expect("service status");
        events["started_at"] = serde_json::json!(status.started_at);
        events["stopped_at"] = serde_json::json!(status.stopped_at);
        let subscriptions: Option<usize> = worker
            .as_ref()
            .map(|w| w.groups.iter().map(|g| g.subscription_count()).sum());
        let drained: Option<u64> = worker
            .as_ref()
            .map(|w| w.groups.iter().map(|g| g.drained_count()).sum());
        let summary = serde_json::json!({"target":"events",
            "subscription_count":subscriptions,"events_drained_count":drained,
            "cycle_count":status.cycle_count,"cycle_failure_count":worker.as_ref().map(|_|event_errors),
            "error":if failed {Some(crate::i18n::t("en", "app_api.errors.service_events_failed", &[]))} else {None},
            "events_missing_channel_count":missing_people.len(),"events_missing_channel_persons":missing_people,"events_error_count":event_errors,
            "state":if failed {"failed"} else if events_running || matches!(status.state,"starting"|"failed") {status.state} else {"stopped"},
            "running":events_running,"listener_count":status.started_at.as_ref().map(|_|listeners),"events_auth_failed_count":status.started_at.as_ref().map(|_|auth_count),"events_auth_failed_persons":auth_people,"events_chat_provider":worker.as_ref().map(|w|&w.chat_provider).cloned().unwrap_or_default()});
        events
            .as_object_mut()
            .unwrap()
            .extend(summary.as_object().unwrap().clone());
        serde_json::json!({"scheduler":scheduler,"events":events,
            "active_works":self.execution.generation().snapshot(),"has_active_work":status.running || status.state == "stopping" || !self.execution.generation().work.is_empty()})
    }
}

pub(crate) async fn status(
    axum::extract::State(api): axum::extract::State<crate::api::Shared>,
) -> axum::Json<serde_json::Value> {
    axum::Json(api.service.view().await)
}
pub(crate) async fn start(
    axum::extract::State(api): axum::extract::State<crate::api::Shared>,
    headers: axum::http::HeaderMap,
) -> axum::response::Response {
    use axum::response::IntoResponse;
    match api.service.start().await {
        Ok(_) => axum::Json(api.service.view().await).into_response(),
        Err(error) => {
            tracing::error!("cannot start service: {error:#}");
            if let Some(held) = error.downcast_ref::<crate::service::AlreadyRunning>() {
                return crate::api::ApiError::new(
                    axum::http::StatusCode::CONFLICT,
                    "service_already_running",
                    &headers,
                )
                .context(held.owner.clone().unwrap_or_else(|| serde_json::json!({})))
                .into_response();
            }
            if let Some(invalid) = error.downcast_ref::<crate::service_settings::InvalidSettings>()
            {
                return crate::api::ApiError::new(
                    axum::http::StatusCode::BAD_REQUEST,
                    "service_settings_invalid",
                    &headers,
                )
                .key(
                    "service_settings_invalid",
                    vec![
                        ("path", invalid.path.display().to_string()),
                        ("reason", invalid.reason.clone()),
                    ],
                )
                .into_response();
            }
            let (status, code) = startup_error(&error, api.workspace.is_some());
            crate::api::ApiError::new(status, code, &headers).into_response()
        }
    }
}
fn startup_error(
    error: &anyhow::Error,
    configured: bool,
) -> (axum::http::StatusCode, &'static str) {
    use axum::http::StatusCode;
    if error.downcast_ref::<StartupSecrets>().is_some() {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            "service_secret_store_unavailable",
        )
    } else if error.downcast_ref::<std::io::Error>().is_some() {
        (StatusCode::SERVICE_UNAVAILABLE, "service_path_unavailable")
    } else if !configured {
        (StatusCode::BAD_REQUEST, "config_not_found")
    } else if error.downcast_ref::<StartupConfig>().is_some() {
        (StatusCode::BAD_REQUEST, "service_config_invalid")
    } else {
        (StatusCode::INTERNAL_SERVER_ERROR, "internal_error")
    }
}
#[derive(Default, serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct Stop {
    force: bool,
}
pub(crate) async fn stop(
    axum::extract::State(api): axum::extract::State<crate::api::Shared>,
    crate::api::Body(request): crate::api::Body<Stop>,
) -> axum::Json<serde_json::Value> {
    api.service.stop(request.force).await;
    axum::Json(api.service.view().await)
}

pub struct NativeWorker {
    service_instance_id: String,
    execution: crate::execution::Coordinator,
    scheduler: crate::scheduler::Scheduler,
    groups: Vec<Arc<crate::chat_receiver::Group>>,
    started: OnceCell<()>,
    receivers: TaskTracker,
    cleanup: TaskTracker,
    draining: CancellationToken,
    cancelled: CancellationToken,
    stop_reception: CancellationToken,
    reception_failed: Arc<std::sync::atomic::AtomicBool>,
    auth_skipped: Vec<String>,
    chat_provider: String,
}

impl NativeWorker {
    pub async fn assemble(
        native: &crate::member::native::Native,
        host: Arc<gb_environment::Host>,
        settings: &Settings,
        execution: crate::execution::Coordinator,
    ) -> anyhow::Result<(Arc<Self>, Vec<String>)> {
        let generation = execution.generation();
        let team = gb_config::team::Team::load(
            &native.workspace.join(".guildbotics/config"),
            &native.identity_root,
        )?;
        let cleanup = TaskTracker::new();
        let draining = generation.draining.clone();
        let cancelled = generation.cancelled.clone();
        let mut scheduler = crate::scheduler::Scheduler::default();
        let mut listeners = BTreeMap::<String, crate::chat_receiver::Connection>::new();
        let mut groups = BTreeMap::<String, Vec<Arc<crate::chat_receiver::Receiver>>>::new();
        let mut members = vec![];
        let mut auth_skipped = vec![];
        let mut chat_provider = String::new();
        for member in team
            .members
            .iter()
            .filter(|m| m.is_active && m.person_type == "agent")
        {
            let (mut runtime, metadata) = crate::workflow_runtime::Runtime::assemble_sources(
                native,
                host.clone(),
                &member.person_id,
                cleanup.clone(),
                draining.clone(),
                cancelled.clone(),
                settings.event_queue.then_some(&mut listeners),
            )?;
            runtime.generation = Some(generation.clone());
            chat_provider.clone_from(&runtime.chat_provider);
            if settings.event_queue
                && !runtime.channels.is_empty()
                && matches!(
                    runtime.chat_error,
                    Some(gb_ports::ServiceError::MissingCredential { .. })
                )
            {
                auth_skipped.push(member.person_id.clone());
            }
            let store = Arc::new(crate::chat_state::Store {
                workspace: native.workspace.clone(),
                writes: runtime.writes.clone(),
                limit: 500,
            });
            let runtime = Arc::new(runtime);
            let chat = if settings.event_queue {
                if let Some(connection) = &runtime.reception {
                    let connection = connection.clone();
                    let chat = runtime
                        .chat
                        .clone()
                        .ok_or_else(|| anyhow::anyhow!("chat reception requires chat service"))?;
                    let receiver = crate::chat_receiver::Receiver::new(
                        member.person_id.clone(),
                        chat.clone(),
                        connection.clone(),
                        runtime.channels.clone(),
                        store.clone(),
                    )
                    .await?;
                    groups
                        .entry(connection.key.clone())
                        .or_default()
                        .push(Arc::new(receiver));
                    Some(crate::chat_dispatch::Dispatcher {
                        person: member.person_id.clone(),
                        service: connection.service.clone(),
                        language: runtime.language.clone(),
                        roster: runtime.roster.clone(),
                        chat,
                        store,
                        runner: runtime.clone(),
                    })
                } else {
                    None
                }
            } else {
                None
            };
            let (scheduled, routines) = commands(&metadata, chrono::Local::now().naive_local())?;
            scheduler.add(
                member.person_id.clone(),
                Arc::new(crate::native_scheduler::Engine::new(runtime, chat)),
                scheduled,
                routines,
            );
            members.push(member.person_id.clone());
        }
        let groups = groups
            .into_values()
            .map(|receivers| crate::chat_receiver::Group::new(receivers).map(Arc::new))
            .collect::<anyhow::Result<_>>()?;
        Ok((
            Arc::new(Self {
                service_instance_id: generation.id.clone(),
                execution,
                scheduler,
                groups,
                started: OnceCell::new(),
                receivers: TaskTracker::new(),
                cleanup,
                draining,
                cancelled,
                stop_reception: CancellationToken::new(),
                reception_failed: Default::default(),
                auth_skipped,
                chat_provider,
            }),
            members,
        ))
    }
}

fn commands(
    metadata: &serde_json::Value,
    now: chrono::NaiveDateTime,
) -> anyhow::Result<(Vec<crate::schedule::ScheduledCommand>, Vec<String>)> {
    let routines = serde_json::from_value(
        metadata
            .get("routine_commands")
            .cloned()
            .unwrap_or_else(|| serde_json::json!([])),
    )?;
    let mut scheduled = vec![];
    if let Some(rows) = metadata.get("task_schedules") {
        for row in rows
            .as_array()
            .ok_or_else(|| anyhow::anyhow!("task_schedules must be a list"))?
        {
            let command = row["command"]
                .as_str()
                .ok_or_else(|| anyhow::anyhow!("schedule command must be a string"))?;
            for schedule in row["schedules"]
                .as_array()
                .ok_or_else(|| anyhow::anyhow!("schedules must be a list"))?
            {
                scheduled.push(crate::schedule::ScheduledCommand::new(
                    command.into(),
                    schedule
                        .as_str()
                        .ok_or_else(|| anyhow::anyhow!("schedule must be a string"))?
                        .into(),
                    now,
                )?);
            }
        }
    }
    Ok((scheduled, routines))
}

impl Worker for NativeWorker {
    fn service_instance_id(&self) -> Option<&str> {
        Some(&self.service_instance_id)
    }
    fn cycle<'a>(&'a self, person: &'a str, settings: &'a Settings) -> Pending<'a, Outcome> {
        Box::pin(async move {
            self.started
                .get_or_init(|| async {
                    for group in &self.groups {
                        let (group, stop) = (group.clone(), self.stop_reception.clone());
                        let failed = self.reception_failed.clone();
                        self.receivers.spawn(async move {
                            if group.run(stop).await.is_err() {
                                failed.store(true, std::sync::atomic::Ordering::SeqCst);
                                tracing::error!("chat receiver stopped");
                            }
                        });
                    }
                })
                .await;
            self.scheduler.cycle(person, settings).await
        })
    }
    fn worker_failed(&self, person: &str, errors: u8) {
        self.scheduler.worker_failed(person, errors);
    }
    fn request_stop(&self, force: bool) {
        self.draining.cancel();
        self.stop_reception.cancel();
        if force {
            self.cancelled.cancel();
        }
    }
    fn shutdown(&self) -> Pending<'_, ()> {
        Box::pin(async {
            self.receivers.close();
            self.receivers.wait().await;
            self.cleanup.close();
            self.cleanup.wait().await;
            self.execution.wait(self.execution.begin_drain(false)).await;
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn startup_failure_is_recorded_and_never_started_events_are_empty() {
        let controller = Controller::default();
        let view = controller.view().await;
        assert_eq!(view["events"]["events_error_count"], 0);
        assert!(view["events"]["error"].is_null());
        assert!(controller.start().await.is_err());
        let status = controller.service.status().await;
        assert_eq!(status.state, "failed");
        assert!(!status.running);
        assert!(status.error.is_some());
    }

    #[test]
    fn startup_error_classes_preserve_config_paths_secrets_and_internal_errors() {
        use axum::http::StatusCode;
        for (error, expected, code) in [
            (
                anyhow::anyhow!("bad yaml").context(StartupConfig),
                StatusCode::BAD_REQUEST,
                "service_config_invalid",
            ),
            (
                anyhow::anyhow!(std::io::Error::from(std::io::ErrorKind::PermissionDenied))
                    .context(StartupConfig),
                StatusCode::SERVICE_UNAVAILABLE,
                "service_path_unavailable",
            ),
            (
                anyhow::anyhow!("keychain denied")
                    .context(StartupSecrets)
                    .context(StartupConfig),
                StatusCode::SERVICE_UNAVAILABLE,
                "service_secret_store_unavailable",
            ),
            (
                anyhow::anyhow!("unexpected"),
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
            ),
        ] {
            assert_eq!(startup_error(&error, true), (expected, code));
            for language in ["en", "ja"] {
                let key = format!("app_api.errors.{code}");
                assert_ne!(crate::i18n::t(language, &key, &[]), key);
            }
        }
    }

    #[tokio::test(start_paused = true)]
    async fn forced_manual_drain_returns_without_reopening_live_work() {
        let controller = Controller::default();
        let release = CancellationToken::new();
        let task = controller
            .execution
            .spawn({
                let release = release.clone();
                move |_| async move { release.cancelled().await }
            })
            .unwrap();
        let generation = controller.execution.generation();
        controller.stop(true).await;
        assert!(!task.is_finished());
        assert!(controller.execution.spawn(|_| async {}).is_err());
        release.cancel();
        task.await.unwrap();
        while controller.execution.generation().id == generation.id {
            tokio::task::yield_now().await;
        }
        assert!(
            controller
                .execution
                .spawn(|_| async {})
                .unwrap()
                .await
                .is_ok()
        );
    }

    #[tokio::test]
    async fn cancelled_manual_stop_still_reopens_admission_after_settlement() {
        let controller = Arc::new(Controller::default());
        let generation = controller.execution.generation();
        let release = CancellationToken::new();
        let task = controller
            .execution
            .spawn({
                let release = release.clone();
                move |_| async move { release.cancelled().await }
            })
            .unwrap();
        let stopper = tokio::spawn({
            let controller = controller.clone();
            async move { controller.stop(false).await }
        });
        generation.draining.cancelled().await;
        tokio::task::yield_now().await;
        stopper.abort();
        let _ = stopper.await;
        release.cancel();
        task.await.unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            while controller.execution.generation().id == generation.id {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(
            controller
                .execution
                .spawn(|_| async {})
                .unwrap()
                .await
                .is_ok()
        );
    }

    #[tokio::test]
    async fn natural_worker_completion_reopens_manual_admission_without_status_polling() {
        let root = tempfile::tempdir().unwrap();
        let execution = crate::execution::Coordinator::default();
        let generation = execution.generation();
        let worker = Arc::new(NativeWorker {
            service_instance_id: generation.id.clone(),
            execution: execution.clone(),
            scheduler: Default::default(),
            groups: vec![],
            started: OnceCell::new(),
            receivers: TaskTracker::new(),
            cleanup: TaskTracker::new(),
            draining: generation.draining.clone(),
            cancelled: generation.cancelled.clone(),
            stop_reception: CancellationToken::new(),
            reception_failed: Default::default(),
            auth_skipped: vec![],
            chat_provider: String::new(),
        });
        let service = crate::service::Service::default();
        service
            .start(
                root.path(),
                root.path(),
                vec![],
                Settings::default(),
                worker,
            )
            .await
            .unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            loop {
                if gb_base::files::FileLock::try_hold(&root.path().join("run/service.lock"))
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
        assert!(execution.spawn(|_| async {}).unwrap().await.is_ok());
        assert_ne!(execution.generation().id, generation.id);
    }

    #[tokio::test]
    async fn cancelled_start_cannot_leave_a_running_service_without_its_worker() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let controller = Arc::new(Controller::new(native, host));
        let publication = controller.worker.write().await;
        let starting = {
            let controller = controller.clone();
            tokio::spawn(async move { controller.start().await })
        };
        tokio::task::yield_now().await;
        assert!(controller.gate.try_lock().is_err());
        assert!(!controller.service.status().await.running);
        assert_eq!(controller.service.status().await.state, "starting");
        starting.abort();
        assert!(starting.await.unwrap_err().is_cancelled());
        while controller.service.status().await.state == "starting" {
            tokio::task::yield_now().await;
        }
        assert_eq!(controller.service.status().await.state, "failed");
        assert!(controller.service.status().await.error.is_some());
        drop(publication);
        assert!(controller.start().await.unwrap().running);
        assert!(controller.worker.read().await.is_some());
        assert!(!controller.stop(true).await.running);
    }
}
