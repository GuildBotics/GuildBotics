//! Assemble native member workers and shared chat reception for a service run.
use crate::{
    service::{Outcome, Pending, Worker},
    service_settings::Settings,
};
use std::{collections::BTreeMap, sync::Arc};
use tokio::sync::OnceCell;

#[derive(Debug)]
pub(crate) struct StartupSecrets;
impl std::fmt::Display for StartupSecrets {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("secret_store_unavailable")
    }
}
impl std::error::Error for StartupSecrets {}
use tokio_util::{sync::CancellationToken, task::TaskTracker};

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
    pub(crate) async fn reset_chat_state(
        &self,
        apply: impl FnOnce() -> anyhow::Result<serde_json::Value> + Send + 'static,
    ) -> anyhow::Result<serde_json::Value> {
        let _gate = self.gate.lock().await;
        anyhow::ensure!(!self.service.status().await.running, "runtime_running");
        tokio::task::spawn_blocking(apply).await?
    }
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
        let native = match self.native.as_ref() {
            Some(native) => native,
            None => {
                let starting = self
                    .service
                    .begin_start(uuid::Uuid::new_v4().simple().to_string())
                    .await;
                return starting
                    .finish(Err(anyhow::anyhow!("config_not_found")))
                    .await;
            }
        };
        let settings = Settings::read(&native.workspace)?;
        let admitted =
            crate::service::Service::admit(&native.data, &native.workspace, &settings).await?;
        let starting = self.service.begin_start(admitted.run_id.clone()).await;
        let result = self.start_worker(native, settings, admitted).await;
        starting.finish(result).await
    }
    async fn start_worker(
        &self,
        native: &crate::member::native::Native,
        settings: Settings,
        admitted: crate::service::Admission,
    ) -> anyhow::Result<crate::service::Status> {
        let (worker, members) = NativeWorker::assemble(
            native,
            self.host.as_ref().unwrap().clone(),
            &settings,
            self.execution.clone(),
            admitted.run_id.clone(),
        )
        .await?;
        let _ = worker.service.set(self.service.clone());
        // Publish the worker before starting its tasks: cancellation while waiting
        // for this lock must not leave an unobservable service running.
        *self.worker.write().await = Some(worker.clone());
        self.service
            .start_admitted(
                admitted,
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
            "events_missing_credential_count",
        ] {
            scheduler[key] = serde_json::Value::Null;
        }
        scheduler["events_auth_failed_persons"] = serde_json::json!([]);
        scheduler["events_missing_credential_persons"] = serde_json::json!([]);
        scheduler["events_chat_provider"] = serde_json::json!("");
        let worker_guard = self.worker.read().await;
        let worker = worker_guard.as_ref().filter(|_| status.state != "starting");
        scheduler["member_routines"] = serde_json::json!(
            worker
                .as_ref()
                .filter(|_| status.running)
                .map(|worker| worker.scheduler.heartbeats())
                .unwrap_or_default()
        );
        scheduler["member_pull_requests"] = serde_json::json!(
            worker
                .as_ref()
                .filter(|_| status.running)
                .map(|worker| worker
                    .quotas
                    .iter()
                    .filter_map(|(person, quota)| quota
                        .snapshot()
                        .map(|load| (person.clone(), load)))
                    .collect::<BTreeMap<_, _>>())
                .unwrap_or_default()
        );
        let events = self.service.events_status().await;
        serde_json::json!({"scheduler":scheduler,"events":events,
            "active_works":self.execution.generation().snapshot(),"has_active_work":status.running || status.state == "stopping" || !self.execution.generation().work.is_empty()})
    }
}

pub(crate) async fn status(
    axum::extract::State(api): axum::extract::State<crate::api::Shared>,
) -> axum::Json<serde_json::Value> {
    axum::Json(api.selected().service.view().await)
}
pub(crate) async fn start(
    axum::extract::State(api): axum::extract::State<crate::api::Shared>,
    headers: axum::http::HeaderMap,
) -> axum::response::Response {
    use axum::response::IntoResponse;
    let Some(admitted) = api.admit() else {
        return crate::api::ApiError::new(
            axum::http::StatusCode::CONFLICT,
            "command_workspace_changing",
            &headers,
        )
        .into_response();
    };
    // The start is the server's work, not the request's: a client that goes
    // away does not cancel it into a failure, and a server that stops waits
    // for it.
    let selected = api.selected();
    let started = api
        .work
        .spawn(async move {
            let _admitted = admitted;
            selected.service.start().await?;
            Ok::<_, anyhow::Error>(selected.service.view().await)
        })
        .await
        .map_err(anyhow::Error::from)
        .and_then(|started| started);
    match started {
        Ok(view) => axum::Json(view).into_response(),
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
            startup_error(&error, api.selected().workspace.is_some(), &headers).into_response()
        }
    }
}
fn startup_error(
    error: &anyhow::Error,
    configured: bool,
    headers: &axum::http::HeaderMap,
) -> crate::api::ApiError {
    use axum::http::StatusCode;
    if error.downcast_ref::<StartupSecrets>().is_some() {
        crate::api::ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "secret_store_unavailable",
            headers,
        )
        .key(
            "secret_store_unavailable",
            vec![("reason", error.root_cause().to_string())],
        )
    } else if !configured {
        crate::api::ApiError::new(StatusCode::BAD_REQUEST, "config_not_found", headers)
    } else if let Some(file) = error.downcast_ref::<gb_config::ConfigFile>() {
        crate::api::ApiError::new(StatusCode::BAD_REQUEST, "member_config_invalid", headers)
            .reason(file.to_string())
            .context(serde_json::json!({"path":file.path()}))
    } else if error.downcast_ref::<std::io::Error>().is_some() {
        crate::api::ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "service_path_unavailable",
            headers,
        )
    } else {
        crate::api::ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "internal_error", headers)
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
    api.selected().service.stop(request.force).await;
    axum::Json(api.selected().service.view().await)
}

pub struct NativeWorker {
    service: std::sync::OnceLock<crate::service::Service>,
    service_instance_id: String,
    scheduler: crate::scheduler::Scheduler,
    quotas: BTreeMap<String, Arc<crate::ticket_selector::PullRequestQuota>>,
    groups: Vec<Arc<crate::chat_receiver::Group>>,
    started: OnceCell<()>,
    receivers: TaskTracker,
    cleanup: TaskTracker,
    draining: CancellationToken,
    cancelled: CancellationToken,
    stop_reception: CancellationToken,
    reception_failed: Arc<std::sync::atomic::AtomicBool>,
    missing_credentials: Vec<String>,
    chat_provider: String,
}

impl NativeWorker {
    pub async fn assemble(
        native: &crate::member::native::Native,
        host: Arc<gb_environment::Host>,
        settings: &Settings,
        execution: crate::execution::Coordinator,
        service_instance_id: String,
    ) -> anyhow::Result<(Arc<Self>, Vec<String>)> {
        let generation = execution.generation();
        let team = gb_config::team::Team::load(
            &native.workspace.join(".guildbotics/config"),
            &native.identity_root,
        )?;
        let cleanup = TaskTracker::new();
        let draining = generation.draining.child_token();
        let cancelled = generation.cancelled.clone();
        let mut scheduler = crate::scheduler::Scheduler::default();
        let mut listeners = BTreeMap::<String, crate::chat_receiver::Connection>::new();
        let mut groups = BTreeMap::<String, Vec<Arc<crate::chat_receiver::Receiver>>>::new();
        let mut members = vec![];
        let mut quotas = BTreeMap::new();
        let mut missing_credentials = vec![];
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
            let quota = Arc::new(crate::ticket_selector::PullRequestQuota::new(
                settings.max_open_pull_requests,
            ));
            runtime.quota = Some(quota.clone());
            quotas.insert(member.person_id.clone(), quota);
            runtime.generation = Some(generation.clone());
            runtime.service_run_id = Some(service_instance_id.clone());
            chat_provider.clone_from(&runtime.chat_provider);
            if settings.event_queue
                && !runtime.channels.is_empty()
                && matches!(
                    runtime.chat_error,
                    Some(gb_ports::ServiceError::MissingCredential { .. })
                )
            {
                missing_credentials.push(member.person_id.clone());
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
            let (scheduled, routines) = commands(&metadata, chrono::Local::now().naive_local())
                .map_err(|error| {
                    gb_config::ConfigFile::wrap(
                        native
                            .workspace
                            .join(".guildbotics/config/team/members")
                            .join(&member.person_id)
                            .join("member.yml"),
                        error,
                    )
                })?;
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
                service: Default::default(),
                service_instance_id,
                scheduler,
                quotas,
                groups,
                started: OnceCell::new(),
                receivers: TaskTracker::new(),
                cleanup,
                draining,
                cancelled,
                stop_reception: CancellationToken::new(),
                reception_failed: Default::default(),
                missing_credentials,
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
    fn listener_count(&self) -> usize {
        self.groups.len()
    }
    fn reception_failed(&self) -> bool {
        self.reception_failed
            .load(std::sync::atomic::Ordering::SeqCst)
    }
    fn event_status(&self, status: &crate::service::Status) -> serde_json::Value {
        let mut auth_people = vec![];
        let mut auth_count = 0;
        let mut missing_people = vec![];
        if status.running {
            for group in &self.groups {
                if group.auth_failed() {
                    auth_count += 1;
                    auth_people.extend(group.people());
                }
                missing_people.extend(group.missing_channel_people());
            }
        }
        auth_people.sort();
        auth_people.dedup();
        missing_people.sort();
        missing_people.dedup();
        let event_errors: u64 = self.groups.iter().map(|g| g.error_count()).sum();
        let subscriptions: usize = self.groups.iter().map(|g| g.subscription_count()).sum();
        let drained: u64 = self.groups.iter().map(|g| g.drained_count()).sum();
        let mut events = serde_json::to_value(crate::service::Status::default()).unwrap();
        events.as_object_mut().unwrap().extend(
            serde_json::json!({
                "target":"events", "subscription_count":subscriptions,
                "events_drained_count":drained,"cycle_count":status.cycle_count,
                "cycle_failure_count":event_errors,"events_error_count":event_errors,
                "events_missing_channel_count":missing_people.len(),
                "events_missing_channel_persons":missing_people,
                "listener_count":if status.running {self.groups.len()} else {0},
                "events_auth_failed_count":auth_count,"events_auth_failed_persons":auth_people,
                "events_missing_credential_count":if status.running {self.missing_credentials.len()} else {0},
                "events_missing_credential_persons":if status.running {self.missing_credentials.clone()} else {vec![]},
                "events_chat_provider":self.chat_provider
            })
            .as_object()
            .unwrap()
            .clone(),
        );
        events
    }
    fn cycle<'a>(&'a self, person: &'a str, settings: &'a Settings) -> Pending<'a, Outcome> {
        Box::pin(async move {
            self.started
                .get_or_init(|| async {
                    for group in &self.groups {
                        let (group, stop) = (group.clone(), self.stop_reception.clone());
                        let failed = self.reception_failed.clone();
                        let service = self.service.get().cloned();
                        let run_id = self.service_instance_id.clone();
                        self.receivers.spawn(supervise_reception(
                            async move { group.run(stop.clone()).await },
                            self.stop_reception.clone(),
                            failed,
                            service,
                            run_id,
                        ));
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
        })
    }
}

/// Observe both failures returned by the receiver and unwinding panics.
async fn supervise_reception(
    reception: impl std::future::Future<Output = anyhow::Result<()>>,
    stop: CancellationToken,
    failed: Arc<std::sync::atomic::AtomicBool>,
    service: Option<crate::service::Service>,
    run_id: String,
) {
    use futures_util::FutureExt;
    let result = std::panic::AssertUnwindSafe(reception).catch_unwind().await;
    if !matches!(result, Ok(Ok(()))) || !stop.is_cancelled() {
        failed.store(true, std::sync::atomic::Ordering::SeqCst);
        if let Some(service) = service {
            service.reception_failed(&run_id).await;
        }
        tracing::error!("chat receiver stopped unexpectedly");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Receiving;
    impl Worker for Receiving {
        fn listener_count(&self) -> usize {
            1
        }
        fn cycle<'a>(&'a self, _: &'a str, _: &'a Settings) -> Pending<'a, Outcome> {
            Box::pin(std::future::pending())
        }
        fn worker_failed(&self, _: &str, _: u8) {}
    }

    #[tokio::test]
    async fn receiver_termination_updates_service_events_and_alerts() {
        for mode in ["panic", "error", "early_return", "stop"] {
            let root = tempfile::tempdir().unwrap();
            let service = crate::service::Service::default();
            let bus = Arc::new(crate::events::EventBus::default());
            let store = gb_observability::Diagnostics::open(root.path());
            store.start_system_session("receiver-test").unwrap();
            bus.record_in(Some(store.clone()));
            service.attach_events(bus.clone());
            service
                .start(
                    root.path(),
                    root.path(),
                    vec!["alice".into()],
                    Settings::default(),
                    Arc::new(Receiving),
                )
                .await
                .unwrap();
            let run_id = bus.snapshot()[0]["attributes"]["service_run_id"]
                .as_str()
                .unwrap()
                .to_owned();
            let stop = CancellationToken::new();
            if mode == "stop" {
                stop.cancel();
            }
            let failed = Arc::new(std::sync::atomic::AtomicBool::new(false));
            supervise_reception(
                async {
                    match mode {
                        "panic" => panic!("synthetic receiver panic"),
                        "error" => anyhow::bail!("synthetic failure"),
                        _ => Ok(()),
                    }
                },
                stop,
                failed.clone(),
                Some(service.clone()),
                run_id,
            )
            .await;
            let events = service.events_status().await;
            assert_eq!(
                events["state"],
                if mode == "stop" { "running" } else { "failed" }
            );
            assert_eq!(
                failed.load(std::sync::atomic::Ordering::SeqCst),
                mode != "stop"
            );
            assert_eq!(bus.snapshot().last().unwrap()["payload"], events);
            let alerts = gb_observability::health::SystemAlerts::list(
                &store,
                &serde_json::json!({"events": events}),
                &[],
                None,
            );
            let alerts = alerts["alerts"].as_array().unwrap();
            if mode == "stop" {
                assert!(alerts.is_empty());
            } else {
                assert!(alerts.iter().any(|a| a["id"] == "runtime:events"
                    && a["code"] == "events_failed"
                    && a["severity"] == "critical"));
                assert_eq!(events["running"], false);
                assert!(events["error"].as_str().is_some_and(|s| !s.is_empty()));
            }
            service.stop(true).await;
        }
    }
    #[tokio::test]
    async fn startup_failure_is_recorded_and_never_started_events_are_empty() {
        let controller = Controller::default();
        let bus = Arc::new(crate::events::EventBus::default());
        controller.service.attach_events(bus.clone());
        let view = controller.view().await;
        assert_eq!(view["events"]["events_error_count"], 0);
        assert!(view["events"]["error"].is_null());
        assert!(controller.start().await.is_err());
        let status = controller.service.status().await;
        assert_eq!(status.state, "failed");
        assert!(!status.running);
        assert!(status.error.is_some());
        let records = bus.snapshot();
        assert_eq!(
            records
                .iter()
                .map(|r| r["type"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["scheduler.starting", "scheduler.failed"]
        );
        assert_eq!(
            records[0]["attributes"]["service_run_id"],
            records[1]["attributes"]["service_run_id"]
        );
        assert_eq!(controller.view().await["events"], view["events"]);
    }

    #[tokio::test]
    async fn startup_error_classes_preserve_config_paths_secrets_and_internal_errors() {
        use axum::response::IntoResponse;
        for language in ["en", "ja"] {
            let mut headers = axum::http::HeaderMap::new();
            headers.insert("x-guildbotics-language", language.parse().unwrap());
            for (error, status, code, path, reason) in [
                (
                    gb_config::ConfigFile::wrap("/team/member.yml", anyhow::anyhow!("bad yaml")),
                    400,
                    "member_config_invalid",
                    Some("/team/member.yml"),
                    "bad yaml",
                ),
                (
                    gb_config::ConfigFile::wrap(
                        "/team/project.yml",
                        anyhow::anyhow!(std::io::Error::from(std::io::ErrorKind::NotFound)),
                    ),
                    400,
                    "member_config_invalid",
                    Some("/team/project.yml"),
                    "",
                ),
                (
                    anyhow::anyhow!("keychain denied").context(StartupSecrets),
                    503,
                    "secret_store_unavailable",
                    None,
                    "keychain denied",
                ),
                (
                    anyhow::anyhow!(std::io::Error::from(std::io::ErrorKind::PermissionDenied)),
                    503,
                    "service_path_unavailable",
                    None,
                    "",
                ),
                (
                    anyhow::anyhow!("unexpected"),
                    500,
                    "internal_error",
                    None,
                    "",
                ),
            ] {
                let response = startup_error(&error, true, &headers).into_response();
                assert_eq!(response.status().as_u16(), status);
                let body: serde_json::Value = serde_json::from_slice(
                    &axum::body::to_bytes(response.into_body(), usize::MAX)
                        .await
                        .unwrap(),
                )
                .unwrap();
                assert_eq!(body["code"], code);
                assert!(body["message"].as_str().unwrap().contains(reason));
                if let Some(path) = path {
                    assert_eq!(body["context"]["path"], path);
                    assert_eq!(body["message"].as_str().unwrap().matches(path).count(), 1);
                    assert!(
                        !body["message"]
                            .as_str()
                            .unwrap()
                            .contains("service_config_invalid")
                    );
                }
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
            service: Default::default(),
            service_instance_id: generation.id.clone(),
            scheduler: Default::default(),
            quotas: Default::default(),
            groups: vec![],
            started: OnceCell::new(),
            receivers: TaskTracker::new(),
            cleanup: TaskTracker::new(),
            draining: generation.draining.child_token(),
            cancelled: generation.cancelled.clone(),
            stop_reception: CancellationToken::new(),
            reception_failed: Default::default(),
            missing_credentials: vec![],
            chat_provider: String::new(),
        });
        let release = CancellationToken::new();
        let manual = execution
            .spawn({
                let release = release.clone();
                move |_| async move { release.cancelled().await }
            })
            .unwrap();
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
        while service.status().await.running {
            tokio::task::yield_now().await;
        }
        assert_eq!(service.status().await.state, "stopped");
        assert!(!manual.is_finished());
        assert!(!generation.cancelled.is_cancelled());
        assert!(execution.spawn(|_| async {}).unwrap().await.is_ok());
        assert_eq!(execution.generation().id, generation.id);
        release.cancel();
        manual.await.unwrap();
    }

    #[tokio::test]
    async fn service_starts_have_distinct_ids_without_replacing_manual_work() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let execution = crate::execution::Coordinator::default();
        let generation = execution.generation();
        let (first, _) = NativeWorker::assemble(
            &native,
            host.clone(),
            &Settings::default(),
            execution.clone(),
            "first-service-run".into(),
        )
        .await
        .unwrap();
        let (second, _) = NativeWorker::assemble(
            &native,
            host,
            &Settings::default(),
            execution.clone(),
            "second-service-run".into(),
        )
        .await
        .unwrap();
        assert_ne!(first.service_instance_id, second.service_instance_id);
        assert_eq!(execution.generation().id, generation.id);
    }

    #[tokio::test]
    async fn a_new_start_waits_for_an_explicit_manual_drain() {
        let controller = Arc::new(Controller::default());
        let generation = controller.execution.generation();
        let release = CancellationToken::new();
        let manual = controller
            .execution
            .spawn({
                let release = release.clone();
                move |_| async move { release.cancelled().await }
            })
            .unwrap();
        controller.execution.begin_drain(false);
        let start = tokio::spawn({
            let controller = controller.clone();
            async move { controller.start().await }
        });
        tokio::task::yield_now().await;
        assert!(!start.is_finished());
        assert_eq!(controller.service.status().await.state, "stopped");
        release.cancel();
        manual.await.unwrap();
        assert!(start.await.unwrap().is_err());
        assert_ne!(controller.execution.generation().id, generation.id);
        assert!(!controller.execution.generation().draining.is_cancelled());
    }

    #[tokio::test]
    async fn starting_preserves_previous_events_but_hides_heartbeats_and_keeps_the_exit_guard() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let controller = Controller::new(native.clone(), host.clone());
        let (mut worker, _) = NativeWorker::assemble(
            &native,
            host,
            &Settings::default(),
            controller.execution.clone(),
            "previous-service-run".into(),
        )
        .await
        .unwrap();
        worker.scheduler.seed_heartbeat("aiko");
        assert!(!worker.scheduler.heartbeats().is_empty());
        let old = Arc::get_mut(&mut worker).unwrap();
        old.reception_failed
            .store(true, std::sync::atomic::Ordering::SeqCst);
        old.missing_credentials.push("previous-agent".into());
        old.chat_provider = "previous-provider".into();
        controller
            .service
            .start(
                &native.data,
                &native.workspace,
                vec![],
                Settings::default(),
                worker.clone(),
            )
            .await
            .unwrap();
        while controller.service.status().await.running {
            tokio::task::yield_now().await;
        }
        *controller.worker.write().await = Some(worker);
        let previous_events = controller.view().await["events"].clone();
        assert_eq!(previous_events["state"], "failed");
        assert_eq!(previous_events["events_chat_provider"], "previous-provider");
        controller.service.starting().await;
        let view = controller.view().await;
        assert_eq!(view["has_active_work"], true);
        assert_eq!(view["scheduler"]["running"], true);
        assert_eq!(view["scheduler"]["member_routines"], serde_json::json!([]));
        assert_eq!(view["events"], previous_events);
    }

    #[tokio::test]
    async fn preflight_refusals_leave_stopped_without_an_error() {
        for (invalid_settings, previous_failed) in
            [(false, false), (true, false), (false, true), (true, true)]
        {
            let root = tempfile::tempdir().unwrap();
            let native = crate::member::native::tests::fixture(root.path());
            let host = Arc::new(gb_environment::Host {
                data: native.data.clone(),
                home: root.path().join("home"),
                package: native.package.clone(),
                identity_root: native.identity_root.clone(),
            });
            let lock = if invalid_settings {
                let path = Settings::path(&native.workspace);
                std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                std::fs::write(path, "[invalid").unwrap();
                None
            } else {
                gb_base::files::FileLock::try_hold(&native.data.join("run/service.lock")).unwrap()
            };
            let controller = Controller::new(native, host);
            let bus = Arc::new(crate::events::EventBus::default());
            controller.service.attach_events(bus.clone());
            if previous_failed {
                let attempt = controller.service.starting().await;
                controller.service.startup_failed(attempt).await;
            }
            let previous = controller.view().await;
            let records = bus.snapshot();
            let error = controller.start().await.unwrap_err();
            assert_eq!(
                error
                    .downcast_ref::<crate::service_settings::InvalidSettings>()
                    .is_some(),
                invalid_settings
            );
            assert_eq!(
                error
                    .downcast_ref::<crate::service::AlreadyRunning>()
                    .is_some(),
                !invalid_settings
            );
            assert_eq!(controller.view().await, previous);
            assert_eq!(bus.snapshot(), records);
            drop(lock);
        }
    }

    struct RejectedCredentials;
    impl gb_ports::EventListener for RejectedCredentials {
        fn connected(&self) -> bool {
            false
        }
        fn auth_failed(&self) -> bool {
            true
        }
        fn start(&self) -> gb_ports::ServiceFuture<'_, ()> {
            Box::pin(async { Ok(()) })
        }
        fn stop(&self) -> gb_ports::ServiceFuture<'_, ()> {
            Box::pin(async { Ok(()) })
        }
        fn drain_events(&self) -> Vec<gb_ports::ChatEvent> {
            vec![]
        }
    }

    #[tokio::test]
    async fn events_status_distinguishes_rejected_credentials_from_missing_tokens() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let controller = Controller::new(native.clone(), host.clone());
        let runtime = controller.workflow("aiko", TaskTracker::new()).unwrap();
        let receiver = Arc::new(
            crate::chat_receiver::Receiver::new(
                "rejected-agent".into(),
                runtime.chat.clone().unwrap(),
                crate::chat_receiver::Connection {
                    service: "local".into(),
                    key: "test".into(),
                    listener: Arc::new(RejectedCredentials),
                    wake: Default::default(),
                },
                vec![],
                Arc::new(crate::chat_state::Store {
                    workspace: native.workspace.clone(),
                    writes: runtime.writes.clone(),
                    limit: 500,
                }),
            )
            .await
            .unwrap(),
        );
        let (mut worker, _) = NativeWorker::assemble(
            &native,
            host,
            &Settings::default(),
            controller.execution.clone(),
            "test-run".into(),
        )
        .await
        .unwrap();
        let worker = Arc::get_mut(&mut worker).unwrap();
        worker.groups.push(Arc::new(
            crate::chat_receiver::Group::new(vec![receiver]).unwrap(),
        ));
        worker.missing_credentials.push("missing-agent".into());
        let status = crate::service::Status {
            running: true,
            ..Default::default()
        };
        let events = worker.event_status(&status);
        assert_eq!(events["events_auth_failed_count"], 1);
        assert_eq!(
            events["events_auth_failed_persons"],
            serde_json::json!(["rejected-agent"])
        );
        assert_eq!(events["events_missing_credential_count"], 1);
        assert_eq!(
            events["events_missing_credential_persons"],
            serde_json::json!(["missing-agent"])
        );
        let stopped = worker.event_status(&crate::service::Status::default());
        for prefix in ["events_auth_failed", "events_missing_credential"] {
            assert_eq!(stopped[format!("{prefix}_count")], 0);
            assert_eq!(stopped[format!("{prefix}_persons")], serde_json::json!([]));
        }
    }

    #[tokio::test]
    async fn native_events_payload_matches_get_metadata_and_admitted_worker_identity() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        std::fs::write(
            native
                .workspace
                .join(".guildbotics/config/team/members/aiko/member.yml"),
            r#"{"is_active":false}"#,
        )
        .unwrap();
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let controller = Controller::new(native, host);
        let bus = Arc::new(crate::events::EventBus::default());
        controller.service.attach_events(bus.clone());
        controller.start().await.unwrap();
        controller.stop(true).await;
        let records = bus.snapshot();
        let events: Vec<_> = records.iter().filter(|r| r["source"] == "events").collect();
        assert_eq!(
            events
                .iter()
                .map(|r| r["type"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["events.starting", "events.stopped"]
        );
        let payload = &events[1]["payload"];
        assert_eq!(payload, &controller.view().await["events"]);
        for key in [
            "listener_count",
            "subscription_count",
            "events_drained_count",
            "events_auth_failed_count",
            "events_missing_credential_count",
            "events_missing_channel_count",
            "events_error_count",
        ] {
            assert_eq!(payload[key], 0, "{key}");
        }
        assert!(payload["worker_count"].is_null());
        let worker = controller.worker.read().await;
        assert_eq!(
            records[0]["attributes"]["service_run_id"],
            worker.as_ref().unwrap().service_instance_id
        );
        assert!(records.iter().all(
            |r| r["attributes"]["service_run_id"] == records[0]["attributes"]["service_run_id"]
        ));
    }

    #[tokio::test]
    async fn assembly_failure_has_only_scheduler_transitions_with_a_fresh_admitted_id() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        // Populate metadata with a real completed run before assembly fails.
        std::fs::write(
            native
                .workspace
                .join(".guildbotics/config/team/members/aiko/member.yml"),
            r#"{"is_active":false}"#,
        )
        .unwrap();
        let controller = Controller::new(native.clone(), host);
        controller.start().await.unwrap();
        controller.stop(true).await;
        let previous = controller.view().await;
        std::fs::remove_file(
            native
                .workspace
                .join(".guildbotics/config/team/project.yml"),
        )
        .unwrap();
        let bus = Arc::new(crate::events::EventBus::default());
        controller.service.attach_events(bus.clone());
        let initial_events = controller.view().await["events"].clone();
        let mut ids = vec![];
        for _ in 0..2 {
            let offset = bus.snapshot().len();
            assert!(controller.start().await.is_err());
            let records = bus.snapshot();
            let records = &records[offset..];
            assert_eq!(
                records
                    .iter()
                    .map(|r| r["type"].as_str().unwrap())
                    .collect::<Vec<_>>(),
                ["scheduler.starting", "scheduler.failed"]
            );
            let id = records[0]["attributes"]["service_run_id"].as_str().unwrap();
            assert_eq!(id.len(), 32);
            assert_eq!(records[1]["attributes"]["service_run_id"], id);
            let owner_bytes = std::fs::read(native.data.join("run/service.lock")).unwrap();
            let owner: serde_json::Value = serde_json::from_slice(&owner_bytes[1..]).unwrap();
            assert_eq!(owner["service_instance_id"], id);
            let view = controller.view().await;
            assert_eq!(view["events"], initial_events);
            for (key, value) in previous["scheduler"].as_object().unwrap() {
                if ["state", "running", "error", "started_at", "stopped_at"].contains(&key.as_str())
                {
                    continue;
                }
                assert_eq!(&view["scheduler"][key], value, "GET {key}");
                if records[0]["payload"].get(key).is_some() {
                    for record in records {
                        assert_eq!(&record["payload"][key], value, "payload {key}");
                    }
                }
            }
            assert!(
                gb_base::files::FileLock::try_hold(&native.data.join("run/service.lock"))
                    .unwrap()
                    .is_some()
            );
            ids.push(id.to_owned());
        }
        assert_ne!(ids[0], ids[1]);
    }

    #[tokio::test]
    async fn config_read_failures_retain_the_actual_filename() {
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let project = native
            .workspace
            .join(".guildbotics/config/team/project.yml");
        for contents in [None, Some("[invalid")] {
            if let Some(text) = contents {
                std::fs::write(&project, text).unwrap();
            } else {
                std::fs::remove_file(&project).unwrap();
            }
            let error = gb_config::team::Team::load(
                &native.workspace.join(".guildbotics/config"),
                &native.identity_root,
            )
            .unwrap_err();
            assert_eq!(
                error
                    .downcast_ref::<gb_config::ConfigFile>()
                    .unwrap()
                    .path(),
                project.as_path()
            );
        }
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
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            while controller.service.status().await.state != "starting" {
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert!(controller.gate.try_lock().is_err());
        assert!(controller.service.status().await.running);
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
