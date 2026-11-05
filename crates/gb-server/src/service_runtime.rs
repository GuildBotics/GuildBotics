//! Assemble native member workers and shared chat reception for a service run.
use crate::{
    service::{Outcome, Pending, Worker},
    service_settings::Settings,
};
use std::{collections::BTreeMap, sync::Arc};
use tokio::sync::OnceCell;
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
        let native = self
            .native
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("config_not_found"))?;
        let settings = Settings::read(&native.workspace)?;
        let (worker, members) = NativeWorker::assemble(
            native,
            self.host.as_ref().unwrap().clone(),
            &settings,
            self.execution.generation(),
        )
        .await?;
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
        if let Some(done) = done {
            done.cancelled().await;
        }
        self.execution.wait(generation).await;
        self.service.status().await
    }
    pub async fn view(&self) -> serde_json::Value {
        let status = self.service.status().await;
        let mut scheduler = serde_json::to_value(&status).expect("service status");
        scheduler["target"] = serde_json::json!("scheduler");
        let worker = self.worker.read().await;
        scheduler["member_routines"] = serde_json::json!(
            worker
                .as_ref()
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
        auth_people.sort();
        auth_people.dedup();
        serde_json::json!({"scheduler":scheduler,"events":{"target":"events",
            "state":if failed {"failed"} else if events_running {status.state} else {"stopped"},
            "running":events_running,"listener_count":listeners,"events_auth_failed_count":auth_count,"events_auth_failed_persons":auth_people,"events_chat_provider":worker.as_ref().map(|w|&w.chat_provider).cloned().unwrap_or_default()},
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
            let (status, code) = if api.workspace.is_none() {
                (axum::http::StatusCode::BAD_REQUEST, "config_not_found")
            } else {
                (
                    axum::http::StatusCode::INTERNAL_SERVER_ERROR,
                    "internal_error",
                )
            };
            crate::api::ApiError::new(status, code, &headers).into_response()
        }
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
        generation: Arc<crate::execution::Generation>,
    ) -> anyhow::Result<(Arc<Self>, Vec<String>)> {
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
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
        starting.abort();
        assert!(starting.await.unwrap_err().is_cancelled());
        drop(publication);
        assert!(controller.start().await.unwrap().running);
        assert!(controller.worker.read().await.is_some());
        assert!(!controller.stop(true).await.running);
    }
}
