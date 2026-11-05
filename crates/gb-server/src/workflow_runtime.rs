//! Native workflow operations under the shared execution and record boundary.
use crate::{
    dispatcher::{DispatchFailure, Dispatcher},
    execution::{Admission, Execution, Rejected},
    service::Pending,
    ticket_selector::{self, Runner, Selector},
    work_stops::{self, Subscription, WorkStop},
};
use anyhow::Context;
use chrono::Utc;
use gb_base::shared_writes::SharedWrites;
use gb_member::context::PublicMember;
use gb_observability::{
    Diagnostics, Trace,
    redaction::Redactor,
    runs::{RunRecord, RunState, RunStore},
};
use gb_ports::{ChatService, CodeHostingService, Task, TicketService};
use gb_wire::{CommandReply, WorkflowInvocation};
use serde_json::{Value, json};
use std::{collections::BTreeMap, sync::Arc};
use tokio_util::{sync::CancellationToken, task::TaskTracker};

pub struct Runtime {
    pub generation: Option<Arc<crate::execution::Generation>>,
    pub service_run_id: Option<String>,
    pub dispatcher: Dispatcher,
    pub writes: Arc<SharedWrites>,
    pub redactor: Redactor,
    pub cleanup: TaskTracker,
    pub draining: CancellationToken,
    pub cancelled: CancellationToken,
    pub device_id: String,
    pub person: PublicMember,
    pub roster: Vec<PublicMember>,
    pub channels: Vec<Subscription>,
    pub language: String,
    pub tickets: Option<Arc<dyn TicketService>>,
    pub code: Option<Arc<dyn CodeHostingService>>,
    pub chat: Option<Arc<dyn ChatService>>,
    pub chat_error: Option<gb_ports::ServiceError>,
    pub chat_provider: String,
    identity_services: Vec<(String, String, Arc<dyn ChatService>)>,
    pub reception: Option<crate::chat_receiver::Connection>,
}

#[derive(Debug)]
pub enum Refused {
    Admission(Rejected),
    Dispatch(DispatchFailure),
}
impl gb_observability::Failure for Refused {
    fn error_type(&self) -> String {
        match self {
            Self::Admission(_) => "WorkRejectedError".into(),
            Self::Dispatch(error) => error.error_type(),
        }
    }
}

impl Runtime {
    fn attributes(
        &self,
        mut attributes: serde_json::Map<String, Value>,
    ) -> serde_json::Map<String, Value> {
        if let Some(service_run_id) = &self.service_run_id {
            attributes.insert("service_run_id".into(), json!(service_run_id));
        }
        attributes
    }
    fn traced(&self, trace: Trace, attributes: serde_json::Map<String, Value>) -> Trace {
        self.attributes(attributes).into_iter().fold(
            trace.person(&self.person.person_id),
            |trace, (key, value)| trace.attribute(&key, value),
        )
    }
    pub fn assemble(
        native: &crate::member::native::Native,
        host: Arc<gb_environment::Host>,
        person_id: &str,
        cleanup: TaskTracker,
        draining: CancellationToken,
        cancelled: CancellationToken,
    ) -> anyhow::Result<(Self, Value)> {
        Self::assemble_sources(native, host, person_id, cleanup, draining, cancelled, None)
    }

    pub fn assemble_sources(
        native: &crate::member::native::Native,
        host: Arc<gb_environment::Host>,
        person_id: &str,
        cleanup: TaskTracker,
        draining: CancellationToken,
        cancelled: CancellationToken,
        receive: Option<&mut BTreeMap<String, crate::chat_receiver::Connection>>,
    ) -> anyhow::Result<(Self, Value)> {
        use gb_config::{
            commands::CommandRoots,
            resolved_guest,
            secrets::{DeviceKey, Scope, SecretStore},
            team::Team,
        };
        let roots = CommandRoots {
            config: native.workspace.join(".guildbotics/config"),
            templates: native.package.join("templates"),
        };
        let team = Team::load(&roots.config, &native.identity_root)?;
        let metadata = resolved_guest::member(&roots, &native.identity_root, person_id)?;
        let person = crate::member::native::public(&metadata);
        anyhow::ensure!(
            person.is_active && person.person_type == "agent",
            "inactive or human service member"
        );
        let roster = team
            .members
            .iter()
            .map(|m| {
                resolved_guest::member(&roots, &native.identity_root, &m.person_id)
                    .map(|v| crate::member::native::public(&v))
            })
            .collect::<anyhow::Result<Vec<_>>>()?;
        let project_path = roots.config.join("team/project.yml");
        let project = (|| -> anyhow::Result<Value> {
            gb_config::yaml_value(&std::fs::read_to_string(&project_path)?)
        })()
        .map_err(|error| gb_config::ConfigFile::wrap(project_path, error))?;
        let writes = Arc::new(SharedWrites::new(
            native.identity_root.clone(),
            Some(native.workspace.clone()),
        ));
        let key = || DeviceKey {
            machine_root: native.data.parent().unwrap_or(&native.data).into(),
            keychain: native.keychain.as_ref(),
        };
        let secrets = SecretStore::new(&writes, Scope::Identity, key())
            .context(crate::service_runtime::StartupSecrets)?;
        let workspace_secrets = SecretStore::new(&writes, Scope::Workspace, key())
            .context(crate::service_runtime::StartupSecrets)?;
        let redactor = Redactor::new(
            secrets
                .values()
                .context(crate::service_runtime::StartupSecrets)?
                .into_values()
                .chain(
                    workspace_secrets
                        .values()
                        .context(crate::service_runtime::StartupSecrets)?
                        .into_values(),
                ),
        );
        let config = crate::member::native::integration(
            &native.workspace,
            &native.identity_root,
            &project,
            &person,
            &roster,
        );
        let factory = gb_integrations::ServiceIntegrationFactory::new(
            config.clone(),
            &secrets,
            native.branvil.clone(),
            Arc::new(|_| {}),
        )?;
        let tickets = if config.ticket_provider.is_empty() {
            None
        } else {
            Some(factory.create_ticket_service()?)
        };
        let code = if config.code_provider.is_empty() {
            None
        } else {
            Some(factory.create_code_hosting_service()?)
        };
        let (chat, mut chat_error) = if config.chat_provider.is_empty() {
            (None, None)
        } else {
            match factory.create_chat_service() {
                Ok(chat) => (Some(chat), None),
                Err(error) => (None, Some(error)),
            }
        };
        let channels = work_stops::subscriptions(&metadata);
        let mut identity_services = vec![];
        if !config.chat_provider.is_empty() {
            for member in &roster {
                if member.person_type == "human"
                    || member
                        .accounts
                        .get(&config.chat_provider)
                        .is_some_and(|s| !s.trim().is_empty())
                {
                    continue;
                }
                let other = crate::member::native::integration(
                    &native.workspace,
                    &native.identity_root,
                    &project,
                    member,
                    &roster,
                );
                if let Ok(service) = gb_integrations::ServiceIntegrationFactory::new(
                    other,
                    &secrets,
                    native.branvil.clone(),
                    Arc::new(|_| {}),
                )
                .and_then(|factory| factory.create_chat_service())
                {
                    identity_services.push((
                        member.person_id.clone(),
                        config.chat_provider.clone(),
                        service,
                    ));
                }
            }
        }
        let reception = match receive {
            Some(connections)
                if !channels.is_empty() && !config.chat_provider.is_empty() && chat.is_some() =>
            {
                match factory.listener_key().and_then(|key| {
                    if let Some(connection) = connections.get(&key) {
                        return Ok(connection.clone());
                    }
                    let wake = Arc::new(tokio::sync::Notify::new());
                    let activity = wake.clone();
                    let connection = factory
                        .create_event_listener(Arc::new(move || activity.notify_one()))
                        .map(|listener| crate::chat_receiver::Connection {
                            service: config.chat_provider.clone(),
                            key,
                            listener,
                            wake,
                        })?;
                    connections.insert(connection.key.clone(), connection.clone());
                    Ok(connection)
                }) {
                    Ok(reception) => Some(reception),
                    Err(error) => {
                        chat_error = Some(error);
                        None
                    }
                }
            }
            _ => None,
        };
        let device_id = gb_workspace::identity::ensure_device_identity(&native.data)?.device_id;
        let mut broker = native.clone();
        broker.runtime_work = Some(cleanup.clone());
        let dispatcher = Dispatcher {
            driver: Arc::new(crate::dispatcher::MicroVm),
            host,
            workspace: native.workspace.clone(),
            member: Some(broker.backend()?),
        };
        Ok((
            Self {
                generation: None,
                service_run_id: None,
                dispatcher,
                writes,
                redactor,
                cleanup,
                draining,
                cancelled,
                device_id,
                channels,
                reception,
                person,
                roster,
                language: team.language,
                tickets,
                code,
                chat,
                chat_error,
                chat_provider: config.chat_provider.clone(),
                identity_services,
            },
            metadata,
        ))
    }

    /// Non-ticket commands use the same admission and settlement boundary.
    pub async fn selection_failed(&self, error: gb_ports::ServiceError, source: &str) {
        let trace = self.traced(Trace::open(&self.diagnostics(), source), Default::default());
        let _ = trace
            .run_command(ticket_selector::COMMAND, &self.person.person_id, async {
                Err::<(), _>(DispatchFailure::Service(error))
            })
            .await;
    }

    pub async fn command(
        &self,
        line: &str,
        source: &str,
        slot: BTreeMap<String, String>,
    ) -> Result<CommandReply, Refused> {
        let words = shlex::split(line).unwrap_or_default();
        let name = words.first().cloned().unwrap_or_else(|| line.to_owned());
        if name == ticket_selector::COMMAND {
            let diagnostics = self.diagnostics();
            let selector = Selector {
                diagnostics: &diagnostics,
                person: &self.person.person_id,
                source,
                language: &self.language,
                ticket: self.tickets.as_deref().ok_or(Refused::Dispatch(
                    DispatchFailure::Service(gb_ports::ServiceError::Unsupported),
                ))?,
                code: self.code.as_deref(),
                runner: self,
            };
            let selected = match selector.next().await {
                Ok(selected) => selected,
                Err(error) => {
                    self.selection_failed(error.clone(), source).await;
                    return Err(Refused::Dispatch(DispatchFailure::Service(error)));
                }
            };
            return match selected {
                Some((task, mut invocation)) => {
                    if words.len() > 1 {
                        invocation
                            .payload
                            .insert("command_arguments".into(), json!(&words[1..]));
                    }
                    self.ticket(
                        &task,
                        invocation,
                        slot,
                        ticket_selector::max_attempts(
                            &std::env::var("GUILDBOTICS_TICKET_MAX_ATTEMPTS").unwrap_or_default(),
                        ),
                    )
                    .await
                }
                None => Ok(CommandReply::default()),
            };
        }
        let trace = self.traced(Trace::open(&self.diagnostics(), source), Default::default());
        let invocation:WorkflowInvocation=serde_json::from_value(json!({"command":name,"person_id":self.person.person_id,"source":source,"trigger_type":if source=="scheduled" {"scheduled"} else {"generic"}}))
            .map_err(|_|Refused::Dispatch(DispatchFailure::InvalidCommand))?;
        let command = self
            .dispatcher
            .prepare(line, invocation, trace.id())
            .map(|mut command| {
                if source != "manual" {
                    command.work_kind = line.into();
                }
                command
            });
        let slot = slot_identity(slot);
        let record:RunRecord=serde_json::from_value(json!({"run_id":trace.id(),"work_kind":if source == "manual" { "manual" } else { line },"work_identity":slot,
            "attributes":self.attributes(Default::default()),"execution_mode":if source=="manual" {"user_initiated"} else {"autonomous"},"member_id":self.person.person_id,
            "device_id":self.device_id,"source":source,"started_at":chrono::Utc::now().to_rfc3339()})).expect("host run fields");
        let execution = Execution::enter(
            Admission {
                generation: self.generation.clone(),
                command: name.clone(),
                workspace: self.dispatcher.workspace.clone(),
                writes: self.writes.clone(),
                record,
                redactor: self.redactor.clone(),
                exclusive: command.as_ref().map_or(true, |c| !c.access.read_only),
                record_start: true,
                cleanup: self.cleanup.clone(),
            },
            self.draining.clone(),
        )
        .await
        .map_err(Refused::Admission)?;
        let result = trace
            .run_command(&name, &self.person.person_id, async {
                self.dispatcher
                    .dispatch_prepared(command?, &trace, &self.cancelled)
                    .await
            })
            .await;
        self.finish(execution, &result, source).await?;
        result.map_err(Refused::Dispatch)
    }

    async fn finish(
        &self,
        execution: Execution,
        result: &Result<CommandReply, DispatchFailure>,
        source: &str,
    ) -> Result<(), Refused> {
        let state = match result {
            Ok(_) => RunState::Succeeded,
            Err(DispatchFailure::Cancelled) => RunState::Cancelled,
            Err(error) if error.deferred(source) => RunState::Deferred,
            Err(_) => RunState::Failed,
        };
        execution
            .finish(state)
            .await
            .map_err(|_| Refused::Dispatch(DispatchFailure::Journal))
    }
    pub fn diagnostics(&self) -> Diagnostics {
        Diagnostics::open(&self.dispatcher.workspace)
    }

    /// The scheduler supplies the input slot separately from the conversation
    /// identity. A later feedback event on the same ticket is new work.
    pub async fn manual_ticket(&self) -> Result<(String, CommandReply), Refused> {
        let diagnostics = self.diagnostics();
        let selector =
            Selector {
                diagnostics: &diagnostics,
                person: &self.person.person_id,
                source: "manual",
                language: &self.language,
                ticket: self.tickets.as_deref().ok_or(Refused::Dispatch(
                    DispatchFailure::Service(gb_ports::ServiceError::Unsupported),
                ))?,
                code: self.code.as_deref(),
                runner: self,
            };
        let selected = match selector.next().await {
            Ok(selected) => selected,
            Err(error) => {
                self.selection_failed(error.clone(), "manual").await;
                return Err(Refused::Dispatch(DispatchFailure::Service(error)));
            }
        };
        let Some((task, invocation)) = selected else {
            return Ok((String::new(), CommandReply::default()));
        };
        let trace = self.traced(Trace::open(&diagnostics, "manual"), Default::default());
        let id = trace.id().to_owned();
        let reply = self
            .ticket_traced(
                &task,
                invocation,
                [("request".into(), id.clone())].into(),
                ticket_selector::max_attempts(
                    &std::env::var("GUILDBOTICS_TICKET_MAX_ATTEMPTS").unwrap_or_default(),
                ),
                Some(trace),
            )
            .await?;
        Ok((id, reply))
    }

    pub async fn ticket(
        &self,
        task: &Task,
        invocation: WorkflowInvocation,
        slot: BTreeMap<String, String>,
        attempts: usize,
    ) -> Result<CommandReply, Refused> {
        self.ticket_traced(task, invocation, slot, attempts, None)
            .await
    }
    async fn ticket_traced(
        &self,
        task: &Task,
        invocation: WorkflowInvocation,
        mut slot: BTreeMap<String, String>,
        attempts: usize,
        trace: Option<Trace>,
    ) -> Result<CommandReply, Refused> {
        if invocation.person_id != self.person.person_id
            || invocation.command != ticket_selector::COMMAND
        {
            return Err(Refused::Dispatch(DispatchFailure::InvalidCommand));
        }
        slot = slot_identity(slot);
        slot.insert("ticket_url".into(), task.url.clone().unwrap_or_default());
        slot.insert(
            "trigger_reason".into(),
            task.trigger_reason.clone().unwrap_or_default(),
        );
        let source = serde_json::to_value(invocation.source)
            .unwrap()
            .as_str()
            .unwrap()
            .to_owned();
        let trace = self.traced(
            trace.unwrap_or_else(|| {
                Trace::open(&self.diagnostics(), &source).person(&self.person.person_id)
            }),
            ticket_selector::task_attributes(task),
        );
        let record:RunRecord=serde_json::from_value(json!({"run_id":trace.id(),"work_kind":invocation.work.as_ref().map(|w|w.kind.as_str()).unwrap_or("ticket"),
            "work_identity":slot,"attributes":self.attributes(ticket_selector::task_attributes(task)),"execution_mode":if source=="manual" {"user_initiated"} else {"autonomous"},
            "member_id":self.person.person_id,"device_id":self.device_id,"source":source,"started_at":chrono::Utc::now().to_rfc3339()})).expect("host run fields");
        let execution = Execution::enter(
            Admission {
                generation: self.generation.clone(),
                command: ticket_selector::COMMAND.into(),
                workspace: self.dispatcher.workspace.clone(),
                writes: self.writes.clone(),
                record,
                redactor: self.redactor.clone(),
                exclusive: true,
                record_start: true,
                cleanup: self.cleanup.clone(),
            },
            self.draining.clone(),
        )
        .await
        .map_err(Refused::Admission)?;
        let diagnostics = self.diagnostics();
        let selector =
            Selector {
                diagnostics: &diagnostics,
                person: &self.person.person_id,
                source: &source,
                language: &self.language,
                ticket: self.tickets.as_deref().ok_or(Refused::Dispatch(
                    DispatchFailure::Service(gb_ports::ServiceError::Unsupported),
                ))?,
                code: self.code.as_deref(),
                runner: self,
            };
        let result = trace
            .run_command(
                ticket_selector::COMMAND,
                &self.person.person_id,
                selector.run(task, invocation, &trace, attempts),
            )
            .await;
        self.finish(execution, &result, &source).await?;
        result.map_err(Refused::Dispatch)
    }
}

impl Runner for Runtime {
    fn run<'a>(
        &'a self,
        mut invocation: WorkflowInvocation,
        trace: &'a Trace,
    ) -> Pending<'a, Result<CommandReply, DispatchFailure>> {
        Box::pin(async move {
            let name = invocation.command.clone();
            let arguments = invocation
                .payload
                .remove("command_arguments")
                .map(serde_json::from_value::<Vec<String>>)
                .transpose()
                .map_err(|_| DispatchFailure::InvalidCommand)?
                .unwrap_or_default();
            let mut command = self.dispatcher.prepare(&name, invocation, trace.id())?;
            command.args = arguments;
            self.dispatcher
                .dispatch_prepared(command, trace, &self.cancelled)
                .await
        })
    }
    fn say<'a>(
        &'a self,
        text: &'a str,
        invocation: &'a WorkflowInvocation,
        trace: &'a Trace,
    ) -> Pending<'a, Option<String>> {
        Box::pin(async move {
            let mut command = self
                .dispatcher
                .prepare("functions/talk_as", invocation.clone(), trace.id())
                .ok()?;
            command.args = vec![format!("topic={text}"), "context_location=Ticket".into()];
            command.wants_result = true;
            // A helper is not a second ticket turn and must not inherit its
            // workflow invocation, which owns the completion contract.
            command.invocation = None;
            let reply = self
                .dispatcher
                .dispatch_prepared(command, trace, &self.cancelled)
                .await
                .ok()?;
            spoken(&reply)
        })
    }
    fn evidence(&self, run_id: &str, kind: &str, payload: Value) -> anyhow::Result<()> {
        self.writes.transaction(|writer| {
            let store = RunStore::open(&self.dispatcher.workspace);
            let seed = store
                .read(run_id)?
                .ok_or_else(|| anyhow::anyhow!("run must start before evidence"))?;
            store.append(
                writer,
                seed,
                &json!({"kind":"evidence","evidence_type":kind,"payload":payload}),
                &chrono::Utc::now().to_rfc3339(),
                &self.redactor,
            )?;
            Ok(())
        })
    }
    fn record(&self, run_id: &str) -> anyhow::Result<Option<RunRecord>> {
        RunStore::open(&self.dispatcher.workspace).read(run_id)
    }
    fn activity(&self, trace: &Trace, kind: &str, payload: Value) {
        let attributes = if kind == "workflow.rate_limited" {
            gb_member::events::rate_limited(
                trace.id(),
                "",
                "",
                payload["retry_after_at"].as_str().unwrap_or(""),
                payload["retry_after_text"].as_str().unwrap_or(""),
            )["attributes"]
                .clone()
        } else if kind.starts_with("chat_dispatch.") {
            json!({"chat_dispatch.state":kind.trim_start_matches("chat_dispatch.")})
        } else {
            json!({})
        };
        let mut event = trace.correlation();
        event["type"] = json!(kind);
        event["payload"] = payload;
        event["attributes"]
            .as_object_mut()
            .unwrap()
            .extend(attributes.as_object().unwrap().clone());
        let result =
            self.writes.transaction(|writer| {
                gb_observability::activity::ActivityEvents::open(&self.dispatcher.workspace)
                    .record(writer, &event, &self.redactor)?;
                Ok(())
            });
        if let Err(error) = result {
            tracing::warn!("cannot record workflow activity: {error:#}");
        }
    }
    fn notify<'a>(&'a self, stop: WorkStop, trace: &'a Trace) -> Pending<'a, ()> {
        Box::pin(async move {
            let Some(chat) = self.chat.as_deref() else {
                return;
            };
            work_stops::notify(
                chat,
                &self.person,
                &self.channels,
                &stop,
                &self.language,
                &|payload| {
                    trace.event(
                        "work_stop.notified",
                        serde_json::from_value(payload).unwrap(),
                    );
                },
            )
            .await;
        })
    }
    fn completed<'a>(
        &'a self,
        task: &'a Task,
        run_id: &'a str,
        trace: &'a Trace,
    ) -> Pending<'a, ()> {
        Box::pin(async move {
            match self.record(run_id) {
                Ok(Some(record)) => {
                    if work_stops::suppressed(&record, &self.roster) {
                        let payload = json!({"run_id":run_id,"reason":record.result.as_ref().unwrap().status,"url":task.pull_request_url.as_deref().or(task.url.as_deref()).unwrap_or(""),"waiting_for":record.result.as_ref().unwrap().waiting_for});
                        trace.event(
                            "work_stop.suppressed",
                            serde_json::from_value(payload).unwrap(),
                        );
                    }
                    if let Some(stop) = work_stops::completed_stop(task, &record, &self.roster) {
                        self.notify(stop, trace).await;
                    }
                }
                Err(error) => tracing::warn!("cannot read workflow completion: {error:#}"),
                _ => (),
            }
        })
    }
}

impl crate::chat_dispatch::Runner for Runtime {
    fn stopping(&self) -> bool {
        self.draining.is_cancelled() || self.cancelled.is_cancelled()
    }

    fn selection_failed<'a>(
        &'a self,
        pending: &'a crate::chat_state::Pending,
        error: &'a anyhow::Error,
    ) -> Pending<'a, ()> {
        Box::pin(async move {
            let Ok(trace) = Trace::resume(&self.diagnostics(), "event_queue", &pending.run_id)
            else {
                return;
            };
            let trace = self.traced(
                trace,
                crate::chat_dispatch::attributes(&self.chat_provider, pending),
            );
            let failure = if let Some(error) = error.downcast_ref::<gb_ports::ServiceError>() {
                DispatchFailure::Service(error.clone())
            } else if error
                .downcast_ref::<crate::chat_dispatch::ContextUnavailable>()
                .is_some()
            {
                DispatchFailure::Guest(Box::new(serde_json::from_value(json!({"command":false,"type":"ThreadContextUnavailableError","message":""})).expect("typed failure")))
            } else {
                DispatchFailure::Journal
            };
            let _ = trace
                .run_command(
                    crate::chat_dispatch::COMMAND,
                    &self.person.person_id,
                    async { Err::<(), _>(failure) },
                )
                .await;
        })
    }
    fn roster<'a>(&'a self, fallback: &'a [PublicMember]) -> Pending<'a, Vec<PublicMember>> {
        Box::pin(async move {
            let mut roster = fallback.to_vec();
            for (person, provider, chat) in &self.identity_services {
                if let Ok(identity) = chat.get_bot_identity().await
                    && !identity.user_id.is_empty()
                    && let Some(member) = roster.iter_mut().find(|m| &m.person_id == person)
                {
                    member.accounts.insert(provider.clone(), identity.user_id);
                }
            }
            roster
        })
    }
    fn dispatch<'a>(
        &'a self,
        pending: &'a mut crate::chat_state::Pending,
        invocation: WorkflowInvocation,
        store: &'a crate::chat_state::Store,
    ) -> Pending<'a, Result<RunRecord, crate::chat_dispatch::Failure>> {
        use crate::chat_dispatch::{COMMAND, Failure};
        Box::pin(async move {
            if invocation.person_id != self.person.person_id || invocation.command != COMMAND {
                return Err(Failure::Rejected);
            }
            let service = invocation.payload["subject"]["service"]
                .as_str()
                .ok_or(Failure::Failed)?
                .to_owned();
            let attributes = self.attributes(crate::chat_dispatch::attributes(&service, pending));
            let trace = self.traced(
                Trace::resume(&self.diagnostics(), "event_queue", &pending.run_id)
                    .map_err(|_| Failure::Failed)?,
                attributes.clone(),
            );
            let record:RunRecord=serde_json::from_value(json!({"run_id":pending.run_id,"work_kind":"chat","execution_mode":"autonomous","member_id":self.person.person_id,"device_id":self.device_id,"source":"event_queue",
                "work_identity":{"service":service,"channel_id":pending.event.message.channel_id,"event_id":pending.event.event_id},"attributes":attributes,"started_at":Utc::now().to_rfc3339()})).map_err(|_|Failure::Failed)?;
            let execution = match Execution::enter(
                Admission {
                    generation: self.generation.clone(),
                    command: COMMAND.into(),
                    workspace: self.dispatcher.workspace.clone(),
                    writes: self.writes.clone(),
                    record: record.clone(),
                    redactor: self.redactor.clone(),
                    exclusive: true,
                    record_start: true,
                    cleanup: self.cleanup.clone(),
                },
                self.draining.clone(),
            )
            .await
            {
                Ok(execution) => execution,
                Err(Rejected::Duplicate) => {
                    let record = RunStore::open(&self.dispatcher.workspace)
                        .records()
                        .map_err(|_| Failure::Failed)?
                        .into_iter()
                        .find(|r| {
                            r.member_id == record.member_id
                                && r.work_identity == record.work_identity
                                && r.finished_at.is_some()
                                && r.result.is_some()
                        })
                        .ok_or(Failure::Rejected)?;
                    pending.run_id = record.run_id.clone();
                    return Ok(record);
                }
                Err(_) => return Err(Failure::Rejected),
            };
            pending.attempt_count = pending.attempt_count.saturating_add(1);
            pending.next_attempt_at = None;
            if store
                .save(&service, &self.person.person_id, pending)
                .is_err()
            {
                execution.finish(RunState::Failed).await.ok();
                return Err(Failure::Failed);
            }
            let batch = json!({"event_ids":invocation.payload["prompt"]["event_ids"],"context_cursor":invocation.payload["context_cursor"]});
            if Runner::evidence(self, &pending.run_id, "chat_batch", batch).is_err() {
                execution.finish(RunState::Failed).await.ok();
                return Err(Failure::Failed);
            }
            let result = trace
                .run_command(
                    COMMAND,
                    &self.person.person_id,
                    self.dispatcher
                        .dispatch(COMMAND, invocation, &trace, &self.cancelled),
                )
                .await;
            let state = match &result {
                Ok(_) => RunState::Succeeded,
                Err(DispatchFailure::Cancelled) => RunState::Cancelled,
                Err(error) if error.deferred("event_queue") => RunState::Deferred,
                _ => RunState::Failed,
            };
            execution.finish(state).await.map_err(|_| Failure::Failed)?;
            match result {
                Ok(_) => RunStore::open(&self.dispatcher.workspace)
                    .read(&pending.run_id)
                    .map_err(|_| Failure::Failed)?
                    .ok_or(Failure::Failed),
                Err(DispatchFailure::Cancelled) => Err(Failure::Rejected),
                Err(DispatchFailure::Guest(error))
                    if matches!(
                        error.type_.as_str(),
                        "JudgmentIncomplete" | "ThreadContextUnavailableError"
                    ) =>
                {
                    pending.attempt_count = pending.attempt_count.saturating_sub(1);
                    Err(Failure::Unavailable)
                }
                Err(DispatchFailure::Guest(error)) => {
                    let result = error.cli_agent_result.as_ref();
                    if result
                        .and_then(|v| v.get("error_category"))
                        .and_then(Value::as_str)
                        == Some("rate_limited")
                    {
                        let text = |key: &str| {
                            result
                                .and_then(|v| v.get("error_details"))
                                .and_then(|v| v.get(key))
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_owned()
                        };
                        Err(Failure::RateLimited {
                            at: text("retry_after_at"),
                            hint: text("retry_after_text"),
                        })
                    } else {
                        Err(Failure::Failed)
                    }
                }
                Err(error) if error.deferred("event_queue") => {
                    pending.attempt_count = pending.attempt_count.saturating_sub(1);
                    Err(Failure::Unavailable)
                }
                Err(_) => Err(Failure::Failed),
            }
        })
    }
    fn record(&self, id: &str) -> anyhow::Result<Option<RunRecord>> {
        RunStore::open(&self.dispatcher.workspace).read(id)
    }
    fn completed(
        &self,
        pending: &crate::chat_state::Pending,
        record: &RunRecord,
    ) -> anyhow::Result<()> {
        let Some(result) = &record.result else {
            return Ok(());
        };
        let event = gb_member::events::completed(
            &record.run_id,
            u64::from(pending.attempt_count),
            true,
            serde_json::to_value(result.status)?.as_str().unwrap_or(""),
        );
        let mut attributes = crate::chat_dispatch::attributes(&self.chat_provider, pending);
        attributes.extend(event["attributes"].as_object().unwrap().clone());
        let trace = self
            .traced(
                Trace::resume(&self.diagnostics(), "event_queue", &record.run_id)?,
                attributes,
            )
            .command(crate::chat_dispatch::COMMAND);
        trace.event_once(
            "workflow.completed",
            event["payload"].as_object().unwrap().clone(),
        )?;
        let mut activity = trace.correlation();
        activity["type"] = event["type"].clone();
        activity["payload"] = event["payload"].clone();
        self.writes.transaction(|writer| {
            gb_observability::activity::ActivityEvents::open(&self.dispatcher.workspace)
                .record_once(writer, &activity, &self.redactor)?;
            Ok(())
        })
    }
    fn event(&self, kind: &str, pending: &crate::chat_state::Pending, mut payload: Value) {
        if kind == "chat_dispatch.abandoned" {
            tracing::error!(
                "chat event abandoned after final attempt: run={} person={} command={} event={}",
                pending.run_id,
                self.person.person_id,
                crate::chat_dispatch::COMMAND,
                pending.event.event_id
            );
        } else if kind == "chat_dispatch.retry_scheduled" {
            tracing::warn!(
                "chat event retry scheduled: run={} person={} command={} event={}",
                pending.run_id,
                self.person.person_id,
                crate::chat_dispatch::COMMAND,
                pending.event.event_id
            );
        }
        payload["event_id"] = json!(pending.event.event_id);
        payload["run_id"] = json!(pending.run_id);
        payload["attempt_count"] = json!(pending.attempt_count.min(pending.max_attempts));
        payload["max_attempts"] = json!(pending.max_attempts);
        if let Ok(trace) = Trace::resume(&self.diagnostics(), "event_queue", &pending.run_id) {
            let mut attributes = crate::chat_dispatch::attributes(&self.chat_provider, pending);
            if kind == "workflow.rate_limited" {
                attributes.extend(
                    gb_member::events::rate_limited(
                        trace.id(),
                        "",
                        "",
                        payload["retry_after_at"].as_str().unwrap_or(""),
                        payload["retry_after_text"].as_str().unwrap_or(""),
                    )["attributes"]
                        .as_object()
                        .unwrap()
                        .clone(),
                );
            } else if let Some(state) = kind.strip_prefix("chat_dispatch.") {
                attributes.insert("chat_dispatch.state".into(), json!(state));
            }
            let trace = self
                .traced(trace, attributes)
                .command(crate::chat_dispatch::COMMAND);
            Runner::activity(self, &trace, kind, payload.clone());
            trace.event(kind, serde_json::from_value(payload).unwrap_or_default());
        }
    }
}

fn spoken(reply: &CommandReply) -> Option<String> {
    reply
        .result
        .get("content")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(str::to_owned)
}
fn slot_identity(mut slot: BTreeMap<String, String>) -> BTreeMap<String, String> {
    if let Some(kind) = slot.remove("kind") {
        slot.insert("slot_kind".into(), kind);
    }
    slot
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn agent_only_asking_records_one_diagnostic_without_domain_activity() {
        let root = tempfile::tempdir().unwrap();
        let runtime = crate::test_chat::runtime(root.path());
        let workspace = &runtime.dispatcher.workspace;
        let record: RunRecord = serde_json::from_value(json!({
            "run_id":"asking", "work_kind":"ticket", "execution_mode":"autonomous",
            "member_id":"aiko", "device_id":"device", "status":"succeeded",
            "started_at":"2026-11-05T00:00:00Z", "finished_at":"2026-11-05T00:01:00Z",
            "result":{"subject_type":"ticket", "subject_id":"1", "status":"asking", "waiting_for":["aiko"]}
        })).unwrap();
        runtime
            .writes
            .transaction(|writer| {
                RunStore::open(workspace).start(writer, record, &runtime.redactor)?;
                Ok(())
            })
            .unwrap();
        let trace = Trace::open(&Diagnostics::open(workspace), "test");
        let task: Task = serde_json::from_value(json!({"id":"1", "title":"Question"})).unwrap();
        Runner::completed(&runtime, &task, "asking", &trace).await;
        let transcript = std::fs::read_to_string(workspace.join(format!(
            ".guildbotics/local/run/sessions/{}.jsonl",
            trace.id()
        )))
        .unwrap();
        let diagnostics = transcript
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .filter(|event| event["type"] == "work_stop.suppressed")
            .collect::<Vec<_>>();
        assert_eq!(diagnostics.len(), 1);
        assert_eq!(diagnostics[0]["payload"]["url"], "");
        assert_eq!(diagnostics[0]["payload"]["waiting_for"], json!(["aiko"]));
        assert!(!workspace.join(".guildbotics/state/events").exists());
    }

    #[test]
    fn talk_as_accepts_only_nonempty_trimmed_text() {
        for content in [json!(null), json!(3), json!([]), json!(""), json!(" \n\t")] {
            assert_eq!(
                spoken(&CommandReply {
                    result: json!({"content":content}),
                    ..Default::default()
                }),
                None
            );
        }
        assert_eq!(
            spoken(&CommandReply {
                result: json!({"content":" \nSafe explanation. \n"}),
                ..Default::default()
            }),
            Some("Safe explanation.".into())
        );
    }
}
