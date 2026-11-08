//! Resolve once and dispatch inside one VM. The caller owns the execution
//! lease, run journal, trace boundary and settlement around this operation.
use gb_config::commands::{CommandRoots, command_access};
use gb_environment::{Command, Failure, Host, Sentence};
use gb_observability::Trace;
use gb_wire::{CommandFailure, CommandReply, WorkflowInvocation};
use std::{path::PathBuf, sync::Arc};
use tokio_util::sync::CancellationToken;

#[derive(Debug)]
pub enum DispatchFailure {
    Environment(Failure),
    Guest(Box<CommandFailure>),
    InvalidCommand,
    Cancelled,
    Service(gb_ports::ServiceError),
    Journal,
}
impl DispatchFailure {
    pub fn deferred(&self, source: &str) -> bool {
        source != "manual" && matches!(self, Self::Service(error) if error.is_unavailable())
    }
}
impl gb_observability::Failure for DispatchFailure {
    fn deferred(&self, source: &str) -> bool {
        DispatchFailure::deferred(self, source)
    }
    fn error_type(&self) -> String {
        match self {
            Self::Guest(failure) => failure.type_.clone(),
            Self::Environment(Failure::Runtime(_)) => "AgentRuntimeError".into(),
            Self::Cancelled => "CancelledError".into(),
            Self::Service(error) => if error.is_unavailable() {
                "ServiceUnavailable"
            } else if error.is_not_found() {
                "ServiceNotFound"
            } else if error.is_unsupported() {
                "ServiceUnsupported"
            } else {
                "ServiceError"
            }
            .into(),
            _ => "CommandError".into(),
        }
    }
    fn code(&self) -> &str {
        match self {
            Self::Cancelled => "cancelled",
            Self::Guest(failure)
                if failure
                    .cli_agent_result
                    .as_ref()
                    .and_then(|v| v.get("error_category"))
                    .and_then(serde_json::Value::as_str)
                    == Some("authentication") =>
            {
                "cli_agent_authentication"
            }
            _ => "",
        }
    }
}

pub struct Dispatcher {
    pub keychain: Arc<dyn gb_config::secrets::Keychain>,
    pub driver: Arc<dyn Driver>,
    pub host: Arc<Host>,
    pub workspace: PathBuf,
    pub member: Option<Arc<crate::member::Backend>>,
}

/// Executes a resolved command. Drivers must settle their resources before
/// returning, including when the cancellation token fires.
pub trait Driver: Send + Sync {
    fn execute<'a>(
        &'a self,
        dispatcher: &'a Dispatcher,
        command: Command,
        cancelled: &'a CancellationToken,
    ) -> crate::service::Pending<'a, Result<CommandReply, DispatchFailure>>;
}
pub struct MicroVm;
impl Driver for MicroVm {
    fn execute<'a>(
        &'a self,
        dispatcher: &'a Dispatcher,
        command: Command,
        cancelled: &'a CancellationToken,
    ) -> crate::service::Pending<'a, Result<CommandReply, DispatchFailure>> {
        Box::pin(dispatcher.dispatch_native(command, cancelled))
    }
}

impl Dispatcher {
    /// [`Dispatcher::prepare_in`] a copy of the configuration taken now.
    pub async fn prepare(
        &self,
        line: &str,
        invocation: WorkflowInvocation,
        trace_id: &str,
    ) -> Result<Command, DispatchFailure> {
        let config = gb_environment::Configuration::copied(&self.host, &self.workspace)
            .await
            .map_err(|error| {
                DispatchFailure::Environment(Failure::Command(Sentence::Text(format!("{error:#}"))))
            })?;
        self.prepare_in(Arc::new(config), line, invocation, trace_id)
    }

    /// The command `line` names, as the run reading `config` runs it.
    /// Background commands start in the exchange directory, just like a
    /// manual command without a cwd. A workflow chooses its turn cwd later.
    pub fn prepare_in(
        &self,
        config: Arc<gb_environment::Configuration>,
        line: &str,
        mut invocation: WorkflowInvocation,
        trace_id: &str,
    ) -> Result<Command, DispatchFailure> {
        if trace_id.is_empty() {
            return Err(DispatchFailure::InvalidCommand);
        }
        let mut words = shlex::split(line)
            .ok_or(DispatchFailure::InvalidCommand)?
            .into_iter();
        let name = words.next().ok_or(DispatchFailure::InvalidCommand)?;
        let roots = CommandRoots {
            config: config.path(),
            templates: self.host.package.join("templates"),
        };
        let team = gb_config::team::Team::load(&roots.config, &self.host.identity_root)
            .map_err(|_| DispatchFailure::InvalidCommand)?;
        if team.find(&invocation.person_id).is_none() {
            return Err(DispatchFailure::InvalidCommand);
        }
        let path = roots
            .resolve(&name, &team.language, Some(&invocation.person_id))
            .map_err(|_| DispatchFailure::InvalidCommand)?;
        let access = command_access(&path).map_err(|_| DispatchFailure::InvalidCommand)?;
        let work =
            serde_json::to_value(&invocation.work).map_err(|_| DispatchFailure::InvalidCommand)?;
        let kind = work["kind"].as_str().unwrap_or("manual").to_owned();
        let identity = work["identity"].as_str().unwrap_or(trace_id).to_owned();
        let cwd = self.host.home.join(gb_environment::EXCHANGE);
        gb_base::host_path::open_directory(&cwd, true).map_err(|error| {
            DispatchFailure::Environment(Failure::Command(Sentence::Text(error.to_string())))
        })?;
        invocation.run_id = trace_id.into();
        Ok(Command {
            config,
            wants_result: false,
            person_id: invocation.person_id.clone(),
            run_id: trace_id.into(),
            trace_id: trace_id.into(),
            work_kind: kind,
            work_identity: identity,
            path,
            name,
            access,
            args: words.collect(),
            cwd,
            pipe: String::new(),
            invocation: Some(invocation),
        })
    }

    pub async fn dispatch(
        &self,
        line: &str,
        invocation: WorkflowInvocation,
        trace: &Trace,
        cancelled: &CancellationToken,
    ) -> Result<CommandReply, DispatchFailure> {
        let command = self.prepare(line, invocation, trace.id()).await?;
        self.dispatch_prepared(command, trace, cancelled).await
    }

    pub async fn dispatch_prepared(
        &self,
        command: Command,
        trace: &Trace,
        cancelled: &CancellationToken,
    ) -> Result<CommandReply, DispatchFailure> {
        let name = command.name.clone();
        let person = command.person_id.clone();
        let awake = tokio::select! {
            biased;
            () = cancelled.cancelled() => return Err(DispatchFailure::Cancelled),
            awake = crate::keep_awake::CommandHold::acquire(&self.workspace) => awake,
        };
        let result = self.driver.execute(self, command, cancelled).await;
        awake.finish().await;
        let mut reply = result?;
        trace.command_output(&name, &person, &reply.text_output);
        if let Some(failure) = reply.failure.take() {
            return Err(DispatchFailure::Guest(Box::new(failure)));
        }
        Ok(reply)
    }

    async fn dispatch_native(
        &self,
        command: Command,
        cancelled: &CancellationToken,
    ) -> Result<CommandReply, DispatchFailure> {
        let device = gb_environment::device::read(&self.host, &self.workspace).await;
        if let Some(refusal) = device.refusal.clone() {
            return Err(DispatchFailure::Environment(Failure::Command(refusal)));
        }
        let services = crate::host_window::prepare_command(
            &self.host,
            &self.workspace,
            &command,
            &device,
            self.keychain.clone(),
            self.member.as_deref(),
            #[cfg(test)]
            None,
        )
        .await
        .map_err(|error| DispatchFailure::Environment(Failure::Command(Sentence::Text(error))))?;
        let run = gb_environment::run(
            &self.host,
            &self.workspace,
            command,
            |warning| tracing::warn!("{}", crate::commands::say(warning, "en")),
            &services.session,
        );
        let result = tokio::select! {
            biased;
            ()=cancelled.cancelled()=>Err(DispatchFailure::Cancelled),
            result=run=>result.map_err(DispatchFailure::Environment),
        };
        services.settle().await;
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    struct TestKeys;
    impl gb_config::secrets::Keychain for TestKeys {
        fn read(&self) -> anyhow::Result<Option<String>> {
            Ok(None)
        }
        fn create(&self, _: &str) -> anyhow::Result<()> {
            unreachable!()
        }
    }
    use serde_json::json;
    #[tokio::test]
    async fn dispatcher_entry_holds_during_success_failure_and_cancellation() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        struct Run {
            count: Arc<AtomicUsize>,
            mode: u8,
            entered: CancellationToken,
        }
        impl Driver for Run {
            fn execute<'a>(
                &'a self,
                _: &'a Dispatcher,
                _: Command,
                _: &'a CancellationToken,
            ) -> crate::service::Pending<'a, Result<CommandReply, DispatchFailure>> {
                Box::pin(async move {
                    self.entered.cancel();
                    assert_eq!(self.count.load(Ordering::SeqCst), 1);
                    match self.mode {
                        0 => Ok(serde_json::from_value(json!({"text_output":"done"})).unwrap()),
                        1 => Err(DispatchFailure::Journal),
                        _ => std::future::pending().await,
                    }
                })
            }
        }
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let count = Arc::new(AtomicUsize::new(0));
        let _factory = crate::keep_awake::test_count(&native.workspace, count.clone());
        for mode in 0..3 {
            let entered = CancellationToken::new();
            let dispatcher = Dispatcher {
                keychain: native.keychain.clone(),
                driver: Arc::new(Run {
                    count: count.clone(),
                    mode,
                    entered: entered.clone(),
                }),
                host: Arc::new(Host {
                    data: native.data.clone(),
                    home: root.path().join("home"),
                    package: native.package.clone(),
                    identity_root: native.identity_root.clone(),
                }),
                workspace: native.workspace.clone(),
                member: None,
            };
            let trace = Trace::open(
                &gb_observability::Diagnostics::open(&native.workspace),
                "manual",
            );
            let invocation = serde_json::from_value(json!({"command":"workflows/chat_conversation_workflow","person_id":"aiko","source":"manual","trigger_type":"generic","payload":{},"work":{"kind":"generic","identity":"test"}})).unwrap();
            let command = dispatcher
                .prepare(
                    "workflows/chat_conversation_workflow",
                    invocation,
                    trace.id(),
                )
                .await
                .unwrap();
            let task = tokio::spawn(async move {
                dispatcher
                    .dispatch_prepared(command, &trace, &CancellationToken::new())
                    .await
            });
            entered.cancelled().await;
            if mode == 2 {
                task.abort();
            }
            let result = task.await;
            if mode == 2 {
                assert!(result.unwrap_err().is_cancelled());
            } else {
                assert_eq!(result.unwrap().is_err(), mode == 1);
            }
            crate::keep_awake::wait_released(&count).await;
        }
    }
    #[test]
    fn wrapped_cli_authentication_and_explicit_cancellation_keep_their_alert_codes() {
        use gb_observability::Failure;
        for category in ["authentication", "network", "rate_limited"] {
            let failure=DispatchFailure::Guest(Box::new(serde_json::from_value(json!({"command":true,"type":"CommandError","message":"wrapped","cli_agent":"codex","cli_agent_result":{"error_category":category}})).unwrap()));
            assert_eq!(
                failure.code(),
                if category == "authentication" {
                    "cli_agent_authentication"
                } else {
                    ""
                }
            );
        }
        assert_eq!(DispatchFailure::Cancelled.code(), "cancelled");
    }

    #[tokio::test]
    async fn dispatcher_resolves_member_override_once_and_carries_the_callers_work_and_run() {
        let root = tempfile::tempdir().unwrap();
        let workspace = root.path().join("workspace");
        let identity = root.path().join("identity");
        let config = workspace.join(".guildbotics/config");
        let templates = root.path().join("package/templates");
        for path in [
            config.join("team/members/aiko"),
            identity.join(".guildbotics/config/team/members/aiko"),
            templates.join("commands/functions"),
            config.join("team/members/aiko/commands/functions"),
        ] {
            std::fs::create_dir_all(path).unwrap();
        }
        std::fs::write(
            config.join("team/project.yml"),
            "name: test\nlanguage: ja\n",
        )
        .unwrap();
        std::fs::write(
            config.join("team/members/aiko/member.yml"),
            "is_active: true\n",
        )
        .unwrap();
        std::fs::write(
            identity.join(".guildbotics/config/team/members/aiko/identity.yml"),
            "person_id: aiko\nname: Aiko\nperson_type: agent\n",
        )
        .unwrap();
        let selected = config.join("team/members/aiko/commands/functions/work.py");
        std::fs::write(&selected, "async def main(context):\n    return 'member'\n").unwrap();
        std::fs::write(
            templates.join("commands/functions/work.py"),
            "async def main(context):\n    return 'template'\n",
        )
        .unwrap();
        let dispatcher = Dispatcher {
            keychain: Arc::new(TestKeys),
            driver: Arc::new(MicroVm),
            host: Arc::new(Host {
                data: root.path().join("data"),
                home: root.path().join("home"),
                package: root.path().join("package"),
                identity_root: identity,
            }),
            workspace,
            member: None,
        };
        let invocation:WorkflowInvocation=serde_json::from_value(json!({"command":"functions/work","person_id":"aiko","source":"routine","trigger_type":"ticket","payload":{"ticket_url":"local://ticket/90"},"work":{"kind":"ticket_mention","identity":"local://ticket/90"}})).unwrap();
        let command = dispatcher
            .prepare(
                "functions/work 'message=two words'",
                invocation.clone(),
                "trace",
            )
            .await
            .unwrap();
        // The member's own file, in the run's copy of the configuration.
        assert_eq!(
            command.config.original(&command.path),
            Some(gb_base::host_path::normalize(&selected).unwrap())
        );
        assert_eq!(command.args, vec!["message=two words"]);
        assert_eq!(command.work_kind, "ticket_mention");
        assert_eq!(command.work_identity, "local://ticket/90");
        assert_eq!(command.run_id, command.trace_id);
        assert_eq!(command.invocation.unwrap().run_id, "trace");
        assert_eq!(
            command.cwd,
            dispatcher.host.home.join("Documents/GuildBotics")
        );
        for line in ["", "  ", "functions/work '", "missing"] {
            assert!(
                dispatcher
                    .prepare(line, invocation.clone(), "trace")
                    .await
                    .is_err()
            );
        }
        assert!(
            dispatcher
                .prepare("functions/work", invocation, "")
                .await
                .is_err()
        );
        let chat:WorkflowInvocation=serde_json::from_value(json!({"command":"functions/work","person_id":"aiko","source":"event_queue","trigger_type":"chat",
            "work":{"kind":"chat","identity":"local:C1:T1","chat":{"service":"local","channel_id":"C1","thread_id":"T1","event_id":"E1","self_user_id":"local-aiko"}}})).unwrap();
        let command = dispatcher
            .prepare("functions/work", chat, "chat-trace")
            .await
            .unwrap();
        let broker = crate::host_window::broker_invocation(&command);
        assert_eq!(broker.person_id, "aiko");
        assert_eq!(broker.run_id, "chat-trace");
        let subject = broker.chat_subject.unwrap();
        assert_eq!(subject.service, "local");
        assert_eq!(subject.channel_id, "C1");
        assert_eq!(subject.thread_id, "T1");
        assert_eq!(subject.event_id, "E1");
        assert_eq!(subject.self_user_id, "local-aiko");
    }
}
