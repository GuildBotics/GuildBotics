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
    /// Background commands start in the exchange directory, just like a
    /// manual command without a cwd. A workflow chooses its turn cwd later.
    pub fn prepare(
        &self,
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
            config: self.workspace.join(".guildbotics/config"),
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
        let cwd = self.host.home.join("Documents/GuildBotics");
        gb_base::host_path::open_directory(&cwd, true).map_err(|error| {
            DispatchFailure::Environment(Failure::Command(Sentence::Text(error.to_string())))
        })?;
        invocation.run_id = trace_id.into();
        Ok(Command {
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
        let command = self.prepare(line, invocation, trace.id())?;
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
        let mut reply = self.driver.execute(self, command, cancelled).await?;
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
        let roots = CommandRoots {
            config: self.workspace.join(".guildbotics/config"),
            templates: self.host.package.join("templates"),
        };
        let services = crate::host_window::prepare_command(
            &self.host,
            &self.workspace,
            &command,
            &roots,
            &device,
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
    use serde_json::json;
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

    #[test]
    fn dispatcher_resolves_member_override_once_and_carries_the_callers_work_and_run() {
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
            .unwrap();
        assert_eq!(command.path, selected);
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
                    .is_err()
            );
        }
        assert!(
            dispatcher
                .prepare("functions/work", invocation, "")
                .is_err()
        );
        let chat:WorkflowInvocation=serde_json::from_value(json!({"command":"functions/work","person_id":"aiko","source":"event_queue","trigger_type":"chat",
            "work":{"kind":"chat","identity":"local:C1:T1","chat":{"service":"local","channel_id":"C1","thread_id":"T1","event_id":"E1","self_user_id":"local-aiko"}}})).unwrap();
        let command = dispatcher
            .prepare("functions/work", chat, "chat-trace")
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
