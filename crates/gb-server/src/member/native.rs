//! Assembly above member capabilities, providers, records, and the VM channel.
use super::{Backend, Execution, Executor, MemberRequest, Phase, Result as Reply};
use gb_base::shared_writes::SharedWrites;
use gb_config::{
    commands::CommandRoots,
    secrets::{DeviceKey, Keychain, Scope, SecretStore, SystemKeychain},
};
use gb_integrations::{IntegrationConfig, ServiceIntegrationFactory};
use gb_member::{
    MemberError, command,
    context::PublicMember,
    control::{Identity, Recipients, RunControl},
    repository::{Repository, RepositoryServices},
    session::Session,
};
use gb_observability::{
    redaction::Redactor,
    runs::{RunRecord, RunStore},
};
use gb_ports::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::{Arc, OnceLock},
    time::{Duration, Instant},
};

#[derive(Clone)]
pub struct Native {
    pub runtime_work: Option<tokio_util::task::TaskTracker>,
    pub chat_subject: Option<gb_member::chat_updates::Subject>,
    pub workspace: PathBuf,
    pub data: PathBuf,
    pub identity_root: PathBuf,
    pub package: PathBuf,
    pub branvil: Option<branvil::api::DataApi>,
    pub invocation: Option<gb_environment::broker::Invocation>,
    pub guest: Option<gb_environment::guest_process::Channel>,
    pub keychain: Arc<dyn Keychain>,
}
/// Compare the executable builds, even when they share a package version.
pub fn binary_version() -> anyhow::Result<String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    static VERSION: OnceLock<String> = OnceLock::new();
    if let Some(version) = VERSION.get() {
        return Ok(version.clone());
    }
    let mut file = std::fs::File::open(std::env::current_exe()?)?;
    let mut hasher = Sha256::new();
    let mut bytes = [0; 65536];
    loop {
        let count = file.read(&mut bytes)?;
        if count == 0 {
            break;
        }
        hasher.update(&bytes[..count]);
    }
    let hash = hasher
        .finalize()
        .iter()
        .map(|v| format!("{v:02x}"))
        .collect::<String>();
    let version = format!("{}+{hash}", env!("CARGO_PKG_VERSION"));
    let _ = VERSION.set(version.clone());
    Ok(version)
}
impl Native {
    pub fn new(workspace: PathBuf, data: PathBuf) -> anyhow::Result<Self> {
        Ok(Self {
            runtime_work: None,
            chat_subject: None,
            workspace,
            data,
            identity_root: gb_base::home::identity_root()?,
            package: gb_base::home::python_package(),
            branvil: None,
            invocation: None,
            guest: None,
            keychain: Arc::new(SystemKeychain),
        })
    }
    pub fn backend(self) -> anyhow::Result<Arc<Backend>> {
        let workspace_id =
            gb_workspace::identity::ensure_workspace_identity(&self.workspace)?.workspace_id;
        Ok(Arc::new(Backend {
            version: binary_version()?,
            workspace_id,
            executor: Arc::new(self),
            timeout: Duration::from_secs(300),
        }))
    }
    async fn invoke(
        &self,
        request: MemberRequest,
        incoming: Option<&Path>,
        outgoing: &Path,
    ) -> anyhow::Result<Reply> {
        let deadline = Instant::now() + Duration::from_secs(300);
        let mut request = request;
        let parsed = command::parse(&request.arguments)?;
        if self.invocation.as_ref().is_some_and(|v| v.access.read_only)
            && !parsed.read_only
            && !parsed.help
        {
            return Err(MemberError::Capability("cli.member.lease.invalid_delegation").into());
        }
        if parsed.help || parsed.command == "help" {
            return Ok(Reply {
                stdout: if parsed.help {
                    command::usage(&parsed.command)
                } else {
                    gb_member::context::help().into()
                },
                ..Default::default()
            });
        }
        if request.phase.is_none() {
            parsed.content(&request.stdin)?;
        }
        let roots = CommandRoots {
            config: self.workspace.join(".guildbotics/config"),
            templates: self.package.join("templates"),
        };
        let team = gb_config::team::Team::load(&roots.config, &self.identity_root)?;
        let selected = self
            .invocation
            .as_ref()
            .map(|v| v.person_id.as_str())
            .unwrap_or(parsed.text("person"));
        let person = team
            .find(selected)
            .ok_or(MemberError::Capability("cli.member.person_mismatch"))?;
        // Rewrite only the person option value, never a title or content value.
        let mut index = 0;
        while index < request.arguments.len() {
            let arg = &request.arguments[index];
            if arg == "--person" {
                request.arguments[index + 1] = person.person_id.clone();
                break;
            }
            if arg.starts_with("--person=") {
                request.arguments[index] = format!("--person={}", person.person_id);
                break;
            }
            if index
                >= request
                    .arguments
                    .iter()
                    .position(|v| v.starts_with('-'))
                    .unwrap_or(request.arguments.len())
                && arg.starts_with('-')
                && !arg.contains('=')
            {
                let spec = command::grammar().get(&parsed.command).and_then(|g| {
                    g.options
                        .iter()
                        .find(|s| s.spelling.split('/').any(|v| v == arg))
                });
                index += if spec.is_some_and(|s| !s.flag) { 2 } else { 1 };
            } else {
                index += 1;
            }
        }
        let metadata =
            gb_config::resolved_guest::member(&roots, &self.identity_root, &person.person_id)?;
        let roster = team
            .members
            .iter()
            .map(|m| {
                gb_config::resolved_guest::member(&roots, &self.identity_root, &m.person_id)
                    .map(|v| public(&v))
            })
            .collect::<anyhow::Result<Vec<_>>>()?;
        let public_person = public(&metadata);
        if person.person_type != "agent" {
            return Err(MemberError::Capability("cli.member.human_subject").into());
        }
        let project = yaml(&roots.config.join("team/project.yml"))?;
        let writes = SharedWrites::new(self.identity_root.clone(), Some(self.workspace.clone()));
        let secrets = SecretStore::new(
            &writes,
            Scope::Identity,
            DeviceKey {
                machine_root: self.data.parent().unwrap_or(&self.data).into(),
                keychain: self.keychain.as_ref(),
            },
        )?;
        let mut values = secrets.values()?.into_values().collect::<Vec<_>>();
        let workspace_secrets = SecretStore::new(
            &writes,
            Scope::Workspace,
            DeviceKey {
                machine_root: self.data.parent().unwrap_or(&self.data).into(),
                keychain: self.keychain.as_ref(),
            },
        )?;
        values.extend(workspace_secrets.values()?.into_values());
        let masks = Redactor::new(values.clone());
        let memory_masks = Redactor::with_min_length(values, 4);
        let config = integration(
            &self.workspace,
            &self.identity_root,
            &project,
            &public_person,
            &roster,
        );
        let factory = ServiceIntegrationFactory::new(
            config.clone(),
            &secrets,
            self.branvil.clone(),
            Arc::new(|_| {}),
        )?;
        let services = Services {
            factory,
            code: OnceLock::new(),
            tickets: OnceLock::new(),
            chat: OnceLock::new(),
            code_configured: !config.code_provider.is_empty(),
        };
        let lifecycle = if self.invocation.is_none()
            && (request.phase.is_none()
                || matches!(request.phase, Some(Phase::Push))
                || matches!(request.phase, Some(Phase::Record)) && parsed.command == "git commit")
        {
            Lifecycle::start(
                &writes,
                &self.workspace,
                &person.person_id,
                &request,
                &parsed.command,
                &masks,
                !matches!(request.phase, Some(Phase::Push)),
            )
        } else {
            None
        };
        let target_attributes = std::sync::Mutex::new(BTreeMap::new());
        let result: anyhow::Result<Reply> = async {
        let peer = Peers {
            config: &config,
            secrets: &secrets,
            branvil: self.branvil.clone(),
            roster: &roster,
            eligible: team.members.iter().filter(|m| gb_config::resolved_guest::member(&roots,&self.identity_root,&m.person_id).ok().is_some_and(|v|v["routine_commands"].as_array().is_some_and(|a|a.iter().any(|x|x=="workflows/ticket_driven_workflow")))).map(|m|m.person_id.clone()).collect(),
        };
        let invocation = self.invocation.as_ref();

        let run_id = invocation.map(|v| v.run_id.as_str()).unwrap_or("");
        let kind = invocation
            .map(|v| v.work_kind.as_str())
            .unwrap_or("command");
        let work_identity = invocation.map(|v| v.work_identity.as_str()).unwrap_or("");
        let trace = invocation.map(|v| v.trace_id.as_str()).unwrap_or_else(||lifecycle.as_ref().map(|v|v.session.trace_id.as_str()).unwrap_or(""));
        let subject_type=if self.chat_subject.is_some(){"chat"}else if kind=="chat"{"chat_thread"}else{kind};
        let chat_now=||chrono::Utc::now().timestamp_millis() as f64 / 1000.0;
        let chat_render=|key:&str|crate::i18n::t(&team.language,key,&[]);
        let guard=gb_member::chat_updates::Gate{workspace:&self.workspace,writes:&writes,person:&person.person_id,run_id,subject:self.chat_subject.as_ref(),redactor:&masks,now:&chat_now,render:&chat_render};
        let actors = roster
            .iter()
            .map(|v| {
                (
                    v.person_id.clone(),
                    ActorName(
                        v.accounts
                            .get(if config.ticket_provider == "branvil" {
                                "branvil"
                            } else {
                                "local"
                            })
                            .cloned()
                            .unwrap_or_else(|| {
                                if config.ticket_provider == "local" {
                                    format!("local-{}", v.person_id)
                                } else {
                                    String::new()
                                }
                            }),
                    ),
                )
            })
            .collect();
        let repository = Repository {
            services: &services,
            guard: &guard,
            roster: &actors,
        };
        let correlation = json!({"trace_id":trace,"run_id":run_id,"person_id":person.person_id,"source":"member"});
        if invocation.is_some_and(|v|!v.access.read_only) && !run_id.is_empty() && RunStore::open(&self.workspace).read(run_id)?.is_none() {
            let device = gb_workspace::identity::ensure_device_identity(&self.data)?;
            let record: RunRecord = serde_json::from_value(
                json!({"run_id":run_id,"work_kind":kind,"work_identity":{"kind":kind,"identity":work_identity},"execution_mode":"autonomous","member_id":person.person_id,"device_id":device.device_id,"source":"member","started_at":chrono::Utc::now().to_rfc3339()}),
            )?;
            writes.transaction(|writer| {
                RunStore::open(&self.workspace).start(writer, record, &masks)?;
                Ok(())
            })?;
        }
        let observer = |command: &str, read_only: bool, payload: &Value| -> gb_member::Result<()> {
            let mut events = vec![];
            if payload["target"].is_object() {
                let mut target = payload["target"].clone();
                target["provider"] = if target["kind"] == "ticket" {
                    config.ticket_provider.as_str()
                } else {
                    config.code_provider.as_str()
                }
                .into();
                let event=gb_member::events::inspected(&target, read_only);
                *target_attributes.lock().unwrap()=serde_json::from_value(event["attributes"].clone()).unwrap_or_default();
                events.push(event);
            }
            match command {
                "pr create" => events.extend(gb_member::events::pr_created(payload)),
                "ticket create" => events.push(gb_member::events::target(
                    &payload["target"],
                    "opened",
                    None,
                )),
                "ticket update" => events.extend(gb_member::events::ticket_closed(payload)),
                "ticket comment" => events.push(gb_member::events::ticket_commented(payload)),
                "git push" | "git publish" => {
                    events.extend(gb_member::events::pushed(payload, &config.code_provider))
                }
                _ => {}
            }
            if events.is_empty(){return Ok(());}
            writes
                .transaction(|writer| {
                    for event in events {
                        gb_member::events::record(
                            &self.workspace,
                            writer,
                            event,
                            &correlation,
                            &masks,
                        )?;
                    }
                    Ok(())
                })
                .map_err(|_|MemberError::Capability("capabilities.task_runs.store_failed"))
        };
        let render_wait = |_: &Readiness| {
            crate::i18n::t(&team.language, "integrations.pull_request_patrol.awaiting_checks", &[])
        };
        let subject_id=self.chat_subject.as_ref().map(|s|s.id()).unwrap_or_else(||work_identity.into());
        let control = RunControl {
            chat:Some(&guard),
            identity: Identity {
                run_id,
                person_id: &person.person_id,
                work_kind: kind,
                work_identity,
                subject_type,
                subject_id: &subject_id,
                subject_url: if matches!(kind, "ticket" | "ticket_mention" | "pull_request" | "pull_request_review") {
                    work_identity
                } else {
                    ""
                },
            },
            workspace: &self.workspace,
            writes: &writes,
            repository: &repository,
            roster: &roster,
            recipients: &peer,
            redactor: &masks,
            observe: &observer,
            render_wait: &render_wait,
            render_notice: &chat_render,
        };
        let memory = if parsed.command=="context" || parsed.command.starts_with("memory "){Some(gb_member::memory::Memory::open(
            &writes,
            &self.workspace,
            &person.person_id,
            &self.data,
            !run_id.is_empty(),
            &memory_masks,
            correlation.clone(),
        )?)}else{None};
        let providers = [
            config.code_provider.clone(),
            config.ticket_provider.clone(),
            config.chat_provider.clone(),
        ];
        let context = if parsed.command=="context" {gb_member::context::member_context(
            &public_person,
            &roster,
            &providers,
            memory.as_ref().expect("context memory").context()?,
            if parsed.command == "context" && !config.code_provider.is_empty() {
                Some(services.code()?)
            } else {
                None
            },
            false,
        )
        .await?}else{json!({})};
        let guest = Guest {
            channel: self.guest.as_ref(),
            language: &team.language,
            deadline,
        };
        let git_root = self
            .workspace
            .join(".guildbotics/local/member_git")
            .join(&person.person_id);
        let fallback_email = format!("{}@guildbotics.local",person.person_id);
        let render_git_error = |error: &MemberError| {
            error.render(|key, args| crate::i18n::t(&team.language, key, &args.iter().map(|(key, value)| (key.as_str(), value.as_str())).collect::<Vec<_>>()))
        };
        let git = if parsed.command.starts_with("git ") || request.phase.is_some() {
            Some(gb_member::git::Workspace {
                host_root: git_root.clone(),
                cwd:invocation.map(|v|v.cwd.clone()).unwrap_or_else(||self.workspace.clone()),
                clones: self
                    .workspace
                    .join(".guildbotics/local/clones")
                    .join(&person.person_id),
                work: invocation.map(|_| (kind, work_identity)),
                guest: &guest,
                services: &services,
                guard: &guard,
                name: metadata["account_info"]["git_user"]
                    .as_str()
                    .filter(|v| !v.is_empty())
                    .unwrap_or(&person.name),
                email: metadata["account_info"]["git_email"]
                    .as_str()
                    .filter(|v| !v.is_empty())
                    .unwrap_or(&fallback_email),
                deadline,
                render_error: &render_git_error,
            })
        } else {
            None
        };
        if let Some(phase) = request.phase {
            if !matches!(
                parsed.command.as_str(),
                "git commit" | "git push" | "git publish"
            ) || parsed.text("workspace-mode") != "current"
                || invocation.is_some()
            {
                return Err(MemberError::Capability("cli.member.current_requires_exchange").into());
            }
            let bytes = incoming.map(std::fs::read).transpose()?;
            let phase = match phase {
                Phase::Identity => "identity",
                Phase::Record => "record",
                Phase::Known => "known",
                Phase::Push => "push",
                Phase::Tracking => "tracking",
            };
            let (payload, bytes) = git.as_ref().ok_or(MemberError::Capability("capabilities.member_git.work_required"))?
                .client_git_with(phase, Value::Object(request.data), bytes.as_deref(),&||{if let Some(lifecycle)=&lifecycle{lifecycle.begin();}})
                .await?;
            if !bytes.is_empty() {
                tokio::fs::write(outgoing, &bytes).await?;
            }
            if phase == "push" || phase == "record" && parsed.command == "git commit" {
                use gb_member::session::Control;
                control.record(
                    if phase == "push" {
                        "git push"
                    } else {
                        "git commit"
                    },
                    false,
                    &payload,
                )?;
            }
            return Ok(Reply {
                stdout: serde_json::to_string(&payload)?,
                ..Default::default()
            });
        }
        let chat = if parsed.command=="context" && parsed.flag("check-credentials") || matches!(parsed.command.as_str(),"chat identity"|"chat resolve-channel"|"chat inspect thread"|"chat inspect channel"|"chat post"|"chat reply"|"chat reaction add") {
            (if parsed.command=="context"{services.chat().ok()}else{Some(services.chat()?)}).map(|service|gb_member::chat::Chat {
                service,
                service_name: &config.chat_provider,
                person_id: &person.person_id,
                guard: &guard,
                participant_labels: roster
                    .iter()
                    .map(|v| (v.person_id.clone(), v.name.clone()))
                    .collect(),
            })
        } else {
            None
        };
        let session = Session {
            person_id: &person.person_id,
            person_type: &person.person_type,
            delegated_to: invocation
                .filter(|v| !v.access.read_only)
                .map(|v| v.person_id.as_str()),
            workflow: invocation.is_some(),
            repository: Repository {
                services: &services,
                guard: &guard,
                roster: &actors,
            },
            chat,
            memory,
            git,
            context,
            control: &control,
            git_root: &git_root,
        };
        let executed = session.execute(&request.arguments, &request.stdin).await;
        let answer = executed?;
        let stdout = answer.text.unwrap_or_else(|| {
            if answer.format == "markdown" {
                markdown(&answer.payload,&team.language)
            } else {
                serde_json::to_string(&answer.payload).expect("member JSON")
            }
        });
        let mut reply = Reply {
            stdout: format!("{stdout}\n"),
            ..Default::default()
        };
        if let Some(bytes) = answer.download {
            tokio::fs::write(outgoing, &bytes).await?;
            reply.metadata.insert(
                "download".into(),
                answer.download_metadata.unwrap_or(Value::Null),
            );
        }
        Ok(reply)
        }.await;
        let result = match (result, services.close().await) {
            (Ok(value), Ok(())) => Ok(value),
            (Err(error), _) => Err(error),
            (_, Err(error)) => Err(error.into()),
        };
        if let Some(lifecycle) = &lifecycle {
            lifecycle.finish(
                if result.is_ok() {
                    "succeeded"
                } else {
                    "failed"
                },
                target_attributes.into_inner().unwrap(),
            );
        }
        result.map_err(|error| match error.downcast::<MemberError>() {
            Ok(MemberError::CapabilityArgs { key, mut args }) => {
                for value in args.values_mut() {
                    *value = masks.text(value);
                }
                MemberError::CapabilityArgs { key, args }.into()
            }
            Ok(error) => error.into(),
            Err(error) => error,
        })
    }
}
impl Executor for Native {
    fn tracker(&self) -> Option<tokio_util::task::TaskTracker> {
        self.runtime_work.clone()
    }
    fn broker(
        &self,
        invocation: gb_environment::broker::Invocation,
        guest: gb_environment::guest_process::Channel,
    ) -> Option<gb_environment::broker::Handler> {
        let mut native = self.clone();
        native.chat_subject =
            invocation
                .chat_subject
                .as_ref()
                .map(|s| gb_member::chat_updates::Subject {
                    service: s.service.clone(),
                    channel_id: s.channel_id.clone(),
                    thread_id: s.thread_id.clone(),
                    event_id: s.event_id.clone(),
                    self_user_id: s.self_user_id.clone(),
                });
        native.invocation = Some(invocation);
        native.guest = Some(guest);
        Some(Arc::new(move |arguments, stdin, invocation| {
            let native = native.clone();
            Box::pin(async move {
                let temporary = match tempfile::tempdir() {
                    Ok(temporary) => temporary,
                    Err(_) => {
                        return gb_environment::broker::Result {
                            exit_code: 1,
                            stdout: String::new(),
                            stderr: crate::i18n::t("en", "cli.member.execution_failed", &[]),
                        };
                    }
                };
                let outgoing = temporary.path().join("download");
                let reply = native
                    .execute(
                        MemberRequest {
                            version: String::new(),
                            workspace_id: String::new(),
                            arguments,
                            stdin,
                            interactive_host: String::new(),
                            interactive_thread_key: String::new(),
                            phase: None,
                            data: Default::default(),
                        },
                        false,
                        None,
                        &outgoing,
                    )
                    .await;
                if reply.exit_code == 0
                    && let Some(download) = reply.metadata.get("download")
                {
                    let mut argv = vec![
                        "python".into(),
                        "-m".into(),
                        "guildbotics.guest.artifact_archive".into(),
                        download["destination"].as_str().unwrap_or(".").into(),
                    ];
                    if let Some(values) = download["attachment"].as_array() {
                        argv.extend(values.iter().filter_map(Value::as_str).map(str::to_owned));
                    }
                    let bytes = match tokio::fs::read(outgoing).await {
                        Ok(bytes) => bytes,
                        Err(_) => {
                            return gb_environment::broker::Result {
                                exit_code: 1,
                                stdout: String::new(),
                                stderr: crate::i18n::t("en", "cli.member.execution_failed", &[]),
                            };
                        }
                    };
                    let cwd = match gb_environment::guest_path(&invocation.cwd) {
                        Ok(cwd) => cwd,
                        Err(_) => return broker_failed(),
                    };
                    let result = native
                        .guest
                        .as_ref()
                        .expect("bound guest channel")
                        .execute(gb_environment::guest_process::Process {
                            argv,
                            cwd,
                            env: vec![],
                            stdin: bytes,
                            limit: 10 * 1024 * 1024,
                        })
                        .await;
                    return match result {
                        Ok(result) => gb_environment::broker::Result {
                            exit_code: result.code,
                            stdout: String::from_utf8_lossy(&result.stdout).into(),
                            stderr: String::from_utf8_lossy(&result.stderr).into(),
                        },
                        Err(_) => gb_environment::broker::Result {
                            exit_code: 1,
                            stdout: String::new(),
                            stderr: crate::i18n::t("en", "cli.member.execution_failed", &[]),
                        },
                    };
                }
                gb_environment::broker::Result {
                    exit_code: reply.exit_code,
                    stdout: reply.stdout,
                    stderr: reply.stderr,
                }
            })
        }))
    }
    fn execute<'a>(
        &'a self,
        request: MemberRequest,
        describe: bool,
        incoming: Option<&'a Path>,
        outgoing: &'a Path,
    ) -> Execution<'a> {
        Box::pin(async move {
            let language = yaml(&self.workspace.join(".guildbotics/config/team/project.yml"))
                .ok()
                .and_then(|v| v["language"].as_str().map(str::to_owned))
                .unwrap_or_else(|| "en".into());
            if describe {
                return match command::parse(&request.arguments) {
                    Ok(parsed) => {
                        let plan = if parsed.help {
                            json!({"result":{"exit_code":0,"stdout":command::usage(&parsed.command),"stderr":""}})
                        } else {
                            parsed.plan()
                        };
                        Reply {
                            metadata: [("plan".into(), plan)].into_iter().collect(),
                            ..Default::default()
                        }
                    }
                    Err(error) => Reply {
                        exit_code: 2,
                        stderr: error.render(|key, args| {
                            let params = args
                                .iter()
                                .map(|(k, v)| (k.as_str(), v.as_str()))
                                .collect::<Vec<_>>();
                            crate::i18n::t(&language, key, &params)
                        }),
                        ..Default::default()
                    },
                };
            }
            let native = self.clone();
            let incoming = incoming.map(Path::to_owned);
            let outgoing = outgoing.to_owned();
            let runtime = tokio::runtime::Handle::current();
            let result = tokio::task::spawn_blocking(move || {
                runtime.block_on(native.invoke(request, incoming.as_deref(), &outgoing))
            })
            .await
            .unwrap_or_else(|_| Err(anyhow::anyhow!("member worker failed")));
            match result {
                Ok(reply) => reply,
                Err(error) => {
                    let message = if let Some(member) = error.downcast_ref::<MemberError>() {
                        member.render(|key, args| {
                            crate::i18n::t(
                                &language,
                                key,
                                &args
                                    .iter()
                                    .map(|(k, v)| (k.as_str(), v.as_str()))
                                    .collect::<Vec<_>>(),
                            )
                        })
                    } else if let Some(service) = error.downcast_ref::<ServiceError>() {
                        crate::i18n::t(
                            &language,
                            service.translation_key(),
                            &service
                                .translation_args()
                                .iter()
                                .map(|(k, v)| (k.as_str(), v.as_str()))
                                .collect::<Vec<_>>(),
                        )
                    } else {
                        crate::i18n::t(&language, "cli.member.execution_failed", &[])
                    };
                    Reply {
                        exit_code: error
                            .downcast_ref::<MemberError>()
                            .map(MemberError::exit_code)
                            .unwrap_or(1),
                        stderr: if error.downcast_ref::<MemberError>().is_some()
                            || error.downcast_ref::<ServiceError>().is_some()
                        {
                            format!("Error: {message}\n")
                        } else {
                            message
                        },
                        ..Default::default()
                    }
                }
            }
        })
    }
}
struct Lifecycle<'a> {
    writes: &'a SharedWrites,
    workspace: &'a Path,
    masks: &'a Redactor,
    session: gb_observability::interactive::InteractiveSession,
    command: String,
    state: std::sync::Mutex<(bool, bool)>,
}
impl<'a> Lifecycle<'a> {
    fn start(
        writes: &'a SharedWrites,
        workspace: &'a Path,
        person: &str,
        request: &MemberRequest,
        command: &str,
        masks: &'a Redactor,
        begin: bool,
    ) -> Option<Self> {
        let sessions = gb_observability::interactive::InteractiveSessions::open(workspace);
        let session = writes
            .transaction(|writer| {
                sessions.start_or_touch(
                    writer,
                    person,
                    &request.interactive_host,
                    &request.interactive_thread_key,
                    chrono::Utc::now(),
                    chrono::Duration::minutes(30),
                )
            })
            .ok()?;
        let result = Self {
            writes,
            workspace,
            masks,
            session,
            command: format!("member {command}"),
            state: std::sync::Mutex::new((false, false)),
        };
        if begin {
            result.begin();
        }
        Some(result)
    }
    fn write(&self, status: &str, attributes: BTreeMap<String, String>) {
        let _ = self.writes.transaction(|writer| {
            gb_member::events::record(self.workspace,writer,json!({"type":if status=="running"{"member.command.started"}else if status=="succeeded"{"member.command.finished"}else{"member.command.failed"},"payload":{"command":self.command},"attributes":attributes}),&json!({"trace_id":self.session.trace_id,"person_id":self.session.person_id,"source":"interactive"}),self.masks)?;
            gb_observability::interactive::InteractiveSessions::open(self.workspace).record(
                writer,
                &self.session,
                gb_observability::interactive::SessionCommand {
                    command: &self.command,
                    status,
                    attributes,
                    now: &chrono::Utc::now().to_rfc3339(),
                },
                self.masks,
            )?;
            Ok(())
        });
    }
    fn begin(&self) {
        let mut state = self.state.lock().unwrap();
        if !state.0 {
            self.write("running", BTreeMap::new());
            state.0 = true;
        }
    }
    fn finish(&self, status: &str, attributes: BTreeMap<String, String>) {
        let mut state = self.state.lock().unwrap();
        if state.0 && !state.1 {
            self.write(status, attributes);
            state.1 = true;
        }
    }
}
impl Drop for Lifecycle<'_> {
    fn drop(&mut self) {
        self.finish("failed", BTreeMap::new());
    }
}

fn broker_failed() -> gb_environment::broker::Result {
    gb_environment::broker::Result {
        exit_code: 1,
        stdout: String::new(),
        stderr: crate::i18n::t("en", "cli.member.execution_failed", &[]),
    }
}
fn yaml(path: &Path) -> anyhow::Result<Value> {
    gb_config::yaml_value(&std::fs::read_to_string(path)?)
        .map_err(|_| anyhow::anyhow!("invalid member configuration"))
}
pub(crate) fn public(member: &Value) -> PublicMember {
    let text = |key: &str| member[key].as_str().unwrap_or("").to_owned();
    let accounts = [("branvil", "branvil_username"), ("slack", "slack_user_id")]
        .into_iter()
        .filter_map(|(service, key)| {
            member["account_info"][key]
                .as_str()
                .map(|v| (service.into(), v.into()))
        })
        .collect();
    PublicMember {
        person_id: text("person_id"),
        name: text("name"),
        person_type: text("person_type"),
        is_active: member["is_active"].as_bool().unwrap_or(true),
        roles: member["roles"].clone(),
        profile: member["profile"].clone(),
        speaking_style: text("speaking_style"),
        accounts,
    }
}
pub(crate) fn integration(
    workspace: &Path,
    identity: &Path,
    project: &Value,
    person: &PublicMember,
    roster: &[PublicMember],
) -> IntegrationConfig {
    let services = &project["services"];
    let name = |kind: &str| services[kind]["name"].as_str().unwrap_or("").to_lowercase();
    let scope = |kind: &str| {
        services[kind][if name(kind) == "branvil" {
            "workspace"
        } else {
            "owner"
        }]
        .as_str()
        .unwrap_or("")
        .to_owned()
    };
    IntegrationConfig {
        local_root: workspace.join(".guildbotics/local/services"),
        identity_root: identity.into(),
        person_id: person.person_id.clone(),
        display_name: person.name.clone(),
        accounts: person
            .accounts
            .iter()
            .map(|(k, v)| (k.clone(), ActorName(v.clone())))
            .collect(),
        branvil_members: roster
            .iter()
            .filter_map(|v| {
                v.accounts
                    .get("branvil")
                    .map(|a| (a.clone(), v.name.clone()))
            })
            .collect(),
        ticket_provider: name("ticket_service"),
        code_provider: name("code_hosting_service"),
        chat_provider: name("chat_service"),
        ticket_scope: scope("ticket_service"),
        code_scope: scope("code_hosting_service"),
        checks: services["code_hosting_service"]["checks"]
            .as_str()
            .unwrap_or("")
            .into(),
        slack_base_url: services["chat_service"]["base_url"]
            .as_str()
            .unwrap_or("")
            .into(),
    }
}
struct Services<'a> {
    code_configured: bool,
    factory: ServiceIntegrationFactory<'a>,
    code: OnceLock<std::result::Result<Arc<dyn CodeHostingService>, ServiceError>>,
    tickets: OnceLock<std::result::Result<Arc<dyn TicketService>, ServiceError>>,
    chat: OnceLock<std::result::Result<Arc<dyn ChatService>, ServiceError>>,
}
impl RepositoryServices for Services<'_> {
    fn code_configured(&self) -> bool {
        self.code_configured
    }
    fn code(&self) -> gb_member::Result<&dyn CodeHostingService> {
        self.code
            .get_or_init(|| self.factory.create_code_hosting_service())
            .as_ref()
            .map(|v| v.as_ref())
            .map_err(|e| e.clone().into())
    }
    fn tickets(&self) -> gb_member::Result<&dyn TicketService> {
        self.tickets
            .get_or_init(|| self.factory.create_ticket_service())
            .as_ref()
            .map(|v| v.as_ref())
            .map_err(|e| e.clone().into())
    }
}
impl Services<'_> {
    async fn close(&self) -> gb_member::Result<()> {
        let mut error = None;
        if let Some(Ok(code)) = self.code.get()
            && let Err(e) = code.aclose().await
        {
            error = Some(e);
        }
        if let Some(Ok(tickets)) = self.tickets.get()
            && let Err(e) = tickets.aclose().await
        {
            error.get_or_insert(e);
        }
        match error {
            Some(error) => Err(error.into()),
            None => Ok(()),
        }
    }
    fn chat(&self) -> gb_member::Result<&dyn ChatService> {
        self.chat
            .get_or_init(|| self.factory.create_chat_service())
            .as_ref()
            .map(|v| v.as_ref())
            .map_err(|e| e.clone().into())
    }
}
struct Guest<'a> {
    language: &'a str,
    channel: Option<&'a gb_environment::guest_process::Channel>,
    deadline: Instant,
}
impl gb_member::git::Guest for Guest<'_> {
    fn path(&self, path: &Path) -> gb_member::Result<String> {
        gb_environment::guest_path(path)
            .map_err(|_| MemberError::Capability("capabilities.member_git.invalid_path"))
    }
    fn run<'a>(
        &'a self,
        cwd: &'a Path,
        argv: Vec<String>,
        env: BTreeMap<String, String>,
        stdin: Vec<u8>,
        limit: usize,
    ) -> gb_member::git::Pending<'a, gb_member::git::Output> {
        Box::pin(async move {
            let channel = self.channel.ok_or(MemberError::Capability(
                "capabilities.member_git.work_required",
            ))?;
            let result = channel
                .execute_until(
                    gb_environment::guest_process::Process {
                        argv,
                        cwd: gb_environment::guest_path(cwd).map_err(|_| {
                            MemberError::Capability("capabilities.member_git.invalid_path")
                        })?,
                        env: env.into_iter().collect(),
                        stdin,
                        limit,
                    },
                    self.deadline,
                )
                .await
                .map_err(|error| MemberError::CapabilityArgs {
                    key: "capabilities.member_git.git_failed_reason",
                    args: [(
                        "reason".into(),
                        crate::i18n::say(error.sentence(), self.language),
                    )]
                    .into(),
                })?;
            Ok(gb_member::git::Output {
                code: result.code,
                stdout: result.stdout,
                stderr: result.stderr,
            })
        })
    }
}
struct Peers<'a> {
    config: &'a IntegrationConfig,
    secrets: &'a SecretStore<'a>,
    branvil: Option<branvil::api::DataApi>,
    roster: &'a [PublicMember],
    eligible: std::collections::BTreeSet<String>,
}
impl Recipients for Peers<'_> {
    fn receives<'a>(
        &'a self,
        person: &'a str,
        subject: &'a str,
    ) -> gb_member::git::Pending<'a, bool> {
        Box::pin(async move {
            if !self.eligible.contains(person) {
                return Ok(false);
            }
            let person = self
                .roster
                .iter()
                .find(|v| v.person_id == person)
                .ok_or(ServiceError::InvalidParameters)?;
            let mut config = self.config.clone();
            config.person_id = person.person_id.clone();
            config.display_name = person.name.clone();
            config.accounts = person
                .accounts
                .iter()
                .map(|(k, v)| (k.clone(), ActorName(v.clone())))
                .collect();
            let factory = ServiceIntegrationFactory::new(
                config,
                self.secrets,
                self.branvil.clone(),
                Arc::new(|_| {}),
            )?;
            let result = if self.config.code_provider.is_empty() {
                Err(ServiceError::NotFound)
            } else {
                let code = factory.create_code_hosting_service()?;
                let result = code.can_receive_work(subject).await;
                code.aclose().await?;
                result
            };
            match result {
                Ok(value) => Ok(value),
                Err(error) if error.is_not_found() => {
                    let tickets = factory.create_ticket_service()?;
                    let result = tickets.can_receive_work(subject).await;
                    tickets.aclose().await?;
                    match result {
                        Err(error) if error.is_not_found() => Ok(false),
                        result => Ok(result?),
                    }
                }
                Err(error) => Err(error.into()),
            }
        })
    }
}
fn markdown(value: &Value, language: &str) -> String {
    gb_member::output::markdown(value, |key| crate::i18n::t(language, key, &[]))
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::sync::Mutex;
    use tower::ServiceExt;
    #[derive(Default)]
    struct Keys(Mutex<Option<String>>);
    impl Keychain for Keys {
        fn read(&self) -> anyhow::Result<Option<String>> {
            Ok(self.0.lock().unwrap().clone())
        }
        fn create(&self, value: &str) -> anyhow::Result<()> {
            *self.0.lock().unwrap() = Some(value.into());
            Ok(())
        }
    }
    pub(crate) fn fixture(root: &Path) -> Native {
        let workspace = root.join("workspace");
        let identity = root.join("identity");
        let data = root.join("machine/data");
        for (path, value) in [
            (
                workspace.join(".guildbotics/config/team/project.yml"),
                json!({"name":"fixture","language":"en","services":{"ticket_service":{"name":"local","owner":"acme"},"code_hosting_service":{"name":"local","owner":"acme"},"chat_service":{"name":"local"}}}),
            ),
            (
                workspace.join(".guildbotics/config/team/members/aiko/member.yml"),
                json!({"is_active":true,"routine_commands":["workflows/ticket_driven_workflow"]}),
            ),
            (
                identity.join(".guildbotics/config/team/members/aiko/identity.yml"),
                json!({"person_id":"aiko","name":"Aiko","person_type":"agent","account_info":{"git_user":"Fixture Member","git_email":"fixture@example.invalid"},"profile":{}}),
            ),
        ] {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, serde_json::to_vec(&value).unwrap()).unwrap();
        }
        Native {
            runtime_work: None,
            chat_subject: None,
            workspace,
            data,
            identity_root: identity,
            package: gb_base::home::python_package(),
            branvil: None,
            invocation: None,
            guest: None,
            keychain: Arc::new(Keys::default()),
        }
    }
    fn request(args: &[&str], stdin: &str) -> MemberRequest {
        MemberRequest {
            version: "fixture".into(),
            workspace_id: "workspace".into(),
            arguments: args.iter().map(|v| (*v).into()).collect(),
            stdin: stdin.into(),
            interactive_host: "fixture-client".into(),
            interactive_thread_key: "thread".into(),
            phase: None,
            data: Default::default(),
        }
    }
    #[tokio::test]
    async fn scheduler_http_owns_native_workers_refuses_unauthorized_start_and_releases_the_lock_on_stop()
     {
        let root = tempfile::tempdir().unwrap();
        let native = fixture(root.path());
        let workspace = native.workspace.clone();
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let controller = crate::service_runtime::Controller::new(native.clone(), host.clone());
        let api = Arc::new(crate::api::Api::new(
            crate::api::Selected {
                workspace: Some(workspace.clone()),
                service: controller,
                ..Default::default()
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            crate::auth::ServerAuth::open(&native.data, 8765).unwrap(),
            "fixture".into(),
            native.data.clone(),
        ));
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let app = crate::api::router(api.clone());
        for (method, path, body, authorized, expected_running, status) in [
            ("GET", "/scheduler/status", "", true, false, 200),
            ("POST", "/scheduler/start", "{}", false, false, 401),
            ("POST", "/scheduler/start", "{}", true, true, 200),
            ("POST", "/scheduler/start", "{}", true, true, 200),
            (
                "POST",
                "/scheduler/stop",
                "{\"force\":true}",
                true,
                false,
                200,
            ),
            ("POST", "/scheduler/stop", "", true, false, 200),
        ] {
            let mut request = axum::http::Request::builder()
                .method(method)
                .uri(format!("/-/guildbotics/api{path}"))
                .header("host", "127.0.0.1:8765")
                .header("content-type", "application/json");
            if authorized {
                request = request.header("authorization", format!("Bearer {}", token.trim()));
            }
            let response = app
                .clone()
                .oneshot(request.body(axum::body::Body::from(body)).unwrap())
                .await
                .unwrap();
            assert_eq!(response.status().as_u16(), status, "{path}");
            if status == 200 {
                let bytes = axum::body::to_bytes(response.into_body(), 65536)
                    .await
                    .unwrap();
                let value: Value = serde_json::from_slice(&bytes).unwrap();
                assert_eq!(value["scheduler"]["running"], expected_running);
                assert_eq!(value["scheduler"]["target"], "scheduler");
                assert_eq!(value["events"]["target"], "events");
                assert_eq!(value["has_active_work"], expected_running);
                assert_eq!(
                    value["scheduler"]["worker_count"],
                    if expected_running {
                        json!(1)
                    } else if path == "/scheduler/status" {
                        Value::Null
                    } else {
                        json!(0)
                    }
                );
            }
        }
        let response = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/-/guildbotics/api/commands/run")
                    .header("host", "127.0.0.1:8765")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {}", token.trim()))
                    .body(axum::body::Body::from(
                        json!({"command":"workflows/ticket_driven_workflow","person":"aiko"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::OK);
        let bytes = axum::body::to_bytes(response.into_body(), 65536)
            .await
            .unwrap();
        let value: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(value["trace_id"], "");
        assert_eq!(value["output"], "");
        assert!(RunStore::open(&workspace).records().unwrap().is_empty());
        let lease = gb_base::files::FileLock::try_hold(
            &workspace.join(".guildbotics/local/run/person-leases/aiko.lock"),
        )
        .unwrap()
        .unwrap();
        for draining in [false, true] {
            let generation = draining.then(|| api.selected().service.execution.begin_drain(false));
            let response = app.clone().oneshot(axum::http::Request::builder().method("POST").uri("/-/guildbotics/api/commands/run")
                .header("host", "127.0.0.1:8765").header("content-type", "application/json")
                .header("authorization", format!("Bearer {}", token.trim()))
                .body(axum::body::Body::from(json!({"command":"workflows/chat_conversation_workflow","person":"aiko"}).to_string())).unwrap()).await.unwrap();
            assert_eq!(response.status(), axum::http::StatusCode::CONFLICT);
            let bytes = axum::body::to_bytes(response.into_body(), 65536)
                .await
                .unwrap();
            let value: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(value["code"], "work_rejected");
            if let Some(generation) = generation {
                api.selected().service.execution.wait(generation).await;
            }
        }
        drop(lease);
        api.work.close();
        api.work.wait().await;
        assert!(RunStore::open(&workspace).records().unwrap().is_empty());
        let second = crate::service_runtime::Controller::new(native, host);
        assert!(second.start().await.unwrap().running);
        assert!(!second.stop(true).await.running);
        let project_path = workspace.join(".guildbotics/config/team/project.yml");
        let mut project: Value =
            serde_json::from_slice(&std::fs::read(&project_path).unwrap()).unwrap();
        project["services"]
            .as_object_mut()
            .unwrap()
            .remove("chat_service");
        std::fs::write(project_path, project.to_string()).unwrap();
        let member_path = workspace.join(".guildbotics/config/team/members/aiko/member.yml");
        std::fs::write(member_path, json!({"is_active":true,"message_channels":[{"name":"general","chat":{"channel_id":"C1"}}]}).to_string()).unwrap();
        let project_path = workspace.join(".guildbotics/config/team/project.yml");
        project["services"]["chat_service"] = json!({"name":"slack"});
        std::fs::write(&project_path, project.to_string()).unwrap();
        assert!(api.selected().service.start().await.unwrap().running);
        let response = app
            .clone()
            .oneshot(
                axum::http::Request::builder()
                    .uri("/-/guildbotics/api/scheduler/status")
                    .header("host", "127.0.0.1:8765")
                    .header("authorization", format!("Bearer {}", token.trim()))
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::OK);
        let bytes = axum::body::to_bytes(response.into_body(), 65536)
            .await
            .unwrap();
        let view: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(view["events"]["events_auth_failed_count"], 0);
        assert_eq!(view["events"]["events_auth_failed_persons"], json!([]));
        assert_eq!(view["events"]["events_missing_credential_count"], 1);
        assert_eq!(
            view["events"]["events_missing_credential_persons"],
            json!(["aiko"])
        );
        assert_eq!(view["events"]["events_chat_provider"], "slack");
        assert!(!api.selected().service.stop(true).await.running);
        let stopped = api.selected().service.view().await;
        assert_eq!(stopped["events"]["events_missing_credential_count"], 0);
        assert_eq!(
            stopped["events"]["events_missing_credential_persons"],
            json!([])
        );
        project["services"]["chat_service"] = json!({"name":"local"});
        std::fs::write(&project_path, project.to_string()).unwrap();
        assert!(api.selected().service.start().await.unwrap().running);
        assert_eq!(
            api.selected().service.view().await["events"]["listener_count"],
            1
        );
        assert!(!api.selected().service.stop(true).await.running);
        assert_eq!(
            api.selected().service.view().await["events"]["listener_count"],
            0
        );
        project["services"]
            .as_object_mut()
            .unwrap()
            .remove("chat_service");
        std::fs::write(&project_path, project.to_string()).unwrap();
        assert!(api.selected().service.start().await.unwrap().running);
        assert_eq!(
            api.selected().service.view().await["events"]["listener_count"],
            0
        );
        assert!(!api.selected().service.stop(true).await.running);
        crate::service_settings::Settings::write(
            &crate::service_settings::Settings::default(),
            &workspace,
        )
        .unwrap();
        std::fs::write(
            crate::service_settings::Settings::path(&workspace),
            "bad: [",
        )
        .unwrap();
        let response = app
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/-/guildbotics/api/scheduler/start")
                    .header("host", "127.0.0.1:8765")
                    .header("authorization", format!("Bearer {}", token.trim()))
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::BAD_REQUEST);
        assert!(
            gb_base::files::FileLock::try_hold(&api.data.join("run/service.lock"))
                .unwrap()
                .is_some()
        );
        let bytes = axum::body::to_bytes(response.into_body(), 65536)
            .await
            .unwrap();
        let value: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(value["code"], "service_settings_invalid");
    }
    #[tokio::test]
    async fn workflow_runtime_assembles_local_ports_and_idle_ticket_command_leaves_no_run() {
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        for (id, kind) in [("kenji", "agent"), ("owner", "human")] {
            let identity = native.identity_root.join(format!(
                ".guildbotics/config/team/members/{id}/identity.yml"
            ));
            let member = native
                .workspace
                .join(format!(".guildbotics/config/team/members/{id}/member.yml"));
            std::fs::create_dir_all(identity.parent().unwrap()).unwrap();
            std::fs::create_dir_all(member.parent().unwrap()).unwrap();
            std::fs::write(
                identity,
                json!({"person_id":id,"name":id,"person_type":kind}).to_string(),
            )
            .unwrap();
            std::fs::write(member, json!({"is_active":false}).to_string()).unwrap();
        }

        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: temp.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let cleanup = tokio_util::task::TaskTracker::new();
        let (runtime, metadata) = crate::workflow_runtime::Runtime::assemble(
            &native,
            host,
            "aiko",
            cleanup.clone(),
            Default::default(),
            Default::default(),
        )
        .unwrap();
        assert_eq!(
            metadata["routine_commands"],
            json!(["workflows/ticket_driven_workflow"])
        );
        assert!(runtime.tickets.is_some());
        assert!(runtime.code.is_some());
        assert!(runtime.chat.is_some());
        let roster = crate::chat_dispatch::Runner::roster(&runtime, &runtime.roster).await;
        assert_eq!(
            roster
                .iter()
                .find(|m| m.person_id == "aiko")
                .unwrap()
                .accounts["local"],
            "local-aiko"
        );
        assert_eq!(
            roster
                .iter()
                .find(|m| m.person_id == "kenji")
                .unwrap()
                .accounts["local"],
            "local-kenji"
        );
        assert!(
            !roster
                .iter()
                .find(|m| m.person_id == "owner")
                .unwrap()
                .accounts
                .contains_key("local")
        );
        runtime
            .command(
                "workflows/ticket_driven_workflow",
                "routine",
                BTreeMap::new(),
            )
            .await
            .unwrap();
        assert!(
            RunStore::open(&native.workspace)
                .records()
                .unwrap()
                .is_empty()
        );
        assert!(
            !native
                .workspace
                .join(".guildbotics/local/run/diagnostics.jsonl")
                .exists()
        );
        cleanup.close();
        cleanup.wait().await;
    }
    struct MemberTurn {
        chat_action: Mutex<&'static str>,
        completion: Mutex<&'static str>,
        after_completion: Mutex<Option<crate::dispatcher::DispatchFailure>>,
        before_completion: Mutex<Option<crate::dispatcher::DispatchFailure>>,
        generation: Arc<crate::execution::Generation>,
        arguments: Mutex<Vec<Vec<String>>>,
        failures: Mutex<std::collections::VecDeque<crate::dispatcher::DispatchFailure>>,
        tickets: Arc<dyn gb_ports::TicketService>,
        native: Native,
        calls: Mutex<Vec<gb_wire::WorkflowInvocation>>,
        /// Each command run, by its file and what the file held as it ran.
        ran: Mutex<Vec<(PathBuf, String)>>,
    }
    impl crate::dispatcher::Driver for MemberTurn {
        fn execute<'a>(
            &'a self,
            _: &'a crate::dispatcher::Dispatcher,
            command: gb_environment::Command,
            _: &'a tokio_util::sync::CancellationToken,
        ) -> crate::service::Pending<
            'a,
            Result<gb_wire::CommandReply, crate::dispatcher::DispatchFailure>,
        > {
            Box::pin(async move {
                self.ran.lock().unwrap().push((
                    command.path.clone(),
                    std::fs::read_to_string(&command.path).unwrap_or_default(),
                ));
                if command.name == "functions/talk_as" && command.wants_result {
                    assert!(command.invocation.is_none());
                    assert!(command.access.read_only);
                    assert!(command.files.is_none());
                    assert_eq!(command.cwd, self.native.data.join("run/empty"));
                    assert!(command.pipe.is_empty());
                    assert_eq!(command.args.len(), 2);
                    assert_eq!(command.args[1], "context_location=Ticket");
                    return Ok(gb_wire::CommandReply {
                        result: json!({"content":" \nSafe failure explanation.\n "}),
                        ..Default::default()
                    });
                }
                let invocation = command.invocation.clone().unwrap();
                let snapshot = self.generation.snapshot();
                assert_eq!(snapshot.len(), 1);
                assert_eq!(snapshot[0]["id"], command.run_id);
                assert_eq!(snapshot[0]["person_id"], command.person_id);
                assert_eq!(
                    snapshot[0]["source"],
                    serde_json::to_value(invocation.source).unwrap()
                );
                let record = RunStore::open(&self.native.workspace)
                    .read(&command.run_id)
                    .unwrap()
                    .unwrap();
                assert_eq!(record.attributes["service_run_id"], self.generation.id);
                self.arguments.lock().unwrap().push(command.args.clone());
                self.calls.lock().unwrap().push(invocation.clone());
                if let Some(error) = self.failures.lock().unwrap().pop_front() {
                    return Err(error);
                }
                let mut native = self.native.clone();
                let bound = crate::host_window::broker_invocation(&command);
                native.chat_subject = bound.chat_subject.as_ref().map(|subject| {
                    serde_json::from_value(serde_json::to_value(subject).unwrap()).unwrap()
                });
                native.invocation = Some(bound);
                let steps = if command.work_kind == "chat" {
                    let channel = invocation.payload["subject"]["channel_id"]
                        .as_str()
                        .unwrap();
                    let thread = invocation.payload["subject"]["thread_id"].as_str().unwrap();
                    let message = invocation.payload["message_id"].as_str().unwrap();
                    let action = *self.chat_action.lock().unwrap();
                    let visible = match action {
                        "noop" => (
                            vec!["chat", "noop", "--person", "aiko", "--content-stdin"],
                            "No response needed.",
                        ),
                        "reaction" => (
                            vec![
                                "chat",
                                "reaction",
                                "add",
                                "--person",
                                "aiko",
                                "--channel-id",
                                channel,
                                "--message-id",
                                message,
                                "--reaction",
                                "ack",
                            ],
                            "",
                        ),
                        _ => (
                            vec![
                                "chat",
                                "reply",
                                "--person",
                                "aiko",
                                "--channel-id",
                                channel,
                                "--thread-id",
                                thread,
                                "--content-stdin",
                            ],
                            "Handled.",
                        ),
                    };
                    vec![
                        (vec!["chat", "updates", "--person", "aiko"], ""),
                        (
                            vec![
                                "chat",
                                "inspect",
                                "thread",
                                "--person",
                                "aiko",
                                "--channel-id",
                                channel,
                                "--thread-id",
                                thread,
                            ],
                            "",
                        ),
                        visible,
                        (vec!["chat", "updates", "--person", "aiko"], ""),
                        (
                            vec![
                                "chat",
                                "complete",
                                "--person",
                                "aiko",
                                "--status",
                                "done",
                                "--content-stdin",
                            ],
                            "Completed.",
                        ),
                    ]
                } else {
                    let url = invocation.payload["task"]["url"].as_str().unwrap();
                    let item = self
                        .tickets
                        .read(gb_ports::Resource::Tickets, url, None, "")
                        .await
                        .unwrap();
                    assert_eq!(
                        item.items()[0]["lane"],
                        if command.work_kind == "ticket_mention" {
                            invocation.payload["task"]["status"].as_str().unwrap()
                        } else {
                            "in_progress"
                        }
                    );
                    if command.work_kind == "ticket_mention" {
                        assert_eq!(item.items()[0]["assignees"], json!(["local-yuki"]));
                    }
                    vec![
                        (
                            vec![
                                "ticket",
                                "comment",
                                "--person",
                                "aiko",
                                "--url",
                                url,
                                "--content-stdin",
                            ],
                            "Handled.",
                        ),
                        (
                            vec![
                                "task",
                                "complete",
                                "--person",
                                "aiko",
                                "--status",
                                "done",
                                "--content-stdin",
                            ],
                            "Completed.",
                        ),
                    ]
                };
                let expected_pr = if command.work_kind == "ticket" {
                    let url = invocation.payload["task"]["url"].as_str().unwrap();
                    let output = native.workspace.join("fixture-output");
                    let read = native
                        .execute(
                            request(
                                &[
                                    "ticket",
                                    "read",
                                    "--person",
                                    "aiko",
                                    "--resource",
                                    "ticket_pull_requests",
                                    "--subject",
                                    url,
                                ],
                                "",
                            ),
                            false,
                            None,
                            &output,
                        )
                        .await;
                    assert_eq!(read.exit_code, 0, "{}", read.stderr);
                    let page: Value = serde_json::from_str(&read.stdout).unwrap();
                    page["items"]
                        .as_array()
                        .unwrap()
                        .first()
                        .and_then(|item| item["url"].as_str())
                        .map(str::to_owned)
                } else {
                    None
                };
                let mut steps = steps;
                if expected_pr.is_some() && command.work_kind != "ticket_mention" {
                    let url = invocation.payload["task"]["url"].as_str().unwrap();
                    steps.insert(
                        0,
                        (
                            vec![
                                "pr",
                                "create",
                                "--person",
                                "aiko",
                                "--repo",
                                "acme/demo",
                                "--head",
                                "feature",
                                "--base",
                                "main",
                                "--title",
                                "Fix login",
                                "--issue-url",
                                url,
                                "--content-stdin",
                            ],
                            "Reuse the requested PR.",
                        ),
                    );
                }
                let output = native.workspace.join("fixture-output");
                let completion = *self.completion.lock().unwrap();
                for (mut args, content) in steps {
                    if args.get(1) == Some(&"complete")
                        && let Some(index) = args.iter().position(|arg| *arg == "--status")
                    {
                        args[index + 1] = completion;
                    }
                    if args.get(1) == Some(&"complete")
                        && let Some(error) = self.before_completion.lock().unwrap().take()
                    {
                        return Err(error);
                    }
                    let reply = native
                        .execute(request(&args, content), false, None, &output)
                        .await;
                    assert_eq!(reply.exit_code, 0, "{} {}", reply.stdout, reply.stderr);
                    if args[..2] == ["pr", "create"] {
                        let value: Value = serde_json::from_str(&reply.stdout).unwrap();
                        assert_eq!(value["created"], false);
                        assert_eq!(value["target"]["url"], expected_pr.as_deref().unwrap());
                    }
                }
                if let Some(error) = self.after_completion.lock().unwrap().take() {
                    return Err(error);
                }
                Ok(gb_wire::CommandReply {
                    text_output: "done".into(),
                    ..Default::default()
                })
            })
        }
    }
    #[tokio::test]
    async fn failure_speech_dispatches_with_the_original_run_still_held_or_finished() {
        use crate::ticket_selector::Runner;
        for language in ["en", "ja"] {
            let root = tempfile::tempdir().unwrap();
            let native = fixture(root.path());
            let project_path = native
                .workspace
                .join(".guildbotics/config/team/project.yml");
            let mut project: Value =
                serde_json::from_slice(&std::fs::read(&project_path).unwrap()).unwrap();
            project["language"] = json!(language);
            write_json(&project_path, project);
            let cleanup = tokio_util::task::TaskTracker::new();
            let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
            let trace = gb_observability::Trace::open(
                &gb_observability::Diagnostics::open(&native.workspace),
                "manual",
            );
            let invocation: gb_wire::WorkflowInvocation = serde_json::from_value(json!({
                "command":"workflows/ticket_driven_workflow", "person_id":"aiko",
                "source":"manual", "trigger_type":"ticket", "payload":{},
                "work":{"kind":"generic", "identity":"failure-speech"}
            }))
            .unwrap();
            let original = runtime
                .dispatcher
                .prepare(
                    "workflows/ticket_driven_workflow",
                    invocation.clone(),
                    trace.id(),
                )
                .await
                .unwrap();
            // Speech must run even while the failed VM's cleanup holds its lease.
            assert_eq!(
                runtime.say("failure", &invocation, &trace).await.as_deref(),
                Some("Safe failure explanation.")
            );
            drop(original);
            assert_eq!(
                runtime.say("failure", &invocation, &trace).await.as_deref(),
                Some("Safe failure explanation.")
            );
            assert_eq!(driver.ran.lock().unwrap().len(), 2);
            cleanup.close();
            cleanup.wait().await;
        }
    }

    fn workflow_fixture(
        native: &Native,
        root: &Path,
        cleanup: &tokio_util::task::TaskTracker,
        receive: bool,
    ) -> (crate::workflow_runtime::Runtime, Arc<MemberTurn>) {
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let mut connections = BTreeMap::new();
        let (mut runtime, _) = crate::workflow_runtime::Runtime::assemble_sources(
            native,
            host,
            "aiko",
            cleanup.clone(),
            Default::default(),
            Default::default(),
            receive.then_some(&mut connections),
        )
        .unwrap();
        let generation = Arc::new(crate::execution::Generation::default());
        runtime.generation = Some(generation.clone());
        runtime.service_run_id = Some(generation.id.clone());
        let driver = Arc::new(MemberTurn {
            chat_action: Mutex::new("reply"),
            completion: Mutex::new("done"),
            after_completion: Default::default(),
            before_completion: Default::default(),
            generation,
            arguments: Default::default(),
            failures: Default::default(),
            tickets: runtime.tickets.clone().unwrap(),
            native: native.clone(),
            calls: Default::default(),
            ran: Default::default(),
        });
        runtime.dispatcher.driver = driver.clone();
        (runtime, driver)
    }
    #[tokio::test]
    async fn native_ticket_patrol_moves_the_local_ticket_and_accepts_real_member_comment_and_completion()
     {
        for (mention, linked) in [(false, false), (true, false), (false, true), (true, true)] {
            let root = tempfile::tempdir().unwrap();
            let native = fixture(root.path());
            let cleanup = tokio_util::task::TaskTracker::new();
            let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
            let tracker = native
                .workspace
                .join(".guildbotics/local/services/tickets/acme%2Fteam");
            let ticket = runtime.tickets.as_ref().unwrap();
            let task = ready_ticket(&native, &runtime).await;
            if mention {
                let path = tracker.join("T-1.json");
                let mut stored: Value =
                    serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
                stored["assignees"] = json!(["local-yuki"]);
                stored["comments"] = json!([{"id":"mention","body":"@local-aiko Please provide context.","author":"owner","created_at":"2026-11-05T00:00:00Z"}]);
                std::fs::write(path, stored.to_string()).unwrap();
            }
            let mut linked_url = None;
            if linked {
                seed_code(&native, root.path());
                let created = runtime
                    .code
                    .as_ref()
                    .unwrap()
                    .create_pull_request(gb_ports::CreatePullRequest {
                        repository: "acme/demo".into(),
                        head: "feature".into(),
                        base: "main".into(),
                        title: "Fix login".into(),
                        body: String::new(),
                        draft: false,
                        tickets: vec![gb_ports::TicketLink {
                            url: task.target.url.clone(),
                            closes: false,
                        }],
                    })
                    .await
                    .unwrap();
                linked_url = Some(created.target.url);
                let path = tracker.join("T-1.json");
                let mut stored: Value =
                    serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
                stored["lane"] = json!("in_progress");
                if !mention {
                    stored["comments"] = json!([{"id":"question","body":"Should I add the check?","author":"local-aiko","created_at":"2026-11-05T00:00:00Z"},{"id":"answer","body":"Yes, please add the check.","author":"owner","created_at":"2026-11-05T00:01:00Z"}]);
                }
                std::fs::write(path, stored.to_string()).unwrap();
            }
            let line = if mention && linked {
                format!("{} 'message=two words'", crate::ticket_selector::COMMAND)
            } else {
                crate::ticket_selector::COMMAND.into()
            };
            let slot = crate::scheduler::slot(
                "routine",
                "aiko",
                &line,
                "2026-11-05T12:00:00".parse().unwrap(),
            );
            let reply = runtime.command(&line, "routine", slot).await.unwrap();
            assert_eq!(reply.text_output, "done");
            assert_eq!(
                driver.arguments.lock().unwrap()[0],
                if mention && linked {
                    vec!["message=two words"]
                } else {
                    vec![]
                }
            );
            let run_id = {
                let calls = driver.calls.lock().unwrap();
                assert_eq!(calls.len(), 1);
                assert_eq!(calls[0].payload["ticket_url"], task.target.url);
                assert_eq!(
                    calls[0].payload["trigger_reason"],
                    if mention {
                        "issue_mention"
                    } else if linked {
                        "issue_comment"
                    } else {
                        "ready_lane"
                    }
                );
                calls[0].run_id.clone()
            };
            let record = RunStore::open(&native.workspace)
                .read(&run_id)
                .unwrap()
                .unwrap();
            assert_eq!(
                record.work_kind,
                if mention { "ticket_mention" } else { "ticket" }
            );
            assert_eq!(record.status, gb_observability::runs::RunState::Succeeded);
            assert_eq!(
                record.result.as_ref().unwrap().status,
                gb_observability::runs::Completion::Done
            );
            assert_eq!(record.result.as_ref().unwrap().subject_url, task.target.url);
            assert_eq!(record.work_identity.unwrap()["slot_kind"], "routine");
            assert_eq!(record.attributes["target.title"], task.target.title);
            assert_eq!(record.attributes["target.url"], task.target.url);
            assert!(runtime.generation.as_ref().unwrap().snapshot().is_empty());
            let page = ticket
                .read(
                    gb_ports::Resource::TicketComments,
                    &task.target.url,
                    None,
                    "",
                )
                .await
                .unwrap();
            assert!(
                page.items()
                    .iter()
                    .any(|comment| comment["body"] == "Handled.")
            );
            assert_eq!(page.items().last().unwrap()["body"], "Handled.");
            if linked {
                assert_eq!(page.items().len(), if mention { 2 } else { 3 });
                let linked = runtime
                    .code
                    .as_ref()
                    .unwrap()
                    .read(
                        gb_ports::Resource::TicketPullRequests,
                        &task.target.url,
                        None,
                        "",
                    )
                    .await
                    .unwrap();
                assert_eq!(linked.items().len(), 1);
                assert_eq!(
                    linked.items()[0]["url"],
                    linked_url.as_ref().unwrap().as_str()
                );
            }
            assert!(ticket.candidates().await.unwrap().is_empty());
            cleanup.close();
            cleanup.wait().await;
        }
    }
    #[tokio::test]
    async fn native_chat_reception_dispatch_and_real_member_reply_complete_one_local_thread() {
        for action in ["reply", "noop", "reaction"] {
            let root = tempfile::tempdir().unwrap();
            let native = fixture(root.path());
            let path = native
                .workspace
                .join(".guildbotics/config/team/members/aiko/member.yml");
            std::fs::write(path,json!({"is_active":true,"message_channels":[{"name":"general","chat":{"channel_id":"C1"}}]}).to_string()).unwrap();
            let cleanup = tokio_util::task::TaskTracker::new();
            let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, true);
            *driver.chat_action.lock().unwrap() = action;
            let runtime = Arc::new(runtime);
            let store = Arc::new(crate::chat_state::Store {
                workspace: native.workspace.clone(),
                writes: runtime.writes.clone(),
                limit: 500,
            });
            let chat = runtime.chat.clone().unwrap();
            let dispatcher = crate::chat_dispatch::Dispatcher {
                person: "aiko".into(),
                service: "local".into(),
                language: "en".into(),
                roster: runtime.roster.clone(),
                chat: chat.clone(),
                store: store.clone(),
                runner: runtime.clone(),
            };
            let mut row = chat_row("M1", "M1", "@local-aiko Please answer.", 1);
            row["occurred_at"] = json!(chrono::Utc::now().to_rfc3339());
            chat_history(&dispatcher, &[row]);
            let connection = runtime.reception.as_ref().unwrap().clone();
            let receiver = Arc::new(
                crate::chat_receiver::Receiver::new(
                    "aiko".into(),
                    chat.clone(),
                    connection,
                    runtime.channels.clone(),
                    store.clone(),
                )
                .await
                .unwrap(),
            );
            let stop = tokio_util::sync::CancellationToken::new();
            let task = tokio::spawn({
                let receiver = receiver.clone();
                let stop = stop.clone();
                async move { receiver.run(stop).await }
            });
            let subject = gb_member::chat_updates::Subject {
                service: "local".into(),
                channel_id: "C1".into(),
                thread_id: "M1".into(),
                event_id: "C1:M1".into(),
                self_user_id: "local-aiko".into(),
            };
            tokio::time::timeout(Duration::from_secs(10), async {
                loop {
                    receiver.drain().await;
                    if !store.pending("local", "aiko", "C1").unwrap().is_empty()
                        && gb_member::chat_updates::Heartbeat::new(&native.workspace).state(
                            &subject,
                            "aiko",
                            chrono::Utc::now().timestamp_millis() as f64 / 1000.0,
                        ) == "ready"
                    {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            assert_eq!(dispatcher.process(chrono::Utc::now()).await.unwrap(), 1);
            stop.cancel();
            task.await.unwrap().unwrap();
            {
                let calls = driver.calls.lock().unwrap();
                assert_eq!(calls.len(), 1);
                assert_eq!(
                    calls[0].work.as_ref().unwrap().identity,
                    "local:local-aiko:C1:M1"
                );
            }
            let remaining = store.pending("local", "aiko", "C1").unwrap();
            assert!(
                store.cursor("local", "aiko", "C1").unwrap()["processed_event_ids"]
                    .as_array()
                    .unwrap()
                    .contains(&json!("C1:M1"))
            );
            let page = chat
                .list_thread_events("C1", "M1", None, 100)
                .await
                .unwrap();
            assert_eq!(page.events.len(), if action == "reply" { 2 } else { 1 });
            if action == "reply" {
                let echo = &page.events[1];
                assert_eq!(echo.message.text, "Handled.");
                assert!(echo.message.is_from_user(Some("local-aiko")));
                assert!(
                    remaining.iter().all(|p| p.event.event_id == echo.event_id),
                    "only the agent's reply echo may remain: {}",
                    serde_json::to_value(&remaining).unwrap()
                );
                // Exercise the echo before a later human event regardless of listener timing.
                store
                    .receive("local", "aiko", echo.clone(), "strict")
                    .unwrap();
                let mut human = page.events[0].clone();
                human.event_id = "C1:M2".into();
                human.message.message_id = "M2".into();
                human.message.text = "@local-aiko Another question.".into();
                human.message.occurred_at = echo.message.occurred_at + chrono::Duration::seconds(1);
                store
                    .receive("local", "aiko", human.clone(), "strict")
                    .unwrap();
                assert_eq!(dispatcher.process(chrono::Utc::now()).await.unwrap(), 1);
                let pending = store.pending("local", "aiko", "C1").unwrap();
                assert_eq!(pending.len(), 1);
                assert_eq!(pending[0].event.event_id, human.event_id);
                assert_eq!(pending[0].attempt_count, 0);
                let processed = store.cursor("local", "aiko", "C1").unwrap();
                let processed = processed["processed_event_ids"].as_array().unwrap();
                assert!(processed.contains(&json!(echo.event_id)));
                assert!(!processed.contains(&json!(human.event_id)));
                assert_eq!(driver.calls.lock().unwrap().len(), 1);
            } else {
                assert!(
                    remaining.is_empty(),
                    "{}",
                    serde_json::to_value(remaining).unwrap()
                );
            }
            let records = RunStore::open(&native.workspace).records().unwrap();
            assert_eq!(records.len(), 1);
            let activity = gb_observability::activity::ActivityEvents::open(&native.workspace)
                .list_between(
                    chrono::DateTime::<chrono::Utc>::MIN_UTC,
                    chrono::DateTime::<chrono::Utc>::MAX_UTC,
                )
                .unwrap();
            let completed = activity
                .iter()
                .filter(|event| event["kind"] == "workflow.completed")
                .collect::<Vec<_>>();
            assert_eq!(completed.len(), 1);
            assert_eq!(completed[0]["payload"]["recovered"], false);
            assert_eq!(completed[0]["payload"]["run_id"], records[0].run_id);
            assert_eq!(completed[0]["payload"]["attempt"], 1);
            let evidence = records[0]
                .provider_evidence
                .iter()
                .map(|e| e["type"].as_str().unwrap())
                .collect::<Vec<_>>();
            let kind = match action {
                "noop" => "chat_noop",
                "reaction" => "chat_reaction",
                _ => "chat_reply",
            };
            assert!(
                evidence.iter().position(|e| *e == "chat_batch").unwrap()
                    < evidence.iter().position(|e| *e == kind).unwrap()
            );
            let state = store.thread("local", "aiko", "C1", "M1").unwrap();
            assert_eq!(
                state["participants"]
                    .as_array()
                    .is_some_and(|a| a.contains(&json!("aiko"))),
                action != "noop"
            );
            assert_eq!(records[0].work_kind, "chat");
            assert_eq!(records[0].attributes["chat.provider"], "local");
            assert_eq!(records[0].attributes["chat.channel"], "C1");
            assert_eq!(records[0].attributes["chat.thread"], "M1");
            assert_eq!(records[0].attributes["chat.message"], "M1");
            assert!(runtime.generation.as_ref().unwrap().snapshot().is_empty());
            assert_eq!(
                records[0].result.as_ref().unwrap().status,
                gb_observability::runs::Completion::Done
            );
            cleanup.close();
            cleanup.wait().await;
        }
    }
    #[tokio::test]
    async fn native_commands_keep_trace_and_journal_in_sync_when_only_autonomous_service_outages_defer()
     {
        let root = tempfile::tempdir().unwrap();
        let native = fixture(root.path());
        let cleanup = tokio_util::task::TaskTracker::new();
        let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
        for source in ["manual", "scheduled", "routine", "event_queue"] {
            for problem in [
                gb_ports::ServiceError::Failed,
                gb_ports::ServiceError::NotFound,
                gb_ports::ServiceError::Unsupported,
                gb_ports::ServiceError::Unavailable,
            ] {
                let deferred = source != "manual" && problem.is_unavailable();
                let error_type = if problem.is_unavailable() {
                    "ServiceUnavailable"
                } else if problem.is_not_found() {
                    "ServiceNotFound"
                } else if problem.is_unsupported() {
                    "ServiceUnsupported"
                } else {
                    "ServiceError"
                };
                driver
                    .failures
                    .lock()
                    .unwrap()
                    .push_back(crate::dispatcher::DispatchFailure::Service(problem));
                let slot = crate::scheduler::slot(
                    source,
                    "aiko",
                    "functions/talk_as",
                    "2026-11-05T12:00:00".parse().unwrap(),
                );
                assert!(
                    runtime
                        .command("functions/talk_as", source, slot)
                        .await
                        .is_err()
                );
                let records = RunStore::open(&native.workspace).records().unwrap();
                let id = driver.calls.lock().unwrap().last().unwrap().run_id.clone();
                let record = records.iter().find(|r| r.run_id == id).unwrap();
                assert_eq!(
                    record.work_kind,
                    if source == "manual" {
                        "manual"
                    } else {
                        "functions/talk_as"
                    }
                );
                assert_eq!(
                    record.status,
                    if deferred {
                        gb_observability::runs::RunState::Deferred
                    } else {
                        gb_observability::runs::RunState::Failed
                    }
                );
                let events = std::fs::read_to_string(
                    native
                        .workspace
                        .join(".guildbotics/local/run/diagnostics.jsonl"),
                )
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str::<Value>(line).unwrap())
                .filter(|e| {
                    e["trace_id"] == record.run_id
                        && e["type"]
                            .as_str()
                            .is_some_and(|kind| kind.starts_with("command."))
                })
                .collect::<Vec<_>>();
                assert_eq!(events.len(), 2);
                assert_eq!(events.last().unwrap()["payload"]["error_type"], error_type);
                assert_eq!(events.first().unwrap()["type"], "command.started");
                assert_eq!(
                    events.last().unwrap()["type"],
                    if deferred {
                        "command.deferred"
                    } else {
                        "command.failed"
                    }
                );
            }
        }
        cleanup.close();
        cleanup.wait().await;
    }
    async fn ready_ticket(
        native: &Native,
        runtime: &crate::workflow_runtime::Runtime,
    ) -> gb_ports::TicketCreated {
        let tracker = native
            .workspace
            .join(".guildbotics/local/services/tickets/acme%2Fteam");
        write_json(
            &tracker.join("tracker.json"),
            json!({"repository":"acme/demo"}),
        );
        runtime
            .tickets
            .as_ref()
            .unwrap()
            .create_ticket(gb_ports::CreateTicket {
                tracker: "acme/team".into(),
                title: "Fix login".into(),
                body: String::new(),
                labels: vec![],
                add_to_board: true,
                interactive: false,
                assignee: Some(gb_ports::ActorName("local-aiko".into())),
                lane: Some(gb_ports::CreateLane::Ready),
                parent: None,
                blocked_by: vec![],
            })
            .await
            .unwrap()
    }
    #[tokio::test]
    async fn a_manual_ticket_run_executes_the_workflow_of_the_configuration_it_read() {
        let root = tempfile::tempdir().unwrap();
        let native = fixture(root.path());
        let cleanup = tokio_util::task::TaskTracker::new();
        let (mut runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
        ready_ticket(&native, &runtime).await;
        let workflow = native
            .workspace
            .join(".guildbotics/config/commands/workflows/ticket_driven_workflow.md");
        std::fs::create_dir_all(workflow.parent().unwrap()).unwrap();
        std::fs::write(&workflow, "---\nbrain: none\n---\nAs read.\n").unwrap();
        let config = Arc::new(
            gb_environment::Configuration::copy(&runtime.dispatcher.host, &native.workspace)
                .unwrap(),
        );
        runtime.config = Some(config.clone());
        // A save after the run read the configuration does not reach it.
        std::fs::write(&workflow, "---\nbrain: none\n---\nSaved later.\n").unwrap();
        runtime.manual_ticket(|trace| trace).await.unwrap();
        let (path, held) = driver.ran.lock().unwrap()[0].clone();
        assert!(path.starts_with(config.path()), "{}", path.display());
        assert_eq!(held, "---\nbrain: none\n---\nAs read.\n");
        cleanup.close();
        cleanup.wait().await;
    }

    #[tokio::test]
    async fn a_manual_ticket_run_streams_its_events_and_answers_its_trace() {
        let root = tempfile::tempdir().unwrap();
        let native = fixture(root.path());
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let controller = crate::service_runtime::Controller::new(native.clone(), host);
        let api = Arc::new(crate::api::Api::new(
            crate::api::Selected {
                workspace: Some(native.workspace.clone()),
                service: controller,
                ..Default::default()
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            crate::auth::ServerAuth::open(&native.data, 8765).unwrap(),
            "fixture".into(),
            native.data.clone(),
        ));
        let work = tokio_util::task::TaskTracker::new();
        let runtime = api
            .selected()
            .service
            .workflow("aiko", work.clone())
            .unwrap();
        ready_ticket(&native, &runtime).await;
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let response = crate::api::router(api.clone())
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/-/guildbotics/api/commands/run")
                    .header("host", "127.0.0.1:8765")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {}", token.trim()))
                    .body(axum::body::Body::from(
                        json!({"command":"workflows/ticket_driven_workflow","person":"aiko"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), 65536)
            .await
            .unwrap();
        let answer: Value = serde_json::from_slice(&bytes).unwrap();
        // The fixture's device refuses the run once its ticket is chosen.
        assert_eq!(status, axum::http::StatusCode::BAD_REQUEST, "{answer}");
        assert_eq!(answer["code"], "command_error");
        let trace = answer["context"]["trace_id"].as_str().unwrap();
        assert!(!trace.is_empty());
        let streamed: Vec<_> = api
            .events
            .snapshot()
            .into_iter()
            .filter(|e| e["trace_id"] == trace)
            .map(|e| e["type"].as_str().unwrap().to_owned())
            .collect();
        assert_eq!(streamed, ["command.started", "command.failed"]);
        work.close();
        work.wait().await;
    }
    #[tokio::test]
    async fn native_patrol_selection_and_refresh_outages_keep_chat_alive_and_recover_without_failed_runs()
     {
        use crate::scheduler::Engine as _;
        for phase in ["candidates", "refresh"] {
            let root = tempfile::tempdir().unwrap();
            let native = fixture(root.path());
            let cleanup = tokio_util::task::TaskTracker::new();
            let (mut runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
            let ticket = ready_ticket(&native, &runtime).await;
            let faults = Arc::new(crate::test_ticket::Faults::new(
                runtime.tickets.clone().unwrap(),
            ));
            faults
                .errors
                .lock()
                .unwrap()
                .insert(phase, gb_ports::ServiceError::Unavailable);
            runtime.tickets = Some(faults.clone());
            let runtime = Arc::new(runtime);
            let dispatch = crate::chat_dispatch::Dispatcher {
                person: "aiko".into(),
                service: "local".into(),
                language: "en".into(),
                roster: runtime.roster.clone(),
                chat: runtime.chat.clone().unwrap(),
                store: Arc::new(crate::chat_state::Store {
                    workspace: native.workspace.clone(),
                    writes: runtime.writes.clone(),
                    limit: 500,
                }),
                runner: runtime.clone(),
            };
            chat_history(
                &dispatch,
                &[chat_row("M1", "M1", "@local-aiko handle chat", 1)],
            );
            let event = dispatch
                .chat
                .list_thread_events("C1", "M1", None, 100)
                .await
                .unwrap()
                .events
                .remove(0);
            dispatch
                .store
                .receive("local", "aiko", event, "strict")
                .unwrap();
            let subject = gb_member::chat_updates::Subject {
                service: "local".into(),
                channel_id: "C1".into(),
                thread_id: "M1".into(),
                event_id: "C1:M1".into(),
                self_user_id: "local-aiko".into(),
            };
            gb_member::chat_updates::Heartbeat::new(&native.workspace)
                .save(
                    &subject,
                    "aiko",
                    "ready",
                    chrono::Utc::now().timestamp_millis() as f64 / 1000.0,
                    0.0,
                )
                .unwrap();
            let engine = crate::native_scheduler::Engine::new(runtime.clone(), Some(dispatch));
            for minute in 0..4 {
                let slot = crate::scheduler::slot(
                    "routine",
                    "aiko",
                    crate::ticket_selector::COMMAND,
                    "2026-11-05T12:00:00"
                        .parse::<chrono::NaiveDateTime>()
                        .unwrap()
                        + chrono::Duration::minutes(minute),
                );
                let (outcome, _) = engine.patrol(slot).await;
                assert_eq!(outcome, crate::service::Outcome::Deferred);
                assert_eq!(
                    engine.events().await,
                    if minute == 0 {
                        crate::service::Outcome::Succeeded
                    } else {
                        crate::service::Outcome::Deferred
                    }
                );
                assert_eq!(driver.calls.lock().unwrap().len(), 1);
            }
            faults.errors.lock().unwrap().clear();
            let slot = crate::scheduler::slot(
                "routine",
                "aiko",
                crate::ticket_selector::COMMAND,
                "2026-11-05T12:04:00".parse().unwrap(),
            );
            assert_eq!(
                engine.patrol(slot).await,
                (crate::service::Outcome::Succeeded, true)
            );
            let calls = driver.calls.lock().unwrap().clone();
            assert_eq!(calls.len(), 2);
            assert_eq!(calls[0].work.as_ref().unwrap().kind, "chat");
            assert_eq!(calls[1].payload["ticket_url"], ticket.target.url);
            let records = RunStore::open(&native.workspace).records().unwrap();
            assert_eq!(records.len(), 2);
            assert!(
                records
                    .iter()
                    .all(|r| r.status == gb_observability::runs::RunState::Succeeded
                        && r.result.is_some())
            );
            cleanup.close();
            cleanup.wait().await;
        }
    }

    #[tokio::test]
    async fn native_completed_ticket_waits_notify_once_and_later_execution_failure_reports_failed()
    {
        for source in ["routine", "manual"] {
            for status in ["done", "asking", "blocked"] {
                for phase in ["none", "before", "after"] {
                    let failed = phase != "none";
                    let fails_later = phase == "after";
                    let root = tempfile::tempdir().unwrap();
                    let native = fixture(root.path());
                    write_json(
                        &native
                            .workspace
                            .join(".guildbotics/config/team/members/aiko/member.yml"),
                        json!({"is_active":true,"routine_commands":["workflows/ticket_driven_workflow"],"message_channels":[{"name":"notify","chat":{"channel_id":"C1","notify_work_stops":true}}]}),
                    );
                    let cleanup = tokio_util::task::TaskTracker::new();
                    let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
                    *driver.completion.lock().unwrap() = status;
                    if fails_later {
                        *driver.after_completion.lock().unwrap() =
                            Some(crate::dispatcher::DispatchFailure::Journal);
                    }
                    if phase == "before" {
                        driver
                            .failures
                            .lock()
                            .unwrap()
                            .push_back(crate::dispatcher::DispatchFailure::Journal);
                    }
                    let ticket = ready_ticket(&native, &runtime).await;
                    let slot = crate::scheduler::slot(
                        source,
                        "aiko",
                        crate::ticket_selector::COMMAND,
                        "2026-11-05T12:00:00".parse().unwrap(),
                    );
                    let result = runtime
                        .command(crate::ticket_selector::COMMAND, source, slot)
                        .await;
                    assert_eq!(result.is_err(), failed);
                    if phase == "before" {
                        let records = RunStore::open(&native.workspace).records().unwrap();
                        assert_eq!(records.len(), 1);
                        assert_eq!(records[0].status, gb_observability::runs::RunState::Failed);
                        assert!(records[0].finished_at.is_some() && records[0].result.is_none());
                    }
                    let notices = runtime
                        .chat
                        .as_ref()
                        .unwrap()
                        .list_channel_events("C1", Default::default())
                        .await
                        .unwrap()
                        .events;
                    let expected = source != "manual" && (failed || status != "done");
                    assert_eq!(notices.len(), usize::from(expected));
                    if let Some(notice) = notices.first() {
                        let payload =
                            serde_json::to_value(notice.status.as_ref().unwrap()).unwrap();
                        assert_eq!(payload["reason"], if failed { "failed" } else { status });
                        assert_eq!(payload["subject"], ticket.target.url);
                        let calls = driver.calls.lock().unwrap();
                        assert_eq!(payload["run_id"], calls[0].run_id);
                    }
                    let comments = runtime
                        .tickets
                        .as_ref()
                        .unwrap()
                        .read(
                            gb_ports::Resource::TicketComments,
                            &ticket.target.url,
                            None,
                            "",
                        )
                        .await
                        .unwrap();
                    assert_eq!(comments.items().len(), if fails_later { 2 } else { 1 });
                    if failed {
                        assert_ne!(comments.items().last().unwrap()["body"], "Handled.");
                    }
                    cleanup.close();
                    cleanup.wait().await;
                }
            }
        }
    }

    #[tokio::test]
    async fn native_failed_selection_closes_traces_without_runs_and_records_the_worker_error_limit()
    {
        use crate::scheduler::Engine as _;
        for phase in ["candidates", "refresh"] {
            let root = tempfile::tempdir().unwrap();
            let native = fixture(root.path());
            let cleanup = tokio_util::task::TaskTracker::new();
            let (mut runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
            ready_ticket(&native, &runtime).await;
            let faults = Arc::new(crate::test_ticket::Faults::new(
                runtime.tickets.clone().unwrap(),
            ));
            faults
                .errors
                .lock()
                .unwrap()
                .insert(phase, gb_ports::ServiceError::Failed);
            runtime.tickets = Some(faults.clone());
            let runtime = Arc::new(runtime);
            let engine = crate::native_scheduler::Engine::new(runtime.clone(), None);
            for minute in 0..3 {
                let slot = crate::scheduler::slot(
                    "routine",
                    "aiko",
                    crate::ticket_selector::COMMAND,
                    "2026-11-05T12:00:00"
                        .parse::<chrono::NaiveDateTime>()
                        .unwrap()
                        + chrono::Duration::minutes(minute),
                );
                assert_eq!(engine.patrol(slot).await.0, crate::service::Outcome::Failed);
            }
            engine.failed(3, 3, "routine");
            assert!(driver.calls.lock().unwrap().is_empty());
            assert!(
                RunStore::open(&native.workspace)
                    .records()
                    .unwrap()
                    .is_empty()
            );
            let events = std::fs::read_to_string(
                native
                    .workspace
                    .join(".guildbotics/local/run/diagnostics.jsonl"),
            )
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .collect::<Vec<_>>();
            assert_eq!(
                events
                    .iter()
                    .filter(|e| e["type"] == "command.failed")
                    .count(),
                3
            );
            let worker = events
                .iter()
                .filter(|e| e["type"] == "scheduler.worker.failed")
                .collect::<Vec<_>>();
            assert_eq!(worker.len(), 1);
            assert_eq!(worker[0]["source"], "scheduler");
            assert_eq!(worker[0]["person_id"], "aiko");
            assert_eq!(
                worker[0]["attributes"]["service_run_id"],
                runtime.generation.as_ref().unwrap().id
            );
            assert_eq!(
                worker[0]["payload"],
                json!({"source":"routine","consecutive_errors":3,"consecutive_error_limit":3})
            );
            cleanup.close();
            cleanup.wait().await;
        }
    }
    #[test]
    fn native_members_reuse_one_listener_and_wakeup_for_the_same_connection() {
        let root = tempfile::tempdir().unwrap();
        let native = fixture(root.path());
        for person in ["aiko", "yuki"] {
            write_json(
                &native.workspace.join(format!(
                    ".guildbotics/config/team/members/{person}/member.yml"
                )),
                json!({"is_active":true,"message_channels":[{"chat":{"channel_id":"C1"}}]}),
            );
            if person == "yuki" {
                write_json(
                    &native
                        .identity_root
                        .join(".guildbotics/config/team/members/yuki/identity.yml"),
                    json!({"person_id":"yuki","name":"Yuki","person_type":"agent","profile":{}}),
                );
            }
        }
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let mut connections = BTreeMap::new();
        let mut made = vec![];
        for person in ["aiko", "yuki", "aiko"] {
            let (runtime, _) = crate::workflow_runtime::Runtime::assemble_sources(
                &native,
                host.clone(),
                person,
                Default::default(),
                Default::default(),
                Default::default(),
                Some(&mut connections),
            )
            .unwrap();
            made.push(runtime.reception.unwrap());
        }
        assert_eq!(connections.len(), 1);
        assert!(
            made.windows(2)
                .all(|pair| Arc::ptr_eq(&pair[0].listener, &pair[1].listener)
                    && Arc::ptr_eq(&pair[0].wake, &pair[1].wake))
        );
    }

    pub(crate) struct Received {
        pub connected: std::sync::atomic::AtomicBool,
        pub events: Mutex<Vec<ChatEvent>>,
        pub starts: std::sync::atomic::AtomicUsize,
        pub stops: std::sync::atomic::AtomicUsize,
    }
    impl Received {
        pub(crate) fn new() -> Self {
            Self {
                connected: std::sync::atomic::AtomicBool::new(true),
                events: Default::default(),
                starts: Default::default(),
                stops: Default::default(),
            }
        }
    }
    #[tokio::test]
    async fn completed_chat_recovery_consumes_only_its_batch_and_updates_with_a_later_action() {
        for batch in [false, true] {
            for action in [
                None,
                Some("chat_reply"),
                Some("chat_noop"),
                Some("blocked"),
                Some("issue_comment"),
                Some("git_publish"),
                Some("git_push"),
                Some("issue_update"),
            ] {
                let root = tempfile::tempdir().unwrap();
                let native = fixture(root.path());
                let cleanup = tokio_util::task::TaskTracker::new();
                let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
                let runtime = Arc::new(runtime);
                let dispatch = crate::chat_dispatch::Dispatcher {
                    person: "aiko".into(),
                    service: "local".into(),
                    language: "en".into(),
                    roster: runtime.roster.clone(),
                    chat: runtime.chat.clone().unwrap(),
                    store: Arc::new(crate::chat_state::Store {
                        workspace: native.workspace.clone(),
                        writes: runtime.writes.clone(),
                        limit: 500,
                    }),
                    runner: runtime.clone(),
                };
                let rows = (1..=4)
                    .map(|n| chat_row(&format!("M{n}"), "M1", "@local-aiko work", n))
                    .collect::<Vec<_>>();
                chat_history(&dispatch, &rows);
                let events = dispatch
                    .chat
                    .list_thread_events("C1", "M1", None, 100)
                    .await
                    .unwrap()
                    .events;
                for (index, event) in events.into_iter().enumerate() {
                    if index != 1 || batch {
                        dispatch
                            .store
                            .receive("local", "aiko", event, "strict")
                            .unwrap();
                    }
                }
                let mut evidence = vec![json!({"type":"chat_reply","payload":{"text":"answered"}})];
                if batch {
                    evidence.push(
                        json!({"type":"chat_batch","payload":{"event_ids":["C1:M1","C1:M2"]}}),
                    );
                }
                if let Some(action) = action {
                    evidence.push(json!({"type":"chat_updates","payload":{"event_ids":["C1:M3"]}}));
                    if action != "blocked" {
                        evidence.push(json!({"type":action,"payload":{"text":"decision"}}));
                    }
                }
                let record:RunRecord=serde_json::from_value(json!({"run_id":"prior-run","work_kind":"chat","execution_mode":"autonomous","member_id":"aiko","device_id":"device","source":"event_queue","started_at":"2026-11-05T00:00:00Z","finished_at":"2026-11-05T00:01:00Z","status":"succeeded",
                    "work_identity":{"service":"local","channel_id":"C1","event_id":"C1:M1"},"provider_evidence":evidence,
                    "result":{"subject_type":"chat","subject_id":"local:C1:M1","status":if action==Some("blocked") {"blocked"} else {"done"}}})).unwrap();
                runtime
                    .writes
                    .transaction(|writer| {
                        RunStore::open(&native.workspace)
                            .start(writer, record.clone(), &runtime.redactor)
                            .map(|_| ())
                    })
                    .unwrap();
                if batch {
                    // The host already emitted the terminal event before recovery.
                    let mut prior =
                        dispatch.store.pending("local", "aiko", "C1").unwrap()[0].clone();
                    prior.run_id = "prior-run".into();
                    crate::chat_dispatch::Runner::event(
                        runtime.as_ref(),
                        "workflow.completed",
                        &prior,
                        json!({"run_id":"prior-run","status":record.result.as_ref().unwrap().status}),
                    );
                }
                assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
                assert!(driver.calls.lock().unwrap().is_empty());
                let pending = dispatch.store.pending("local", "aiko", "C1").unwrap();
                let unhandled =
                    action.is_none() || action == Some("chat_noop") || action == Some("blocked");
                assert_eq!(
                    pending
                        .iter()
                        .map(|p| p.event.event_id.as_str())
                        .collect::<Vec<_>>(),
                    if unhandled {
                        vec!["C1:M3", "C1:M4"]
                    } else {
                        vec!["C1:M4"]
                    }
                );
                assert!(
                    dispatch.store.cursor("local", "aiko", "C1").unwrap()["processed_event_ids"]
                        .as_array()
                        .unwrap()
                        .contains(&json!("C1:M1"))
                );
                let records = RunStore::open(&native.workspace).records().unwrap();
                assert_eq!(records.len(), 1);
                assert_eq!(records[0], record);
                let transcript = std::fs::read_to_string(
                    native
                        .workspace
                        .join(".guildbotics/local/run/sessions/prior-run.jsonl"),
                )
                .unwrap();
                assert_eq!(
                    transcript
                        .lines()
                        .map(|line| serde_json::from_str::<Value>(line).unwrap())
                        .filter(|e| e["type"] == "workflow.completed"
                            && e["payload"]["run_id"] == "prior-run")
                        .count(),
                    1
                );
                if !batch {
                    let activity =
                        gb_observability::activity::ActivityEvents::open(&native.workspace)
                            .list_between(
                                chrono::DateTime::<chrono::Utc>::MIN_UTC,
                                chrono::DateTime::<chrono::Utc>::MAX_UTC,
                            )
                            .unwrap();
                    let completed = activity
                        .iter()
                        .find(|event| event["kind"] == "workflow.completed")
                        .unwrap();
                    assert_eq!(completed["payload"]["run_id"], "prior-run");
                    assert_eq!(completed["payload"]["recovered"], true);
                }
                cleanup.close();
                cleanup.wait().await;
            }
        }
    }
    #[tokio::test]
    async fn completed_chat_recovery_reconciles_each_event_store_once_across_failed_settlement_restarts()
     {
        for diagnostic_exists in [false, true] {
            for activity_exists in [false, true] {
                let root = tempfile::tempdir().unwrap();
                let native = fixture(root.path());
                let cleanup = tokio_util::task::TaskTracker::new();
                let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
                let runtime = Arc::new(runtime);
                let mut dispatch = crate::chat_dispatch::Dispatcher {
                    person: "aiko".into(),
                    service: "local".into(),
                    language: "en".into(),
                    roster: runtime.roster.clone(),
                    chat: runtime.chat.clone().unwrap(),
                    store: Arc::new(crate::chat_state::Store {
                        workspace: native.workspace.clone(),
                        writes: runtime.writes.clone(),
                        limit: 500,
                    }),
                    runner: runtime.clone(),
                };
                seed_chat(&dispatch, &[chat_row("M1", "M1", "@local-aiko work", 1)]).await;
                let mut pending = dispatch
                    .store
                    .pending("local", "aiko", "C1")
                    .unwrap()
                    .remove(0);
                pending.run_id = "interrupted-completion".into();
                pending.attempt_count = 3;
                dispatch.store.save("local", "aiko", &pending).unwrap();
                let record: RunRecord = serde_json::from_value(json!({
                    "run_id":pending.run_id,"work_kind":"chat","execution_mode":"autonomous",
                    "member_id":"aiko","device_id":"device","source":"event_queue",
                    "started_at":"2026-10-31T23:58:00Z","finished_at":"2026-10-31T23:59:00Z","status":"succeeded",
                    "work_identity":{"service":"local","channel_id":"C1","event_id":"C1:M1"},
                    "provider_evidence":[{"type":"chat_reply","payload":{"text":"answered"}}],
                    "result":{"subject_type":"chat","subject_id":"local:C1:M1","status":"done"}
                })).unwrap();
                runtime
                    .writes
                    .transaction(|writer| {
                        RunStore::open(&native.workspace).start(
                            writer,
                            record.clone(),
                            &runtime.redactor,
                        )?;
                        Ok(())
                    })
                    .unwrap();
                let trace = gb_observability::Trace::resume(
                    &gb_observability::Diagnostics::open(&native.workspace),
                    "event_queue",
                    &pending.run_id,
                )
                .unwrap()
                .person("aiko")
                .command(crate::chat_dispatch::COMMAND);
                let mut original = gb_member::events::completed(&pending.run_id, 2, false, "done");
                original["attributes"]["service_run_id"] = json!("prior-service");
                let trace = original["attributes"]
                    .as_object()
                    .unwrap()
                    .iter()
                    .fold(trace, |trace, (key, value)| {
                        trace.attribute(key, value.clone())
                    });
                if diagnostic_exists {
                    trace.event(
                        "workflow.completed",
                        original["payload"].as_object().unwrap().clone(),
                    );
                }
                let activity = gb_observability::activity::ActivityEvents::open(&native.workspace);
                if activity_exists {
                    let mut event = trace.correlation();
                    event["timestamp"] = json!(record.finished_at);
                    event["type"] = original["type"].clone();
                    event["payload"] = original["payload"].clone();
                    runtime
                        .writes
                        .transaction(|writer| {
                            activity.record_once(writer, &event, &runtime.redactor)?;
                            Ok(())
                        })
                        .unwrap();
                }
                if !activity_exists {
                    // Failure at this run's month must retain the durable input.
                    let month = native.workspace.join(".guildbotics/state/events/2026/10");
                    std::fs::create_dir_all(month.parent().unwrap()).unwrap();
                    std::fs::write(&month, "blocked").unwrap();
                    assert!(dispatch.process(chrono::Utc::now()).await.is_err());
                    assert_eq!(
                        dispatch.store.pending("local", "aiko", "C1").unwrap().len(),
                        1
                    );
                    assert!(driver.calls.lock().unwrap().is_empty());
                    std::fs::remove_file(month).unwrap();
                }
                // Unrelated damaged history must not block completion or acknowledgement.
                let broken = native
                    .workspace
                    .join(".guildbotics/state/events/2025/01/broken.json");
                std::fs::create_dir_all(broken.parent().unwrap()).unwrap();
                std::fs::write(&broken, "broken").unwrap();
                std::fs::create_dir_all(
                    native
                        .workspace
                        .join(".guildbotics/state/events/2026/10/unreadable.json"),
                )
                .unwrap();
                // A directory at the thread file forces settlement to fail after event persistence.
                let blocked = dispatch
                    .store
                    .thread_file("local", "aiko", "C1", "M1", false)
                    .unwrap();
                std::fs::create_dir_all(&blocked).unwrap();
                for attempt in 0..4 {
                    let (restarted, restarted_driver) =
                        workflow_fixture(&native, root.path(), &cleanup, false);
                    dispatch.runner = Arc::new(restarted);
                    if attempt < 3 {
                        assert!(dispatch.process(chrono::Utc::now()).await.is_err());
                        assert_eq!(
                            dispatch.store.pending("local", "aiko", "C1").unwrap().len(),
                            1
                        );
                    } else {
                        std::fs::remove_dir(&blocked).unwrap();
                        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
                        assert!(
                            dispatch
                                .store
                                .pending("local", "aiko", "C1")
                                .unwrap()
                                .is_empty()
                        );
                    }
                    assert!(driver.calls.lock().unwrap().is_empty());
                    assert!(restarted_driver.calls.lock().unwrap().is_empty());
                    let check_completed = |completed: Vec<&Value>| {
                        assert_eq!(completed.len(), 1);
                        if diagnostic_exists || activity_exists {
                            assert_eq!(
                                completed[0]["attributes"]["service_run_id"],
                                "prior-service"
                            );
                        }
                        assert_eq!(completed[0]["payload"]["run_id"], pending.run_id);
                        assert_eq!(
                            completed[0]["payload"]["attempt"],
                            if diagnostic_exists || activity_exists {
                                2
                            } else {
                                3
                            }
                        );
                        assert_eq!(
                            completed[0]["payload"]["recovered"],
                            !(diagnostic_exists || activity_exists)
                        );
                    };
                    let detail = gb_observability::Diagnostics::open(&native.workspace)
                        .trace_detail(&pending.run_id)
                        .unwrap();
                    let completed = detail["records"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .filter(|event| event["type"] == "workflow.completed")
                        .collect::<Vec<_>>();
                    check_completed(completed);
                    let events = activity
                        .list_between(
                            chrono::DateTime::<chrono::Utc>::MIN_UTC,
                            chrono::DateTime::<chrono::Utc>::MAX_UTC,
                        )
                        .unwrap();
                    let completed = events
                        .iter()
                        .filter(|event| event["kind"] == "workflow.completed")
                        .collect::<Vec<_>>();
                    check_completed(completed);
                    assert_eq!(
                        RunStore::open(&native.workspace)
                            .read(&pending.run_id)
                            .unwrap()
                            .unwrap(),
                        record
                    );
                }
                cleanup.close();
                cleanup.wait().await;
            }
        }
    }
    #[tokio::test]
    async fn malformed_scheduled_and_routine_commands_fail_under_a_closed_trace_and_run() {
        let root = tempfile::tempdir().unwrap();
        let native = fixture(root.path());
        let cleanup = tokio_util::task::TaskTracker::new();
        let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
        for source in ["scheduled", "routine"] {
            for line in ["functions/talk_as '", "   ", "missing"] {
                let slot = crate::scheduler::slot(
                    source,
                    "aiko",
                    line,
                    "2026-11-05T12:00:00".parse().unwrap(),
                );
                assert!(matches!(
                    runtime.command(line, source, slot).await,
                    Err(crate::workflow_runtime::Refused::Dispatch(
                        crate::dispatcher::DispatchFailure::InvalidCommand
                    ))
                ));
            }
        }
        assert!(driver.calls.lock().unwrap().is_empty());
        let records = RunStore::open(&native.workspace).records().unwrap();
        assert_eq!(records.len(), 6);
        let events = std::fs::read_to_string(
            native
                .workspace
                .join(".guildbotics/local/run/diagnostics.jsonl"),
        )
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .collect::<Vec<_>>();
        for record in records {
            assert_eq!(record.status, gb_observability::runs::RunState::Failed);
            assert!(record.finished_at.is_some());
            let boundary = events
                .iter()
                .filter(|e| {
                    e["trace_id"] == record.run_id
                        && e["type"]
                            .as_str()
                            .is_some_and(|kind| kind.starts_with("command."))
                })
                .collect::<Vec<_>>();
            assert_eq!(boundary.len(), 2);
            assert_eq!(boundary[0]["type"], "command.started");
            assert_eq!(boundary[1]["type"], "command.failed");
        }
        cleanup.close();
        cleanup.wait().await;
    }
    #[tokio::test]
    async fn local_chat_preparation_failure_spends_budget_but_history_outage_retries_without_a_run()
    {
        for operation in ["identity:", "thread:M1"] {
            for error in [
                gb_ports::ServiceError::Failed,
                gb_ports::ServiceError::NotFound,
                gb_ports::ServiceError::Unsupported,
                gb_ports::ServiceError::Unavailable,
            ] {
                let root = tempfile::tempdir().unwrap();
                let native = fixture(root.path());
                let cleanup = tokio_util::task::TaskTracker::new();
                let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
                let runtime = Arc::new(runtime);
                let chat = Arc::new(crate::test_chat::Faults::new(runtime.chat.clone().unwrap()));
                chat.errors
                    .lock()
                    .unwrap()
                    .insert(operation.into(), error.clone());
                let dispatch = crate::chat_dispatch::Dispatcher {
                    person: "aiko".into(),
                    service: "local".into(),
                    language: "en".into(),
                    roster: runtime.roster.clone(),
                    chat: chat.clone(),
                    store: Arc::new(crate::chat_state::Store {
                        workspace: native.workspace.clone(),
                        writes: runtime.writes.clone(),
                        limit: 500,
                    }),
                    runner: runtime.clone(),
                };
                chat_history(&dispatch, &[chat_row("M1", "M1", "@local-aiko work", 1)]);
                let event = chat
                    .inner
                    .list_thread_events("C1", "M1", None, 100)
                    .await
                    .unwrap()
                    .events
                    .remove(0);
                let mut pending = crate::chat_state::Pending::new(event, "strict");
                pending.max_attempts = 2;
                dispatch.store.save("local", "aiko", &pending).unwrap();
                let now = "2026-11-05T00:00:05Z"
                    .parse::<chrono::DateTime<chrono::Utc>>()
                    .unwrap();
                let unavailable = operation.starts_with("thread") || error.is_unavailable();
                let mut id = String::new();
                for attempt in 0..3 {
                    assert_eq!(
                        dispatch
                            .process(now + chrono::Duration::minutes(attempt * 10))
                            .await
                            .unwrap(),
                        0
                    );
                    let queued = dispatch.store.pending("local", "aiko", "C1").unwrap();
                    if unavailable {
                        assert_eq!(queued.len(), 1);
                        assert_eq!(queued[0].attempt_count, 0);
                        assert_eq!(queued[0].last_error_category, "provider_unavailable");
                        assert!(queued[0].next_attempt_at.is_some());
                    } else if attempt == 0 {
                        assert_eq!(queued.len(), 1);
                        assert_eq!(queued[0].attempt_count, 1);
                        assert_eq!(queued[0].last_error_category, "failed");
                        assert!(queued[0].next_attempt_at.is_some());
                    } else {
                        assert!(queued.is_empty());
                    }
                    if let Some(pending) = queued.first() {
                        id = pending.run_id.clone();
                    }
                }
                assert!(driver.calls.lock().unwrap().is_empty());
                assert!(
                    RunStore::open(&native.workspace)
                        .records()
                        .unwrap()
                        .is_empty()
                );
                let events = std::fs::read_to_string(
                    native
                        .workspace
                        .join(".guildbotics/local/run/diagnostics.jsonl"),
                )
                .unwrap()
                .lines()
                .map(|line| serde_json::from_str::<Value>(line).unwrap())
                .filter(|e| {
                    e["trace_id"] == id
                        && e["type"]
                            .as_str()
                            .is_some_and(|kind| kind.starts_with("command."))
                })
                .collect::<Vec<_>>();
                assert_eq!(events.len(), if unavailable { 6 } else { 4 });
                for pair in events.chunks_exact(2) {
                    assert_eq!(pair[0]["type"], "command.started");
                    assert_eq!(
                        pair[1]["type"],
                        if operation == "identity:" && error.is_unavailable() {
                            "command.deferred"
                        } else {
                            "command.failed"
                        }
                    );
                    assert_eq!(pair[1]["attributes"]["chat.channel"], "C1");
                    assert_eq!(pair[1]["attributes"]["chat.thread"], "M1");
                    assert_eq!(pair[1]["attributes"]["chat.message"], "M1");
                }
                let history = chat
                    .inner
                    .list_thread_events("C1", "M1", None, 100)
                    .await
                    .unwrap();
                assert_eq!(history.events.len(), if unavailable { 1 } else { 2 });
                if !unavailable {
                    assert_eq!(
                        history.events[1].status.as_ref().unwrap().reason(),
                        gb_ports::WorkflowStatusReason::Failed
                    );
                }
                cleanup.close();
                cleanup.wait().await;
            }
        }
    }
    #[derive(Default)]
    struct ChatRuns {
        invocations: Mutex<Vec<gb_wire::WorkflowInvocation>>,
        failures: Mutex<std::collections::VecDeque<crate::chat_dispatch::Failure>>,
        records: Mutex<BTreeMap<String, RunRecord>>,
        events: Mutex<Vec<Value>>,
    }
    impl crate::chat_dispatch::Runner for ChatRuns {
        fn dispatch<'a>(
            &'a self,
            pending: &'a mut crate::chat_state::Pending,
            invocation: gb_wire::WorkflowInvocation,
            store: &'a crate::chat_state::Store,
        ) -> crate::service::Pending<
            'a,
            std::result::Result<RunRecord, crate::chat_dispatch::Failure>,
        > {
            Box::pin(async move {
                pending.attempt_count += 1;
                store.save("local", &invocation.person_id, pending).unwrap();
                self.invocations.lock().unwrap().push(invocation.clone());
                if let Some(error) = self.failures.lock().unwrap().pop_front() {
                    if matches!(
                        error,
                        crate::chat_dispatch::Failure::Unavailable
                            | crate::chat_dispatch::Failure::Rejected
                    ) {
                        pending.attempt_count -= 1;
                        store.save("local", &invocation.person_id, pending).unwrap();
                    }
                    return Err(error);
                }
                let record:RunRecord=serde_json::from_value(json!({"run_id":pending.run_id,"work_kind":"chat","execution_mode":"autonomous","member_id":"aiko","device_id":"device","started_at":"2026-11-05T00:00:00Z","finished_at":"2026-11-05T00:01:00Z","status":"succeeded",
                    "result":{"subject_type":"chat","subject_id":"event","status":"done"},"provider_evidence":[{"type":"chat_batch","payload":{"event_ids":invocation.payload["prompt"]["event_ids"]}}]})).unwrap();
                self.records
                    .lock()
                    .unwrap()
                    .insert(pending.run_id.clone(), record.clone());
                Ok(record)
            })
        }
        fn record(&self, id: &str) -> anyhow::Result<Option<RunRecord>> {
            Ok(self.records.lock().unwrap().get(id).cloned())
        }
        fn event(&self, kind: &str, pending: &crate::chat_state::Pending, payload: Value) {
            self.events.lock().unwrap().push(json!({"type":kind,"run_id":pending.run_id,"attempt":pending.attempt_count,"max_attempts":pending.max_attempts,"payload":payload}));
        }
    }
    fn chat_fixture(root: &Path) -> (crate::chat_dispatch::Dispatcher, Arc<ChatRuns>) {
        let native = fixture(root);
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let (runtime, _) = crate::workflow_runtime::Runtime::assemble(
            &native,
            host,
            "aiko",
            Default::default(),
            Default::default(),
            Default::default(),
        )
        .unwrap();
        let store = Arc::new(crate::chat_state::Store {
            workspace: native.workspace,
            writes: runtime.writes,
            limit: 500,
        });
        let runner = Arc::new(ChatRuns::default());
        let dispatch = crate::chat_dispatch::Dispatcher {
            person: "aiko".into(),
            service: "local".into(),
            language: "en".into(),
            roster: runtime.roster,
            chat: runtime.chat.unwrap(),
            store,
            runner: runner.clone(),
        };
        (dispatch, runner)
    }
    fn chat_history(dispatch: &crate::chat_dispatch::Dispatcher, rows: &[Value]) {
        let path = dispatch
            .store
            .workspace
            .join(".guildbotics/local/services/chat/C1.jsonl");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            path,
            rows.iter()
                .map(|row| format!("{row}\n"))
                .collect::<String>(),
        )
        .unwrap();
    }
    fn chat_row(id: &str, thread: &str, text: &str, second: u32) -> Value {
        json!({"message_id":id,"thread_id":thread,"author":"owner","text":text,"occurred_at":format!("2026-11-05T00:00:{second:02}Z")})
    }
    #[tokio::test]
    async fn local_chat_snapshot_paginates_bounds_history_and_keeps_every_unread_message_at_the_same_time()
     {
        for head in [1, 150] {
            let root = tempfile::tempdir().unwrap();
            let (dispatch, runner) = chat_fixture(root.path());
            let rows = (1..=150)
                .map(|i| chat_row(&format!("M{i:03}"), "M001", "@local-aiko work", 1))
                .collect::<Vec<_>>();
            chat_history(&dispatch, &rows);
            let events = dispatch
                .chat
                .list_thread_events("C1", "M001", None, 200)
                .await
                .unwrap()
                .events;
            assert_eq!(events.len(), 150);
            dispatch
                .store
                .receive("local", "aiko", events[head - 1].clone(), "strict")
                .unwrap();
            assert_eq!(
                dispatch
                    .process("2026-11-05T00:00:05Z".parse().unwrap())
                    .await
                    .unwrap(),
                1
            );
            let calls = runner.invocations.lock().unwrap();
            assert_eq!(calls.len(), 1);
            let prompt = &calls[0].payload["prompt"];
            assert_eq!(prompt["thread_context_complete"], true);
            assert_eq!(
                prompt["unprocessed_messages"].as_array().unwrap().len(),
                151 - head
            );
            assert_eq!(
                prompt["thread_context"].as_array().unwrap().len(),
                if head == 150 { 100 } else { 0 }
            );
            assert_eq!(
                prompt["source_snapshot"].as_array().unwrap().len(),
                if head == 150 { 101 } else { 150 }
            );
            assert_eq!(
                prompt["unprocessed_messages"][0]["timestamp"],
                events[head - 1].message.position()
            );
            assert_eq!(
                prompt["unprocessed_messages"][150 - head]["timestamp"],
                events[149].message.position()
            );
            if head == 150 {
                assert_eq!(
                    prompt["thread_context"][0]["timestamp"],
                    events[49].message.position()
                );
            }
        }
    }
    #[tokio::test]
    async fn local_chat_dispatch_participation_gates_leave_no_run_and_provider_outage_keeps_budget()
    {
        for (policy, text, self_message, expected) in [
            ("strict", "ambient", false, 0),
            ("social", "ambient", false, 1),
            ("muted", "@local-aiko work", false, 1),
            ("social", "@other work", false, 0),
            ("strict", "@local-aiko work", true, 0),
        ] {
            let temp = tempfile::tempdir().unwrap();
            let (dispatch, runner) = chat_fixture(temp.path());
            let mut row = chat_row("M1", "M1", text, 1);
            if self_message {
                row["author"] = json!("local-aiko");
            }
            chat_history(&dispatch, &[row]);
            let event = dispatch
                .chat
                .list_thread_events("C1", "M1", None, 100)
                .await
                .unwrap()
                .events
                .remove(0);
            dispatch
                .store
                .receive("local", "aiko", event, policy)
                .unwrap();
            dispatch.process(chrono::Utc::now()).await.unwrap();
            assert_eq!(runner.invocations.lock().unwrap().len(), expected);
            if expected == 0 {
                assert!(
                    RunStore::open(&dispatch.store.workspace)
                        .records()
                        .unwrap()
                        .is_empty()
                );
            }
        }
        let temp = tempfile::tempdir().unwrap();
        let (dispatch, runner) = chat_fixture(temp.path());
        chat_history(&dispatch, &[chat_row("M1", "M1", "@local-aiko work", 1)]);
        let event = dispatch
            .chat
            .list_thread_events("C1", "M1", None, 100)
            .await
            .unwrap()
            .events
            .remove(0);
        dispatch
            .store
            .receive("local", "aiko", event, "strict")
            .unwrap();
        runner
            .failures
            .lock()
            .unwrap()
            .push_back(crate::chat_dispatch::Failure::Unavailable);
        dispatch.process(chrono::Utc::now()).await.unwrap();
        let pending = dispatch.store.pending("local", "aiko", "C1").unwrap();
        assert_eq!(pending[0].attempt_count, 0);
        assert_eq!(pending[0].last_error_category, "provider_unavailable");
    }
    #[tokio::test]
    async fn local_chat_dispatch_freezes_latest_batch_and_recovers_completion_without_reinvocation()
    {
        let temp = tempfile::tempdir().unwrap();
        let (dispatch, runner) = chat_fixture(temp.path());
        let old = chat_row("M1", "M1", "@local-aiko original", 1);
        chat_history(&dispatch, &[old]);
        let original = dispatch
            .chat
            .list_thread_events("C1", "M1", None, 100)
            .await
            .unwrap()
            .events
            .remove(0);
        dispatch
            .store
            .receive("local", "aiko", original.clone(), "strict")
            .unwrap();
        chat_history(
            &dispatch,
            &[
                chat_row("M1", "M1", "@local-aiko corrected", 1),
                chat_row("M2", "M1", "followup", 2),
            ],
        );
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
        let calls = runner.invocations.lock().unwrap().clone();
        assert_eq!(calls.len(), 1);
        assert_eq!(
            calls[0].payload["prompt"]["unprocessed_messages"][0]["content"],
            "@aiko corrected"
        );
        assert_eq!(
            calls[0].payload["prompt"]["event_ids"],
            json!(["C1:M1", "C1:M2"])
        );
        assert!(
            dispatch
                .store
                .pending("local", "aiko", "C1")
                .unwrap()
                .is_empty()
        );
        assert!(
            dispatch.store.thread("local", "aiko", "C1", "M1").unwrap()["participants"].is_null()
        );
        let mut retry = crate::chat_state::Pending::new(original, "strict");
        retry.run_id = calls[0].run_id.clone();
        dispatch.store.save("local", "aiko", &retry).unwrap();
        write_json(
            &dispatch
                .store
                .workspace
                .join(".guildbotics/state/chat_state/local/aiko/channels/C1.json"),
            json!({"processed_event_ids":[]}),
        );
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
        assert_eq!(runner.invocations.lock().unwrap().len(), 1);
    }
    #[tokio::test]
    async fn local_chat_dispatch_plain_failure_wakes_for_new_follower_but_rate_limit_does_not() {
        for rate in [false, true] {
            let temp = tempfile::tempdir().unwrap();
            let (dispatch, runner) = chat_fixture(temp.path());
            chat_history(&dispatch, &[chat_row("M1", "M1", "@local-aiko work", 1)]);
            let event = dispatch
                .chat
                .list_thread_events("C1", "M1", None, 100)
                .await
                .unwrap()
                .events
                .remove(0);
            dispatch
                .store
                .receive("local", "aiko", event, "strict")
                .unwrap();
            runner.failures.lock().unwrap().push_back(if rate {
                crate::chat_dispatch::Failure::RateLimited {
                    at: "2099-01-01T00:00:00Z".into(),
                    hint: "later".into(),
                }
            } else {
                crate::chat_dispatch::Failure::Failed
            });
            let now: chrono::DateTime<chrono::Utc> = "2026-11-05T00:00:05Z".parse().unwrap();
            assert_eq!(dispatch.process(now).await.unwrap(), 0);
            assert_eq!(runner.invocations.lock().unwrap().len(), 1);
            let pending = dispatch.store.pending("local", "aiko", "C1").unwrap();
            assert_eq!(pending[0].attempt_count, 1);
            assert!(pending[0].next_attempt_at.is_some());
            chat_history(
                &dispatch,
                &[
                    chat_row("M1", "M1", "@local-aiko work", 1),
                    chat_row("M2", "M1", "wake", 2),
                ],
            );
            let follower = dispatch
                .chat
                .list_thread_events("C1", "M1", None, 100)
                .await
                .unwrap()
                .events
                .remove(1);
            dispatch
                .store
                .receive("local", "aiko", follower, "strict")
                .unwrap();
            assert_eq!(
                dispatch
                    .process(now + chrono::Duration::seconds(1))
                    .await
                    .unwrap(),
                usize::from(!rate)
            );
            assert_eq!(
                runner.invocations.lock().unwrap().len(),
                if rate { 1 } else { 2 }
            );
        }
    }
    #[tokio::test]
    async fn local_chat_dispatch_final_failure_releases_only_the_head_and_announces_the_correct_reason()
     {
        for rate in [false, true] {
            let temp = tempfile::tempdir().unwrap();
            let (dispatch, runner) = chat_fixture(temp.path());
            chat_history(
                &dispatch,
                &[
                    chat_row("M1", "M1", "@local-aiko work", 1),
                    chat_row("M2", "M1", "follower", 2),
                ],
            );
            let events = dispatch
                .chat
                .list_thread_events("C1", "M1", None, 100)
                .await
                .unwrap()
                .events;
            for event in &events {
                dispatch
                    .store
                    .receive("local", "aiko", event.clone(), "strict")
                    .unwrap();
            }
            let mut head = dispatch
                .store
                .pending("local", "aiko", "C1")
                .unwrap()
                .remove(0);
            head.attempt_count = 1;
            head.max_attempts = 2;
            dispatch.store.save("local", "aiko", &head).unwrap();
            runner.failures.lock().unwrap().push_back(if rate {
                crate::chat_dispatch::Failure::RateLimited {
                    at: "2099-01-01T00:00:00Z".into(),
                    hint: "later".into(),
                }
            } else {
                crate::chat_dispatch::Failure::Failed
            });
            dispatch.process(chrono::Utc::now()).await.unwrap();
            let pending = dispatch.store.pending("local", "aiko", "C1").unwrap();
            assert_eq!(pending.len(), 1);
            assert_eq!(pending[0].event.event_id, "C1:M2");
            let notices = dispatch
                .chat
                .list_thread_events("C1", "M1", None, 100)
                .await
                .unwrap()
                .events
                .into_iter()
                .filter_map(|e| e.status)
                .collect::<Vec<_>>();
            assert_eq!(notices.len(), 1);
            assert_eq!(
                notices[0].reason(),
                if rate {
                    WorkflowStatusReason::RateLimited
                } else {
                    WorkflowStatusReason::Failed
                }
            );
        }
    }
    async fn seed_chat(dispatch: &crate::chat_dispatch::Dispatcher, rows: &[Value]) {
        chat_history(dispatch, rows);
        let mut threads = std::collections::BTreeSet::new();
        for row in rows {
            threads.insert(row["thread_id"].as_str().unwrap());
        }
        for thread in threads {
            for event in dispatch
                .chat
                .list_thread_events("C1", thread, None, 100)
                .await
                .unwrap()
                .events
            {
                dispatch
                    .store
                    .receive("local", &dispatch.person, event, "strict")
                    .unwrap();
            }
        }
    }
    #[tokio::test]
    async fn chat_retry_budget_is_durable_due_rejection_and_notices_do_not_spend_extra_attempts() {
        use crate::chat_dispatch::Failure;
        for rate in [false, true] {
            for post_fails in [false, true] {
                let root = tempfile::tempdir().unwrap();
                let (mut dispatch, runner) = chat_fixture(root.path());
                seed_chat(&dispatch, &[chat_row("M1", "M1", "@local-aiko work", 1)]).await;
                let faulty = Arc::new(crate::test_chat::Faults::new(dispatch.chat.clone()));
                if post_fails {
                    faulty
                        .errors
                        .lock()
                        .unwrap()
                        .insert("post:C1".into(), ServiceError::Failed);
                }
                dispatch.chat = faulty;
                let mut queued = dispatch
                    .store
                    .pending("local", "aiko", "C1")
                    .unwrap()
                    .remove(0);
                queued.max_attempts = 3;
                dispatch.store.save("local", "aiko", &queued).unwrap();
                let mut now: chrono::DateTime<chrono::Utc> =
                    "2026-11-05T00:00:05Z".parse().unwrap();
                runner.failures.lock().unwrap().push_back(Failure::Rejected);
                assert_eq!(dispatch.process(now).await.unwrap(), 0);
                assert_eq!(
                    dispatch.store.pending("local", "aiko", "C1").unwrap()[0].attempt_count,
                    0
                );
                let mut identity = String::new();
                for attempt in 1..=3 {
                    runner.failures.lock().unwrap().push_back(if rate {
                        Failure::RateLimited {
                            at: String::new(),
                            hint: "later".into(),
                        }
                    } else {
                        Failure::Failed
                    });
                    assert_eq!(dispatch.process(now).await.unwrap(), 0);
                    let pending = dispatch.store.pending("local", "aiko", "C1").unwrap();
                    if attempt < 3 {
                        assert_eq!(pending.len(), 1);
                        let pending = &pending[0];
                        assert_eq!(pending.attempt_count, attempt);
                        if identity.is_empty() {
                            identity = pending.run_id.clone();
                        }
                        assert_eq!(pending.run_id, identity);
                        let next: chrono::DateTime<chrono::Utc> =
                            pending.next_attempt_at.as_ref().unwrap().parse().unwrap();
                        assert_eq!(
                            next - now,
                            chrono::Duration::seconds(if attempt == 1 { 60 } else { 120 })
                        );
                        let calls = runner.invocations.lock().unwrap().len();
                        assert_eq!(
                            dispatch
                                .process(next - chrono::Duration::seconds(1))
                                .await
                                .unwrap(),
                            0
                        );
                        assert_eq!(runner.invocations.lock().unwrap().len(), calls);
                        now = next;
                    } else {
                        assert!(pending.is_empty());
                    }
                }
                let attempts = runner
                    .invocations
                    .lock()
                    .unwrap()
                    .iter()
                    .map(|i| i.payload["attempt"].as_u64().unwrap())
                    .collect::<Vec<_>>();
                assert_eq!(attempts, vec![1, 1, 2, 3]);
                let events = runner.events.lock().unwrap().clone();
                assert_eq!(
                    events
                        .iter()
                        .filter(|e| e["type"] == "chat_dispatch.retry_scheduled")
                        .count(),
                    2
                );
                let abandoned = events
                    .iter()
                    .find(|e| e["type"] == "chat_dispatch.abandoned")
                    .unwrap();
                assert_eq!(abandoned["attempt"], 3);
                assert_eq!(abandoned["max_attempts"], 3);
                assert_eq!(abandoned["run_id"], identity);
                let state = dispatch.store.thread("local", "aiko", "C1", "M1").unwrap();
                assert_eq!(
                    state["system_notices"].as_array().map_or(0, Vec::len),
                    usize::from(!post_fails)
                );
                let notices = dispatch
                    .chat
                    .list_thread_events("C1", "M1", None, 100)
                    .await
                    .unwrap()
                    .events
                    .into_iter()
                    .filter_map(|e| e.status)
                    .collect::<Vec<_>>();
                assert_eq!(notices.len(), usize::from(!post_fails));
                if let Some(notice) = notices.first() {
                    assert_eq!(
                        notice.reason(),
                        if rate {
                            WorkflowStatusReason::RateLimited
                        } else {
                            WorkflowStatusReason::Failed
                        }
                    );
                }
            }
        }
    }
    #[tokio::test]
    async fn a_failed_head_wakes_once_only_and_other_threads_and_terminal_followers_keep_their_order()
     {
        use crate::chat_dispatch::Failure;
        for category in ["failed", "unknown", "rate_limited"] {
            let root = tempfile::tempdir().unwrap();
            let (dispatch, runner) = chat_fixture(root.path());
            seed_chat(
                &dispatch,
                &[
                    chat_row("M1", "M1", "@local-aiko head", 1),
                    chat_row("M2", "M1", "follower", 2),
                    chat_row("M3", "M3", "@local-aiko other", 3),
                ],
            )
            .await;
            let mut head = dispatch
                .store
                .pending("local", "aiko", "C1")
                .unwrap()
                .remove(0);
            head.attempt_count = 1;
            head.last_error_category = category.into();
            head.next_attempt_at = Some("2099-01-01T00:00:00Z".into());
            dispatch.store.save("local", "aiko", &head).unwrap();
            if category == "failed" {
                runner.failures.lock().unwrap().push_back(Failure::Failed);
            }
            let now = "2026-11-05T00:00:05Z".parse().unwrap();
            assert_eq!(dispatch.process(now).await.unwrap(), 1);
            let calls = runner.invocations.lock().unwrap().clone();
            assert_eq!(
                calls
                    .iter()
                    .map(|i| i.payload["subject"]["event_id"].as_str().unwrap())
                    .collect::<Vec<_>>(),
                if category == "failed" {
                    vec!["C1:M1", "C1:M3"]
                } else {
                    vec!["C1:M3"]
                }
            );
            assert_eq!(dispatch.process(now).await.unwrap(), 0);
            assert_eq!(runner.invocations.lock().unwrap().len(), calls.len());
            let pending = dispatch.store.pending("local", "aiko", "C1").unwrap();
            assert_eq!(pending.len(), 2);
            if category == "failed" {
                assert_eq!(pending[0].wake_cursor, pending[1].event.message.position());
            }
            head = pending[0].clone();
            head.attempt_count = head.max_attempts - 1;
            head.next_attempt_at = None;
            dispatch.store.save("local", "aiko", &head).unwrap();
            runner.failures.lock().unwrap().push_back(Failure::Failed);
            assert_eq!(dispatch.process(now).await.unwrap(), 0);
            assert_eq!(
                dispatch.store.pending("local", "aiko", "C1").unwrap()[0]
                    .event
                    .event_id,
                "C1:M2"
            );
            assert_eq!(dispatch.process(now).await.unwrap(), 1);
            assert!(
                dispatch
                    .store
                    .pending("local", "aiko", "C1")
                    .unwrap()
                    .is_empty()
            );
        }
    }
    #[tokio::test]
    async fn equal_time_threads_sort_by_id_and_already_processed_input_never_calls_the_provider() {
        let root = tempfile::tempdir().unwrap();
        let (mut dispatch, runner) = chat_fixture(root.path());
        seed_chat(
            &dispatch,
            &[
                chat_row("M2", "M2", "@local-aiko second", 1),
                chat_row("M1", "M1", "@local-aiko first", 1),
            ],
        )
        .await;
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 2);
        assert_eq!(
            runner
                .invocations
                .lock()
                .unwrap()
                .iter()
                .map(|i| i.payload["subject"]["event_id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec!["C1:M1", "C1:M2"]
        );
        let mut pending = crate::chat_state::Pending::new(
            dispatch
                .chat
                .list_thread_events("C1", "M1", None, 100)
                .await
                .unwrap()
                .events
                .remove(0),
            "strict",
        );
        pending.run_id = "unused".into();
        dispatch.store.save("local", "aiko", &pending).unwrap();
        let faulty = Arc::new(crate::test_chat::Faults::new(dispatch.chat.clone()));
        faulty
            .errors
            .lock()
            .unwrap()
            .insert("identity:".into(), ServiceError::Failed);
        dispatch.chat = faulty;
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 0);
        assert_eq!(runner.invocations.lock().unwrap().len(), 2);
        assert!(
            dispatch
                .store
                .pending("local", "aiko", "C1")
                .unwrap()
                .is_empty()
        );
    }
    #[tokio::test]
    async fn fresh_followups_preserve_participation_labels_handoff_candidates_and_external_reaction_target()
     {
        for policy in ["strict", "social", "muted"] {
            for other in [false, true] {
                let root = tempfile::tempdir().unwrap();
                let (mut dispatch, runner) = chat_fixture(root.path());
                let rows = json!([
                    {"person_id":"aiko","name":"Aiko","accounts":{"local":"local-aiko"},"roles":{"architect":{}}},
                    {"person_id":"yuki","name":" Yuki ","is_active":false,"accounts":{"local":"local-yuki"},"roles":{"design":{"summary":"Design","description":"UX"}}},
                    {"person_id":"nochat","name":"No chat","accounts":{},"roles":{"design":{}}},
                    {"person_id":"roleless","name":"Roleless","accounts":{"local":"other"},"roles":{}}
                ]);
                dispatch.roster = rows
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|row| {
                        let mut value = serde_json::to_value(&dispatch.roster[0]).unwrap();
                        value
                            .as_object_mut()
                            .unwrap()
                            .extend(row.as_object().unwrap().clone());
                        serde_json::from_value(value).unwrap()
                    })
                    .collect();
                let faulty = Arc::new(crate::test_chat::Faults::new(dispatch.chat.clone()));
                dispatch.chat = faulty.clone();
                chat_history(
                    &dispatch,
                    &[
                        chat_row("M1", "M1", "@local-aiko prior", 1),
                        chat_row(
                            "M2",
                            "M1",
                            if other {
                                "@local-yuki followup"
                            } else {
                                "followup"
                            },
                            2,
                        ),
                        {
                            let mut r = chat_row("M3", "M1", "already finished", 3);
                            r["author"] = json!("local-aiko");
                            r
                        },
                    ],
                );
                let events = dispatch
                    .chat
                    .list_thread_events("C1", "M1", None, 100)
                    .await
                    .unwrap()
                    .events;
                dispatch
                    .store
                    .receive("local", "aiko", events[1].clone(), policy)
                    .unwrap();
                dispatch.store.update_thread("local","aiko","C1","M1",&json!({"effort":"high","thread_topic":"release","latest_focus":"review","handoffs":[{"person_id":"yuki","text":"prior request"}]})).unwrap();
                faulty.calls.lock().unwrap().clear();
                assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
                let calls = runner.invocations.lock().unwrap();
                let expected = !other && policy != "muted";
                assert_eq!(calls.len(), usize::from(expected));
                assert_eq!(
                    faulty
                        .calls
                        .lock()
                        .unwrap()
                        .iter()
                        .filter(|c| c["operation"] == "thread")
                        .count(),
                    1
                );
                if expected {
                    let turn = &calls[0].payload;
                    let prompt = &turn["prompt"];
                    assert_eq!(turn["message_id"], "M2");
                    assert_eq!(turn["effort"], "high");
                    assert!(turn["context_cursor"].as_str().unwrap().ends_with("M3"));
                    assert_eq!(prompt["participant_labels"]["local-aiko"], "aiko");
                    assert_eq!(prompt["participant_labels"]["local-yuki"], "yuki");
                    assert_eq!(
                        prompt["handoff_candidates"],
                        json!([{"person_id":"yuki","name":"Yuki","mention":"@yuki","roles":{"design":{"summary":"Design","description":"UX"}}}])
                    );
                    assert_eq!(
                        prompt["previous_thread_context"]["handoffs"],
                        json!([{"person_id":"yuki","text":"prior request"}])
                    );
                    assert_eq!(prompt["thread_context"][0]["content"], "@aiko prior");
                    assert_eq!(
                        prompt["unprocessed_messages"][1]["content"],
                        "already finished"
                    );
                    assert_eq!(
                        dispatch
                            .store
                            .messages("local", "aiko", "C1", "M1")
                            .unwrap()
                            .len(),
                        3
                    );
                }
            }
        }
    }
    #[tokio::test]
    async fn each_member_can_dispatch_the_same_event_and_thread_identity_survives_new_input() {
        let root = tempfile::tempdir().unwrap();
        let (dispatch, runner) = chat_fixture(root.path());
        seed_chat(&dispatch, &[chat_row("M1", "M1", "@local-aiko first", 1)]).await;
        let mut other = crate::chat_dispatch::Dispatcher {
            person: "yuki".into(),
            service: "local".into(),
            language: "en".into(),
            roster: dispatch.roster.clone(),
            chat: dispatch.chat.clone(),
            store: dispatch.store.clone(),
            runner: runner.clone(),
        };
        let event = dispatch.store.pending("local", "aiko", "C1").unwrap()[0]
            .event
            .clone();
        other
            .store
            .receive("local", "yuki", event, "strict")
            .unwrap();
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
        assert_eq!(other.process(chrono::Utc::now()).await.unwrap(), 1);
        assert!(
            other
                .store
                .pending("local", "yuki", "C1")
                .unwrap()
                .is_empty()
        );
        other.person = "aiko".into();
        chat_history(
            &dispatch,
            &[
                chat_row("M1", "M1", "@local-aiko first", 1),
                chat_row("M2", "M1", "second", 2),
            ],
        );
        let event = dispatch
            .chat
            .list_thread_events("C1", "M1", None, 100)
            .await
            .unwrap()
            .events
            .remove(1);
        dispatch
            .store
            .receive("local", "aiko", event, "strict")
            .unwrap();
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
        let calls = runner.invocations.lock().unwrap();
        assert_eq!(
            calls
                .iter()
                .map(|i| i.person_id.as_str())
                .collect::<Vec<_>>(),
            vec!["aiko", "yuki", "aiko"]
        );
        assert_eq!(
            calls[0].work.as_ref().unwrap().identity,
            calls[2].work.as_ref().unwrap().identity
        );
        assert!(
            calls[0].payload["context_cursor"].as_str().unwrap()
                < calls[2].payload["context_cursor"].as_str().unwrap()
        );
    }
    #[tokio::test]
    async fn native_chat_retry_events_keep_command_trace_rate_metadata_and_capped_abandonment() {
        use crate::chat_dispatch::Runner as _;
        let root = tempfile::tempdir().unwrap();
        let native = fixture(root.path());
        let cleanup = tokio_util::task::TaskTracker::new();
        let (runtime, _) = workflow_fixture(&native, root.path(), &cleanup, false);
        let (dispatch, _) = chat_fixture(root.path());
        seed_chat(&dispatch, &[chat_row("M1", "M1", "@local-aiko work", 1)]).await;
        let mut pending = dispatch
            .store
            .pending("local", "aiko", "C1")
            .unwrap()
            .remove(0);
        pending.run_id = "event-run".into();
        pending.attempt_count = 99;
        pending.max_attempts = 3;
        let ((), logs) = crate::test_logs::capture(|| {
            runtime.event(
                "chat_dispatch.retry_scheduled",
                &pending,
                json!({"next_attempt_at":"later","error_category":"failed"}),
            );
            runtime.event("workflow.rate_limited",&pending,json!({"category":"rate_limited","retry_after_at":"2027-01-01T00:00:00Z","retry_after_text":"later","source_event_id":"C1:M1","subject_id":"C1:M1"}));
            runtime.event(
                "chat_dispatch.abandoned",
                &pending,
                json!({"error_category":"failed"}),
            );
        });
        assert_eq!(logs.len(), 2);
        assert!(logs[0].contains("event-run"));
        assert!(logs[1].starts_with("chat event abandoned after final attempt"));
        assert!(logs[1].contains("aiko"));
        let diagnostics = std::fs::read_to_string(
            native
                .workspace
                .join(".guildbotics/local/run/diagnostics.jsonl"),
        )
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|e| e["trace_id"] == "event-run" && e["type"] != "session.pointer")
        .collect::<Vec<_>>();
        assert_eq!(diagnostics.len(), 3);
        for e in &diagnostics {
            assert_eq!(e["person_id"], "aiko");
            assert_eq!(e["command"], crate::chat_dispatch::COMMAND);
            assert_eq!(e["source"], "event_queue");
            assert_eq!(e["payload"]["attempt_count"], 3);
            assert_eq!(e["payload"]["max_attempts"], 3);
            assert_eq!(
                e["attributes"]["service_run_id"],
                runtime.generation.as_ref().unwrap().id
            );
            assert_eq!(e["attributes"]["chat.thread"], "M1");
        }
        assert_eq!(
            diagnostics[1]["attributes"]["error.category"],
            "rate_limited"
        );
        assert_eq!(
            diagnostics[1]["attributes"]["rate_limit.retry_after_at"],
            "2027-01-01T00:00:00Z"
        );
        let activity = gb_observability::activity::ActivityEvents::open(&native.workspace)
            .list_between(
                "2020-01-01T00:00:00Z".parse().unwrap(),
                "2099-01-01T00:00:00Z".parse().unwrap(),
            )
            .unwrap();
        let rate = activity
            .iter()
            .find(|e| e["kind"] == "workflow.rate_limited")
            .unwrap();
        assert_eq!(rate["attributes"]["error.category"], "rate_limited");
        assert_eq!(rate["payload"]["source_event_id"], "C1:M1");
        assert_eq!(rate["command"], crate::chat_dispatch::COMMAND);
        cleanup.close();
        cleanup.wait().await;
    }
    #[tokio::test]
    async fn recovered_reply_records_only_known_team_handoffs_and_preserves_topic_focus_and_effort()
    {
        let root = tempfile::tempdir().unwrap();
        let (mut dispatch, runner) = chat_fixture(root.path());
        let mut peer = dispatch.roster[0].clone();
        peer.person_id = "yuki".into();
        peer.name = "Yuki".into();
        peer.accounts = BTreeMap::from([("local".into(), "local-yuki".into())]);
        peer.roles = json!({"design":{}});
        dispatch.roster.push(peer);
        seed_chat(&dispatch, &[chat_row("M1", "M1", "@local-aiko work", 1)]).await;
        let mut original = dispatch
            .store
            .pending("local", "aiko", "C1")
            .unwrap()
            .remove(0);
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
        original.run_id = runner.invocations.lock().unwrap()[0].run_id.clone();
        // This runner double leaves the host terminal event to the fixture.
        crate::chat_dispatch::Runner::event(
            runner.as_ref(),
            "workflow.completed",
            &original,
            json!({"run_id":original.run_id,"status":"done"}),
        );
        runner.records.lock().unwrap().get_mut(&original.run_id).unwrap().provider_evidence.push(json!({"type":"chat_reply","payload":{"channel_id":"C1","thread_id":"M1","message_id":"response","text":"@local-yuki design review @outsider","occurred_at":"2026-11-05T00:00:05Z"}}));
        dispatch
            .store
            .update_thread(
                "local",
                "aiko",
                "C1",
                "M1",
                &json!({"thread_topic":"release","latest_focus":"review","effort":"high"}),
            )
            .unwrap();
        dispatch.store.save("local", "aiko", &original).unwrap();
        write_json(
            &dispatch
                .store
                .workspace
                .join(".guildbotics/state/chat_state/local/aiko/channels/C1.json"),
            json!({"processed_event_ids":[]}),
        );
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 1);
        assert_eq!(runner.invocations.lock().unwrap().len(), 1);
        let state = dispatch.store.thread("local", "aiko", "C1", "M1").unwrap();
        assert_eq!(
            state["handoffs"],
            json!([{"person_id":"yuki","roles":["design"],"message_id":"response","text":"@local-yuki design review @outsider","thread_topic":"release","latest_focus":"review"}])
        );
        assert_eq!(state["effort"], "high");
        assert!(
            state["participants"]
                .as_array()
                .unwrap()
                .contains(&json!("aiko"))
        );
        assert_eq!(
            dispatch
                .store
                .messages("local", "aiko", "C1", "M1")
                .unwrap()
                .last()
                .unwrap()
                .message_id,
            "response"
        );
        assert_eq!(
            runner
                .events
                .lock()
                .unwrap()
                .iter()
                .filter(|event| event["type"] == "workflow.completed")
                .count(),
            1
        );
    }
    #[tokio::test]
    async fn native_patrol_keeps_distinct_candidates_after_failure_and_lease_conflict_then_records_cancellation()
     {
        use crate::scheduler::Engine as _;
        let root = tempfile::tempdir().unwrap();
        let native = fixture(root.path());
        let cleanup = tokio_util::task::TaskTracker::new();
        let (mut runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
        let first = ready_ticket(&native, &runtime).await;
        let second = ready_ticket(&native, &runtime).await;
        let faults = Arc::new(crate::test_ticket::Faults::new(
            runtime.tickets.clone().unwrap(),
        ));
        runtime.tickets = Some(faults.clone());
        let runtime = Arc::new(runtime);
        let engine = crate::native_scheduler::Engine::new(runtime.clone(), None);
        let slot = || {
            crate::scheduler::slot(
                "routine",
                "aiko",
                crate::ticket_selector::COMMAND,
                "2026-11-05T12:00:00".parse().unwrap(),
            )
        };
        let lease_path = native
            .workspace
            .join(".guildbotics/local/run/person-leases/aiko.lock");
        let lease = gb_base::files::FileLock::try_hold(&lease_path)
            .unwrap()
            .unwrap();
        assert_eq!(
            engine.patrol(slot()).await,
            (crate::service::Outcome::Deferred, false)
        );
        assert!(driver.calls.lock().unwrap().is_empty());
        assert!(!runtime.draining.is_cancelled());
        drop(lease);
        driver
            .failures
            .lock()
            .unwrap()
            .push_back(crate::dispatcher::DispatchFailure::Service(
                ServiceError::Failed,
            ));
        assert_eq!(
            engine.patrol(slot()).await,
            (crate::service::Outcome::Failed, false)
        );
        let second_path = native
            .workspace
            .join(".guildbotics/local/services/tickets/acme%2Fteam/T-2.json");
        let mut updated: Value =
            serde_json::from_slice(&std::fs::read(&second_path).unwrap()).unwrap();
        updated["lane"] = json!("in_progress");
        updated["title"] = json!("Updated title");
        write_json(&second_path, updated);
        faults
            .errors
            .lock()
            .unwrap()
            .insert("move", ServiceError::Failed);
        assert_eq!(
            engine.patrol(slot()).await,
            (crate::service::Outcome::Succeeded, true)
        );
        let records = RunStore::open(&native.workspace).records().unwrap();
        assert_eq!(records.len(), 2);
        assert_eq!(
            records
                .iter()
                .map(|r| r.work_identity.as_ref().unwrap()["ticket_url"].as_str())
                .collect::<std::collections::BTreeSet<_>>(),
            [first.target.url.as_str(), second.target.url.as_str()]
                .into_iter()
                .collect()
        );
        assert_eq!(
            records
                .iter()
                .filter(|r| r.status == gb_observability::runs::RunState::Failed)
                .count(),
            1
        );
        for r in &records {
            assert_eq!(r.source, "routine");
            assert_eq!(
                r.attributes["target.title"],
                if r.work_identity.as_ref().unwrap()["ticket_url"] == second.target.url {
                    "Updated title"
                } else {
                    "Fix login"
                }
            );
            assert!(r.finished_at.is_some());
        }
        for source in ["scheduled", "routine"] {
            driver
                .failures
                .lock()
                .unwrap()
                .push_back(crate::dispatcher::DispatchFailure::Cancelled);
            let outcome = engine
                .command(
                    "functions/talk_as",
                    source,
                    crate::scheduler::slot(
                        source,
                        "aiko",
                        "functions/talk_as",
                        "2026-11-05T12:01:00".parse().unwrap(),
                    ),
                )
                .await;
            assert_eq!(outcome, crate::service::Outcome::Deferred);
            let id = driver.calls.lock().unwrap().last().unwrap().run_id.clone();
            assert_eq!(
                RunStore::open(&native.workspace)
                    .read(&id)
                    .unwrap()
                    .unwrap()
                    .status,
                gb_observability::runs::RunState::Cancelled
            );
        }
        cleanup.close();
        cleanup.wait().await;
        assert!(runtime.generation.as_ref().unwrap().snapshot().is_empty());
    }
    #[tokio::test]
    async fn failed_native_chat_batches_refresh_edited_and_new_input_and_preserve_prior_action_evidence()
     {
        for policy in ["strict", "muted"] {
            for wrote in [false, true] {
                let root = tempfile::tempdir().unwrap();
                let native = fixture(root.path());
                let cleanup = tokio_util::task::TaskTracker::new();
                let (runtime, driver) = workflow_fixture(&native, root.path(), &cleanup, false);
                let runtime = Arc::new(runtime);
                let dispatch = crate::chat_dispatch::Dispatcher {
                    person: "aiko".into(),
                    service: "local".into(),
                    language: "en".into(),
                    roster: runtime.roster.clone(),
                    chat: runtime.chat.clone().unwrap(),
                    store: Arc::new(crate::chat_state::Store {
                        workspace: native.workspace.clone(),
                        writes: runtime.writes.clone(),
                        limit: 500,
                    }),
                    runner: runtime.clone(),
                };
                seed_chat(
                    &dispatch,
                    &[
                        chat_row("M1", "M1", "@local-aiko original", 1),
                        chat_row("M2", "M1", "correction", 2),
                    ],
                )
                .await;
                let mut head = dispatch
                    .store
                    .pending("local", "aiko", "C1")
                    .unwrap()
                    .remove(0);
                head.chat_participation = policy.into();
                dispatch.store.save("local", "aiko", &head).unwrap();
                let failure = crate::dispatcher::DispatchFailure::Service(ServiceError::Failed);
                if wrote {
                    *driver.before_completion.lock().unwrap() = Some(failure);
                } else {
                    driver.failures.lock().unwrap().push_back(failure);
                }
                let subject = gb_member::chat_updates::Subject {
                    service: "local".into(),
                    channel_id: "C1".into(),
                    thread_id: "M1".into(),
                    event_id: "C1:M1".into(),
                    self_user_id: "local-aiko".into(),
                };
                let heartbeat = gb_member::chat_updates::Heartbeat::new(&native.workspace);
                heartbeat
                    .save(
                        &subject,
                        "aiko",
                        "ready",
                        chrono::Utc::now().timestamp_millis() as f64 / 1000.0,
                        1.0,
                    )
                    .unwrap();
                let now = chrono::Utc::now();
                assert_eq!(dispatch.process(now).await.unwrap(), 0);
                let id = driver.calls.lock().unwrap()[0].run_id.clone();
                let prior = RunStore::open(&native.workspace)
                    .read(&id)
                    .unwrap()
                    .unwrap();
                assert_eq!(prior.status, gb_observability::runs::RunState::Failed);
                assert!(prior.result.is_none());
                assert_eq!(
                    prior
                        .provider_evidence
                        .iter()
                        .any(|e| e["type"] == "chat_reply"),
                    wrote
                );
                assert_eq!(
                    dispatch.store.pending("local", "aiko", "C1").unwrap().len(),
                    2
                );
                chat_history(
                    &dispatch,
                    &[
                        chat_row("M1", "M1", "@local-aiko edited", 1),
                        chat_row("M2", "M1", "correction", 2),
                        chat_row("M3", "M1", "new work", 3),
                    ],
                );
                assert_eq!(
                    dispatch
                        .process(now + chrono::Duration::seconds(61))
                        .await
                        .unwrap(),
                    1
                );
                let calls = driver.calls.lock().unwrap().clone();
                assert_eq!(calls.len(), 2);
                assert_eq!(calls[1].run_id, id);
                assert_eq!(
                    calls[1].payload["prompt"]["unprocessed_messages"][0]["content"],
                    "@aiko edited"
                );
                assert_eq!(
                    calls[1].payload["prompt"]["event_ids"],
                    json!(["C1:M1", "C1:M2", "C1:M3"])
                );
                let record = RunStore::open(&native.workspace)
                    .read(&id)
                    .unwrap()
                    .unwrap();
                assert_eq!(
                    record
                        .provider_evidence
                        .iter()
                        .filter(|e| e["type"] == "chat_batch")
                        .map(|e| e["payload"]["event_ids"].as_array().unwrap().len())
                        .collect::<Vec<_>>(),
                    vec![2, 3]
                );
                assert_eq!(
                    record
                        .provider_evidence
                        .iter()
                        .filter(|e| e["type"] == "chat_reply")
                        .count(),
                    if wrote { 2 } else { 1 }
                );
                assert!(record.result.is_some());
                assert!(
                    dispatch
                        .store
                        .pending("local", "aiko", "C1")
                        .unwrap()
                        .is_empty()
                );
                cleanup.close();
                cleanup.wait().await;
            }
        }
    }
    #[tokio::test]
    async fn initial_chat_budget_reads_the_process_environment_without_mutating_the_test_runtime() {
        const CHILD: &str = "GUILDBOTICS_TEST_CHAT_BUDGET";
        if std::env::var_os(CHILD).is_none() {
            let output=std::process::Command::new(std::env::current_exe().unwrap()).args(["--exact","member::native::tests::initial_chat_budget_reads_the_process_environment_without_mutating_the_test_runtime","--nocapture"]).env(CHILD,"1").env("GUILDBOTICS_CHAT_MAX_ATTEMPTS","10").output().unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            return;
        }
        let root = tempfile::tempdir().unwrap();
        let (dispatch, runner) = chat_fixture(root.path());
        seed_chat(&dispatch, &[chat_row("M1", "M1", "@local-aiko work", 1)]).await;
        runner
            .failures
            .lock()
            .unwrap()
            .push_back(crate::chat_dispatch::Failure::Failed);
        assert_eq!(dispatch.process(chrono::Utc::now()).await.unwrap(), 0);
        let pending = dispatch.store.pending("local", "aiko", "C1").unwrap();
        assert_eq!(pending[0].max_attempts, 10);
        assert_eq!(pending[0].attempt_count, 1);
    }
    impl EventListener for Received {
        fn connected(&self) -> bool {
            self.connected.load(std::sync::atomic::Ordering::SeqCst)
        }
        fn auth_failed(&self) -> bool {
            false
        }
        fn start(&self) -> ServiceFuture<'_, ()> {
            Box::pin(async {
                self.starts
                    .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                self.connected
                    .store(true, std::sync::atomic::Ordering::SeqCst);
                Ok(())
            })
        }
        fn stop(&self) -> ServiceFuture<'_, ()> {
            Box::pin(async {
                self.stops.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                self.connected
                    .store(false, std::sync::atomic::Ordering::SeqCst);
                Ok(())
            })
        }
        fn drain_events(&self) -> Vec<ChatEvent> {
            std::mem::take(&mut *self.events.lock().unwrap())
        }
    }
    #[tokio::test]
    async fn local_chat_receiver_backfills_threads_suppresses_status_and_retains_live_input_during_write_contention()
     {
        use crate::{chat_receiver::Receiver, chat_state::Store, work_stops::subscriptions};
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: temp.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let cleanup = tokio_util::task::TaskTracker::new();
        let (runtime, _) = crate::workflow_runtime::Runtime::assemble(
            &native,
            host,
            "aiko",
            cleanup.clone(),
            Default::default(),
            Default::default(),
        )
        .unwrap();
        let chat = runtime.chat.unwrap();
        let root = chat.post_message("C1", "Root", None, None).await.unwrap();
        chat.post_message("C1", "Reply", Some(&root.thread_id), None)
            .await
            .unwrap();
        let status: WorkflowStatus = serde_json::from_value(
            json!({"reason":"failed","person_id":"aiko","run_id":"old","subject":"ticket"}),
        )
        .unwrap();
        let notice = chat
            .post_message("C1", "Stopped", None, Some(&status))
            .await
            .unwrap();
        let store = Arc::new(Store {
            workspace: native.workspace.clone(),
            writes: runtime.writes.clone(),
            limit: 500,
        });
        store
            .update_thread(
                "local",
                "aiko",
                "C1",
                &root.thread_id,
                &json!({"participants":["owner"],"thread_topic":"keep this"}),
            )
            .unwrap();
        let messages = chat
            .list_thread_events("C1", &root.thread_id, None, 100)
            .await
            .unwrap()
            .events;
        store.cache("local", "aiko", &messages[0].message).unwrap();
        assert_eq!(
            store
                .thread("local", "aiko", "C1", &root.thread_id)
                .unwrap()["thread_topic"],
            "keep this"
        );
        store
            .update_thread("local", "aiko", "C1", "missing", &json!({}))
            .unwrap();
        let listener = Arc::new(Received::new());
        let connection = crate::chat_receiver::Connection {
            service: "local".into(),
            key: "fixture".into(),
            listener: listener.clone(),
            wake: Default::default(),
        };
        let policy = subscriptions(
            &json!({"message_channels":[{"chat":{"channel_id":"C1","backfill_limit":1,"startup_backfill_minutes":60}}]}),
        );
        let receiver = Receiver::new(
            "aiko".into(),
            chat.clone(),
            connection.clone(),
            policy,
            store.clone(),
        )
        .await
        .unwrap();
        receiver
            .backfill(&receiver.channels.lock().await[0], chrono::Utc::now())
            .await
            .unwrap();
        assert_eq!(store.pending("local", "aiko", "C1").unwrap().len(), 2);
        assert!(
            store.cursor("local", "aiko", "C1").unwrap()["processed_event_ids"]
                .as_array()
                .unwrap()
                .contains(&json!(format!("C1:{}", notice.message_id)))
        );
        assert_eq!(
            store.thread("local", "aiko", "C1", "missing").unwrap()["backfill_disabled_reason"],
            "thread_not_found"
        );
        let ids = store
            .pending("local", "aiko", "C1")
            .unwrap()
            .into_iter()
            .map(|p| p.event.event_id)
            .collect::<Vec<_>>();
        store.acknowledge("local", "aiko", "C1", &ids).unwrap();
        write_json(
            &native
                .workspace
                .join(".guildbotics/state/chat_state/local/aiko/receive_cutoff.json"),
            json!({"cutoff":(chrono::Utc::now()+chrono::Duration::hours(1)).to_rfc3339()}),
        );
        receiver
            .backfill(&receiver.channels.lock().await[0], chrono::Utc::now())
            .await
            .unwrap();
        assert!(store.pending("local", "aiko", "C1").unwrap().is_empty());
        let mut live = messages[0].clone();
        live.event_id = "live".into();
        live.message.message_id = "live".into();
        listener.events.lock().unwrap().push(live);
        let writes = store.writes.clone();
        let (held_tx, held_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let holder = tokio::task::spawn_blocking(move || {
            writes.transaction(|_| {
                held_tx.send(()).unwrap();
                release_rx.recv().unwrap();
                Ok(())
            })
        });
        held_rx.await.unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(1), receiver.drain())
            .await
            .unwrap();
        assert!(
            !store
                .pending("local", "aiko", "C1")
                .unwrap()
                .iter()
                .any(|p| p.event.event_id == "live")
        );
        release_tx.send(()).unwrap();
        holder.await.unwrap().unwrap();
        receiver.drain().await;
        assert_eq!(
            store
                .pending("local", "aiko", "C1")
                .unwrap()
                .iter()
                .filter(|p| p.event.event_id == "live")
                .count(),
            1
        );
        let stop = tokio_util::sync::CancellationToken::new();
        stop.cancel();
        receiver.run(stop).await.unwrap();
        assert!(!listener.connected());
        let primary = Arc::new(receiver);
        let second = Arc::new(
            Receiver::new(
                "yuki".into(),
                chat.clone(),
                connection,
                primary.channels.lock().await.clone(),
                store.clone(),
            )
            .await
            .unwrap(),
        );
        listener.start().await.unwrap();
        let mut event = messages[0].clone();
        event.event_id = "fanout".into();
        event.message.message_id = "fanout".into();
        listener.events.lock().unwrap().push(event);
        let group = crate::chat_receiver::Group::new(vec![primary, second]).unwrap();
        group.drain().await;
        for person in ["aiko", "yuki"] {
            assert_eq!(
                store
                    .pending("local", person, "C1")
                    .unwrap()
                    .iter()
                    .filter(|p| p.event.event_id == "fanout")
                    .count(),
                1
            );
        }
        let stop = tokio_util::sync::CancellationToken::new();
        stop.cancel();
        group.run(stop).await.unwrap();
        assert!(!listener.connected());

        cleanup.close();
        cleanup.wait().await;
    }
    async fn http(native: Native, request: MemberRequest, path: &str) -> Value {
        let root = tempfile::tempdir().unwrap();
        let api = Arc::new(crate::api::Api::new(
            crate::api::Selected {
                member: Some(Arc::new(Backend {
                    version: "fixture".into(),
                    workspace_id: "workspace".into(),
                    executor: Arc::new(native),
                    timeout: Duration::from_secs(30),
                })),
                ..Default::default()
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            crate::auth::ServerAuth::open(root.path(), 8765).unwrap(),
            "fixture".into(),
            root.path().into(),
        ));
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        // MemberRequest is input-only; construct the wire object explicitly.
        let value = json!({"version":request.version,"workspace_id":request.workspace_id,"arguments":request.arguments,"stdin":request.stdin,"interactive_host":request.interactive_host,"interactive_thread_key":request.interactive_thread_key});
        let response = crate::api::router(api.clone())
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri(path)
                    .header("host", "127.0.0.1:8765")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {}", token.trim()))
                    .body(axum::body::Body::from(value.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::OK);
        let bytes = axum::body::to_bytes(response.into_body(), 4 << 20)
            .await
            .unwrap();
        api.work.close();
        api.work.wait().await;
        serde_json::from_slice(&bytes).unwrap()
    }
    #[tokio::test]
    async fn native_http_runs_memory_commands_and_plan_uses_the_same_grammar() {
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        let result = http(
            native.clone(),
            request(
                &[
                    "memory",
                    "record",
                    "--person",
                    "aiko",
                    "--title",
                    "note",
                    "--scope",
                    "team",
                    "--content-stdin",
                ],
                "本文\n",
            ),
            "/-/guildbotics/api/member/run",
        )
        .await;
        assert_eq!(result["exit_code"], 0, "{result}");
        let payload: Value = serde_json::from_str(result["stdout"].as_str().unwrap()).unwrap();
        let id = payload["doc_id"].as_str().unwrap();
        let result = http(
            native.clone(),
            request(
                &["memory", "get", "--person", "aiko", "--id", id, "--team"],
                "",
            ),
            "/-/guildbotics/api/member/run",
        )
        .await;
        assert_eq!(result["exit_code"], 0, "{result}");
        let payload: Value = serde_json::from_str(result["stdout"].as_str().unwrap()).unwrap();
        assert_eq!(payload["body"], "本文\n");
        let result = http(
            native,
            request(
                &[
                    "ticket",
                    "comment",
                    "--person",
                    "aiko",
                    "--url",
                    "url",
                    "--content-file",
                    "caller.md",
                ],
                "",
            ),
            "/-/guildbotics/api/member/plan",
        )
        .await;
        assert_eq!(result["plan"]["content_file"], "caller.md");
        assert_eq!(result["plan"]["arguments"][6], "--content-stdin");
    }
    #[tokio::test]
    async fn waiting_recipients_need_patrol_and_fall_back_from_code_to_tickets() {
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        let writes =
            SharedWrites::new(native.identity_root.clone(), Some(native.workspace.clone()));
        let secrets = SecretStore::new(
            &writes,
            Scope::Identity,
            DeviceKey {
                machine_root: native.data.parent().unwrap().into(),
                keychain: native.keychain.as_ref(),
            },
        )
        .unwrap();
        let config = IntegrationConfig {
            local_root: temp.path().join("services"),
            identity_root: native.identity_root.clone(),
            person_id: "aiko".into(),
            display_name: "Aiko".into(),
            accounts: Default::default(),
            branvil_members: Default::default(),
            ticket_provider: "local".into(),
            code_provider: "local".into(),
            chat_provider: "local".into(),
            ticket_scope: "acme".into(),
            code_scope: "acme".into(),
            checks: String::new(),
            slack_base_url: String::new(),
        };
        write_json(
            &config.local_root.join("tickets/acme%2Fdemo/tracker.json"),
            json!({"repository":"acme/demo","labels":[]}),
        );
        let factory =
            ServiceIntegrationFactory::new(config.clone(), &secrets, None, Arc::new(|_| {}))
                .unwrap();
        let tickets = factory.create_ticket_service().unwrap();
        let ticket = tickets
            .create_ticket(CreateTicket {
                tracker: "acme/demo".into(),
                title: "assigned".into(),
                body: String::new(),
                labels: vec![],
                add_to_board: true,
                interactive: false,
                assignee: Some(ActorName("local-aiko".into())),
                lane: Some(CreateLane::Ready),
                parent: None,
                blocked_by: vec![],
            })
            .await
            .unwrap();
        let roster = [PublicMember {
            person_id: "aiko".into(),
            name: "Aiko".into(),
            person_type: "agent".into(),
            is_active: true,
            roles: json!({}),
            profile: json!({}),
            speaking_style: String::new(),
            accounts: Default::default(),
        }];
        let mut peers = Peers {
            config: &config,
            secrets: &secrets,
            branvil: None,
            roster: &roster,
            eligible: Default::default(),
        };
        assert!(!peers.receives("aiko", &ticket.target.url).await.unwrap());
        peers.eligible.insert("aiko".into());
        assert!(peers.receives("aiko", &ticket.target.url).await.unwrap());
    }
    #[tokio::test]
    async fn ticket_mentions_keep_their_origin_in_completion_and_status() {
        let temp = tempfile::tempdir().unwrap();
        let mut native = fixture(temp.path());
        write_json(
            &native
                .workspace
                .join(".guildbotics/local/services/tickets/acme%2Fdemo/tracker.json"),
            json!({"repository":"acme/demo","labels":[]}),
        );
        let ticket = command(
            &native,
            &[
                "ticket",
                "create",
                "--person",
                "aiko",
                "--tracker",
                "acme/demo",
                "--title",
                "T",
                "--human-approved",
                "--content-stdin",
            ],
            "body",
        )
        .await;
        let url = ticket["target"]["url"].as_str().unwrap();
        native.invocation = Some(gb_environment::broker::Invocation {
            chat_subject: None,
            person_id: "aiko".into(),
            run_id: "mention".into(),
            trace_id: "mention".into(),
            work_kind: "ticket_mention".into(),
            work_identity: url.into(),
            cwd: native.workspace.clone(),
            access: gb_wire::CommandAccess {
                read_only: false,
                inspects: vec![],
            },
        });
        command(
            &native,
            &[
                "ticket",
                "comment",
                "--person",
                "aiko",
                "--url",
                url,
                "--content-stdin",
            ],
            "answered",
        )
        .await;
        let answer = command(
            &native,
            &[
                "task",
                "complete",
                "--person",
                "aiko",
                "--status",
                "blocked",
                "--content-stdin",
            ],
            "reason",
        )
        .await;
        assert_eq!(answer["subject_url"], url);
        let status = command(&native, &["task", "status", "--person", "aiko"], "").await;
        assert_eq!(status["subject_url"], url);
    }
    #[tokio::test]
    async fn native_assembly_uses_four_character_memory_redaction_without_disclosing_secrets_in_context()
     {
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        let writes =
            SharedWrites::new(native.identity_root.clone(), Some(native.workspace.clone()));
        let store = SecretStore::new(
            &writes,
            Scope::Identity,
            DeviceKey {
                machine_root: native.data.parent().unwrap().into(),
                keychain: native.keychain.as_ref(),
            },
        )
        .unwrap();
        for (key, value) in [
            ("TEST_SENTINEL", "FOUR"),
            ("TEST_FIVE", "FIVES"),
            ("TEST_SIX", "SIXVAL"),
            ("TEST_SEVEN", "SEVENIT"),
        ] {
            store.set(key, value).unwrap();
        }
        let output = temp.path().join("out");
        let reply = native
            .execute(
                request(
                    &[
                        "memory",
                        "record",
                        "--person",
                        "aiko",
                        "--title",
                        "FOUR",
                        "--content-stdin",
                    ],
                    "prefix FOUR FIVES SIXVAL SEVENIT suffix",
                ),
                false,
                None,
                &output,
            )
            .await;
        assert_eq!(reply.exit_code, 0, "{}", reply.stderr);
        for sentinel in ["FOUR", "FIVES", "SIXVAL", "SEVENIT"] {
            assert!(!reply.stdout.contains(sentinel));
        }
        let context = native
            .execute(
                request(&["context", "--person", "aiko"], ""),
                false,
                None,
                &output,
            )
            .await;
        assert_eq!(context.exit_code, 0, "{}", context.stderr);
        assert!(!context.stdout.contains("FOUR"));
        assert!(!context.stdout.contains("TEST_SENTINEL"));
        let payload: Value = serde_json::from_str(&reply.stdout).unwrap();
        let id = payload["doc_id"].as_str().unwrap();
        let reply = native
            .execute(
                request(&["memory", "get", "--person", "aiko", "--id", id], ""),
                false,
                None,
                &output,
            )
            .await;
        assert_eq!(reply.exit_code, 0, "{}", reply.stderr);
        for sentinel in ["FOUR", "FIVES", "SIXVAL", "SEVENIT"] {
            assert!(!reply.stdout.contains(sentinel));
        }
        assert!(reply.stdout.contains("prefix"));
    }
    #[tokio::test]
    async fn bound_read_only_invocations_cannot_write_and_no_caller_file_is_read() {
        let temp = tempfile::tempdir().unwrap();
        let mut native = fixture(temp.path());
        let output = temp.path().join("out");
        let reply = native
            .execute(
                request(
                    &[
                        "memory",
                        "record",
                        "--person",
                        "aiko",
                        "--title",
                        "note",
                        "--content-file",
                        "/caller/must-not-open",
                    ],
                    "",
                ),
                false,
                None,
                &output,
            )
            .await;
        assert_ne!(reply.exit_code, 0);
        assert!(
            !native
                .workspace
                .join(".guildbotics/state/documents")
                .exists()
        );
        native.invocation = Some(gb_environment::broker::Invocation {
            chat_subject: None,
            person_id: "aiko".into(),
            run_id: "run".into(),
            trace_id: "run".into(),
            work_kind: "command".into(),
            work_identity: "work".into(),
            cwd: "/guest".into(),
            access: gb_wire::CommandAccess {
                read_only: true,
                inspects: vec![],
            },
        });
        let reply = native
            .execute(
                request(
                    &[
                        "memory",
                        "record",
                        "--person",
                        "aiko",
                        "--title",
                        "note",
                        "--content-stdin",
                    ],
                    "body",
                ),
                false,
                None,
                &output,
            )
            .await;
        assert_ne!(reply.exit_code, 0);
        assert!(
            !native
                .workspace
                .join(".guildbotics/state/documents")
                .exists()
        );
    }
    async fn command(native: &Native, arguments: &[&str], stdin: &str) -> Value {
        let reply = http(
            native.clone(),
            request(arguments, stdin),
            "/-/guildbotics/api/member/run",
        )
        .await;
        assert_eq!(reply["exit_code"], 0, "{reply}");
        serde_json::from_str(reply["stdout"].as_str().unwrap()).unwrap()
    }
    fn write_json(path: &Path, value: Value) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, value.to_string()).unwrap();
    }
    fn git(path: &Path, args: &[&str]) -> String {
        let output = std::process::Command::new("git")
            .args(args)
            .current_dir(path)
            .env("GIT_AUTHOR_NAME", "Fixture")
            .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
            .env("GIT_COMMITTER_NAME", "Fixture")
            .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap().trim().into()
    }
    pub(crate) fn seed_code(native: &Native, root: &Path) -> (PathBuf, PathBuf, String) {
        let seed = root.join("seed");
        std::fs::create_dir(&seed).unwrap();
        git(&seed, &["init", "-qb", "main"]);
        std::fs::write(seed.join("file.txt"), "base\n").unwrap();
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-qm", "base"]);
        git(&seed, &["checkout", "-qb", "feature"]);
        std::fs::write(seed.join("file.txt"), "base\nadded\n").unwrap();
        git(&seed, &["commit", "-qam", "feature"]);
        let head = git(&seed, &["rev-parse", "HEAD"]);
        let bare = native
            .workspace
            .join(".guildbotics/local/services")
            .join("code/acme%2Fdemo/repository.git");
        std::fs::create_dir_all(bare.parent().unwrap()).unwrap();
        git(
            &seed,
            &[
                "clone",
                "-q",
                "--bare",
                seed.to_str().unwrap(),
                bare.to_str().unwrap(),
            ],
        );
        git(&bare, &["symbolic-ref", "HEAD", "refs/heads/main"]);
        (seed, bare, head)
    }
    #[tokio::test]
    async fn native_http_preserves_ticket_pr_review_reaction_and_completion_contracts() {
        let temp = tempfile::tempdir().unwrap();
        let mut native = fixture(temp.path());
        let services = native.workspace.join(".guildbotics/local/services");
        write_json(
            &services.join("tickets/acme%2Fdemo/tracker.json"),
            json!({"repository":"acme/demo","labels":["bug"]}),
        );
        let ticket = command(
            &native,
            &[
                "ticket",
                "create",
                "--person",
                "aiko",
                "--tracker",
                "acme/demo",
                "--title",
                "Ticket",
                "--label",
                "bug",
                "--human-approved",
                "--content-stdin",
            ],
            "entire\nbody\n",
        )
        .await;
        let url = ticket["target"]["url"].as_str().unwrap();
        let updated = command(
            &native,
            &[
                "ticket",
                "update",
                "--person",
                "aiko",
                "--url",
                url,
                "--title",
                "Changed",
                "--content-stdin",
            ],
            "  \n",
        )
        .await;
        assert_eq!(updated["body"], "");
        let read = command(
            &native,
            &[
                "ticket",
                "read",
                "--person",
                "aiko",
                "--resource",
                "tickets",
                "--subject",
                url,
            ],
            "",
        )
        .await;
        assert_eq!(read["items"][0]["title"], "Changed");
        assert_eq!(read["items"][0]["labels"], json!(["bug"]));
        let comment = command(
            &native,
            &[
                "ticket",
                "comment",
                "--person",
                "Aiko",
                "--url",
                url,
                "--content-stdin",
            ],
            "multi\nline\n",
        )
        .await;
        command(
            &native,
            &[
                "ticket",
                "reaction",
                "add",
                "--person",
                "aiko",
                "--url",
                url,
                "--comment-id",
                comment["id"].as_str().unwrap(),
                "--reaction",
                "ack",
            ],
            "",
        )
        .await;
        let (_, _, head) = seed_code(&native, temp.path());
        native.invocation = Some(gb_environment::broker::Invocation {
            chat_subject: None,
            person_id: "aiko".into(),
            run_id: "bound-run".into(),
            trace_id: "bound-run".into(),
            work_kind: "ticket".into(),
            work_identity: url.into(),
            cwd: native.workspace.clone(),
            access: gb_wire::CommandAccess {
                read_only: false,
                inspects: vec![],
            },
        });
        let pr = command(
            &native,
            &[
                "pr",
                "create",
                "--person",
                "aiko",
                "--repo",
                "acme/demo",
                "--head",
                "feature",
                "--base",
                "main",
                "--title",
                "PR",
                "--issue-url",
                url,
                "--closes-issue",
                "--content-stdin",
            ],
            "body\n",
        )
        .await;
        let pr_url = pr["target"]["url"].as_str().unwrap();
        command(
            &native,
            &[
                "pr",
                "update",
                "--person",
                "aiko",
                "--url",
                pr_url,
                "--title",
                "Updated",
                "--content-stdin",
            ],
            " \n",
        )
        .await;
        command(
            &native,
            &[
                "pr",
                "review",
                "--person",
                "aiko",
                "--url",
                pr_url,
                "--event",
                "comment",
                "--head",
                &head,
                "--content-stdin",
            ],
            "review\nbody",
        )
        .await;
        let inline = command(
            &native,
            &[
                "pr",
                "review-comment",
                "--person",
                "aiko",
                "--url",
                pr_url,
                "--head",
                &head,
                "--path",
                "file.txt",
                "--line",
                "2",
                "--side",
                "RIGHT",
                "--content-stdin",
            ],
            "inline body",
        )
        .await;
        command(
            &native,
            &[
                "pr",
                "reaction",
                "add",
                "--person",
                "aiko",
                "--url",
                pr_url,
                "--comment-id",
                inline["id"].as_str().unwrap(),
                "--reaction",
                "ack",
            ],
            "",
        )
        .await;
        command(
            &native,
            &[
                "pr",
                "comment",
                "--person",
                "aiko",
                "--url",
                pr_url,
                "--content-stdin",
            ],
            "result",
        )
        .await;
        let done = command(
            &native,
            &[
                "task",
                "complete",
                "--person",
                "aiko",
                "--status",
                "done",
                "--content-stdin",
            ],
            "finished\nsummary",
        )
        .await;
        assert_eq!(done["completed"], true);
        assert_eq!(done["subject_id"], url);
        assert!(
            done["evidence_types"]
                .as_array()
                .unwrap()
                .iter()
                .any(|v| v == "pr_create")
        );
        let status = command(&native, &["task", "status", "--person", "aiko"], "").await;
        assert_eq!(status["status"], "done");
        assert_eq!(status["run_id"], "bound-run");
    }
    #[tokio::test]
    async fn native_usage_content_subject_and_scope_failures_are_short_and_have_no_evidence() {
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        let out = temp.path().join("out");
        let cases = [
            vec![
                "pr",
                "create",
                "--person",
                "aiko",
                "--repo",
                "acme/demo",
                "--head",
                "feature",
                "--title",
                "title",
                "--closes-issue",
                "--content-stdin",
            ],
            vec![
                "pr",
                "create",
                "--person",
                "aiko",
                "--repo",
                "acme/demo",
                "--head",
                "feature",
                "--title",
                "bad\ntitle",
                "--content-stdin",
            ],
            vec![
                "pr",
                "create",
                "--person",
                "aiko",
                "--repo",
                "acme/demo",
                "--head",
                "feature",
                "--title",
                "title",
                "--draft",
                "maybe",
                "--content-stdin",
            ],
            vec!["pr", "update", "--person", "aiko", "--url", "url"],
            vec!["ticket", "update", "--person", "aiko", "--url", "url"],
            vec![
                "ticket",
                "update",
                "--person",
                "aiko",
                "--url",
                "url",
                "--state-reason",
                "completed",
            ],
            vec![
                "pr",
                "review",
                "--person",
                "aiko",
                "--url",
                "url",
                "--event",
                "approve",
                "--content-stdin",
            ],
            vec!["git", "prepare", "--person", "aiko"],
            vec!["git", "prepare", "--person", "aiko", "--branch", "topic"],
            vec![
                "pr",
                "reaction",
                "add",
                "--person",
                "aiko",
                "--url",
                "url",
                "--target",
                "comment",
                "--reaction",
                "ack",
            ],
            vec![
                "ticket",
                "read",
                "--person",
                "aiko",
                "--resource",
                "pull_requests",
                "--subject",
                "url",
            ],
            vec![
                "pr",
                "read",
                "--person",
                "aiko",
                "--resource",
                "tickets",
                "--subject",
                "url",
            ],
            vec!["task", "status", "--run-id", "foreign"],
        ];
        for args in cases {
            let result = native
                .execute(request(&args, "body"), false, None, &out)
                .await;
            assert_ne!(result.exit_code, 0, "{args:?}");
            assert!(result.stdout.is_empty());
            assert!(
                result.stderr.starts_with("Error:"),
                "{args:?}: {}",
                result.stderr
            );
        }
        for body in ["", " ", "\n"] {
            let reply = native
                .execute(
                    request(
                        &[
                            "ticket",
                            "comment",
                            "--person",
                            "aiko",
                            "--url",
                            "url",
                            "--content-stdin",
                        ],
                        body,
                    ),
                    false,
                    None,
                    &out,
                )
                .await;
            assert_eq!(reply.exit_code, 2);
        }
        assert!(
            RunStore::open(&native.workspace)
                .records()
                .unwrap()
                .is_empty()
        );
    }
    #[tokio::test]
    async fn interactive_lifecycle_reuses_identity_keeps_first_target_and_records_failures() {
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        let services = native.workspace.join(".guildbotics/local/services");
        write_json(
            &services.join("tickets/acme%2Fdemo/tracker.json"),
            json!({"repository":null,"labels":[]}),
        );
        let first = command(
            &native,
            &[
                "ticket",
                "create",
                "--person",
                "aiko",
                "--tracker",
                "acme/demo",
                "--title",
                "First target",
                "--human-approved",
                "--content-stdin",
            ],
            "body",
        )
        .await;
        let out = temp.path().join("out");
        let result = native
            .execute(
                request(
                    &[
                        "ticket",
                        "comment",
                        "--person",
                        "aiko",
                        "--url",
                        "local://tickets/outside/demo/T-1",
                        "--content-stdin",
                    ],
                    "body",
                ),
                false,
                None,
                &out,
            )
            .await;
        assert_ne!(result.exit_code, 0);
        let dir = native.workspace.join(".guildbotics/state/sessions");
        let entries = std::fs::read_dir(dir)
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(entries.len(), 1);
        let record: Value =
            serde_json::from_slice(&std::fs::read(entries[0].path()).unwrap()).unwrap();
        assert_eq!(record["status"], "failed");
        assert_eq!(record["attributes"]["target.url"], first["target"]["url"]);
        assert_eq!(record["attributes"]["target.title"], "First target");
        assert!(!record.to_string().contains("fixture-client"));
    }
    #[tokio::test]
    async fn current_git_retries_have_one_lifecycle_and_auxiliary_phases_do_not_fetch_again() {
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        let (seed, bare, _) = seed_code(&native, temp.path());
        let remote = url::Url::from_file_path(&bare).unwrap().to_string();
        git(&seed, &["checkout", "-qb", "new-topic"]);
        std::fs::write(seed.join("new"), "new").unwrap();
        git(&seed, &["add", "."]);
        git(&seed, &["commit", "-qm", "new"]);
        let head = git(&seed, &["rev-parse", "HEAD"]);
        let bundle = temp.path().join("in.bundle");
        git(
            &seed,
            &["bundle", "create", bundle.to_str().unwrap(), "new-topic"],
        );
        let arguments = [
            "git",
            "push",
            "--person",
            "aiko",
            "--repo-path",
            "/caller/not-opened",
            "--workspace-mode",
            "current",
        ];
        let outgoing = temp.path().join("out");
        let mut input = request(&arguments, "");
        input.phase = Some(Phase::Known);
        input.data=json!({"remotes":[remote],"repo_path":"/caller/not-opened","branch":"new-topic","head":head}).as_object().unwrap().clone();
        let known = native.execute(input.clone(), false, None, &outgoing).await;
        assert_eq!(known.exit_code, 0, "{}", known.stderr);
        input.phase = Some(Phase::Push);
        let retry = native.execute(input.clone(), false, None, &outgoing).await;
        assert_eq!(retry.exit_code, 0);
        let payload: Value = serde_json::from_str(&retry.stdout).unwrap();
        assert_eq!(payload["retry"], true);
        let sessions = native.workspace.join(".guildbotics/state/sessions");
        assert!(!sessions.exists());
        let pushed = native
            .execute(input.clone(), false, Some(&bundle), &outgoing)
            .await;
        assert_eq!(pushed.exit_code, 0, "{}", pushed.stderr);
        let payload: Value = serde_json::from_str(&pushed.stdout).unwrap();
        assert_eq!(payload["pushed"], true);
        let records = std::fs::read_dir(&sessions)
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(records.len(), 1);
        let record: Value =
            serde_json::from_slice(&std::fs::read(records[0].path()).unwrap()).unwrap();
        assert_eq!(record["status"], "succeeded");
        let events = std::fs::read_to_string(
            native
                .workspace
                .join(".guildbotics/local/run/diagnostics.jsonl"),
        )
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .collect::<Vec<_>>();
        assert_eq!(
            events
                .iter()
                .filter(|v| v["type"] == "member.command.started")
                .count(),
            1
        );
        assert_eq!(
            events
                .iter()
                .filter(|v| v["type"] == "member.command.finished")
                .count(),
            1
        );
        std::fs::write(seed.join("new"), "remote advance").unwrap();
        git(&seed, &["commit", "-qam", "remote advance"]);
        git(&seed, &["push", "-q", bare.to_str().unwrap(), "new-topic"]);
        let rejected = native.execute(input.clone(), false, None, &outgoing).await;
        assert_ne!(rejected.exit_code, 0);
        assert!(rejected.stderr.starts_with("Error:"));
        let record: Value =
            serde_json::from_slice(&std::fs::read(records[0].path()).unwrap()).unwrap();
        assert_eq!(record["status"], "failed");
        // Tracking returns the fetched snapshot even if the remote advances again.
        let expected_tracking = git(&seed, &["rev-parse", "HEAD"]);
        std::fs::write(seed.join("new"), "another remote advance").unwrap();
        git(&seed, &["commit", "-qam", "another advance"]);
        git(&seed, &["push", "-q", bare.to_str().unwrap(), "new-topic"]);
        input.phase = Some(Phase::Tracking);
        input.data.insert("known".into(), json!([]));
        let tracking = native.execute(input.clone(), false, None, &outgoing).await;
        assert_eq!(tracking.exit_code, 0, "{}", tracking.stderr);
        assert!(outgoing.is_file());
        let payload: Value = serde_json::from_str(&tracking.stdout).unwrap();
        assert_eq!(
            payload["refs"]["refs/remotes/origin/new-topic"],
            expected_tracking
        );
        std::fs::rename(&bare, temp.path().join("offline.git")).unwrap();
        input.phase = Some(Phase::Known);
        let failed = native.execute(input, false, None, &outgoing).await;
        assert_ne!(failed.exit_code, 0);
        assert!(failed.stderr.starts_with("Error:"));
        assert!(!failed.stderr.contains("panicked"));
    }
    #[tokio::test]
    async fn readonly_show_and_download_never_open_a_callers_destination_and_help_matches_the_plan()
    {
        let temp = tempfile::tempdir().unwrap();
        let native = fixture(temp.path());
        seed_code(&native, temp.path());
        let out = temp.path().join("out");
        let shown = command(
            &native,
            &[
                "git",
                "show",
                "--person",
                "aiko",
                "--repo",
                "acme/demo",
                "--path",
                "file.txt",
            ],
            "",
        )
        .await;
        assert_eq!(shown["default_branch"], "main");
        assert_eq!(shown["items"][0]["content"], "base\n");
        assert!(!native.workspace.join(".guildbotics/local/clones").exists());
        let ticket = "local://tickets/acme/demo/T-1";
        let root = native
            .workspace
            .join(".guildbotics/local/services/tickets/acme%2Fdemo");
        write_json(
            &root.join("T-1.json"),
            json!({"body":"local://tickets/acme/demo/attachments/att","comments":[]}),
        );
        write_json(
            &root.join("attachments/att.json"),
            json!({"name":"report.bin","media_type":"application/octet-stream"}),
        );
        std::fs::write(root.join("attachments/att"), b"raw\0\xff").unwrap();
        let dest = temp.path().join("must-not-open");
        let reply = native
            .execute(
                request(
                    &[
                        "ticket",
                        "attachment",
                        "download",
                        "--person",
                        "aiko",
                        "--url",
                        ticket,
                        "--id",
                        "att",
                        "--dest",
                        dest.to_str().unwrap(),
                    ],
                    "",
                ),
                false,
                None,
                &out,
            )
            .await;
        assert_eq!(reply.exit_code, 0, "{}", reply.stderr);
        assert_eq!(std::fs::read(&out).unwrap(), b"raw\0\xff");
        assert!(!dest.exists());
        assert_eq!(
            reply.metadata["download"]["attachment"],
            json!(["report.bin", "att"])
        );
        let input = request(&["pr", "review", "--help"], "");
        let plan = native.execute(input.clone(), true, None, &out).await;
        let reply = native.execute(input, false, None, &out).await;
        assert_eq!(plan.metadata["plan"]["result"]["stdout"], reply.stdout);
        assert_eq!(reply.exit_code, 0);
    }
    #[tokio::test]
    async fn simultaneous_interactive_and_bound_calls_keep_their_outputs_and_invocations_separate()
    {
        let temp = tempfile::tempdir().unwrap();
        let interactive = fixture(temp.path());
        let mut bound = interactive.clone();
        bound.invocation = Some(gb_environment::broker::Invocation {
            chat_subject: None,
            person_id: "aiko".into(),
            run_id: "bound".into(),
            trace_id: "bound".into(),
            work_kind: "command".into(),
            work_identity: "fixed".into(),
            cwd: bound.workspace.clone(),
            access: gb_wire::CommandAccess {
                read_only: false,
                inspects: vec![],
            },
        });
        let out1 = temp.path().join("out1");
        let out2 = temp.path().join("out2");
        let args = [
            "memory",
            "record",
            "--person",
            "aiko",
            "--title",
            "note",
            "--content-stdin",
        ];
        let (a, b) = tokio::join!(
            interactive.execute(request(&args, "interactive body"), false, None, &out1),
            bound.execute(request(&args, "bound body"), false, None, &out2)
        );
        assert_eq!(a.exit_code, 0, "{}", a.stderr);
        assert_eq!(b.exit_code, 0, "{}", b.stderr);
        let a: Value = serde_json::from_str(&a.stdout).unwrap();
        let b: Value = serde_json::from_str(&b.stdout).unwrap();
        assert_ne!(a["doc_id"], b["doc_id"]);
        for (id, body) in [
            (a["doc_id"].as_str().unwrap(), "interactive body"),
            (b["doc_id"].as_str().unwrap(), "bound body"),
        ] {
            let result = command(
                &interactive,
                &["memory", "get", "--person", "aiko", "--id", id],
                "",
            )
            .await;
            assert_eq!(result["body"], body);
        }
        let runs = RunStore::open(&interactive.workspace).records().unwrap();
        assert_eq!(runs.len(), 1);
        assert_eq!(runs[0].run_id, "bound");
        let refused = bound
            .execute(
                request(
                    &[
                        "git",
                        "publish",
                        "--person",
                        "aiko",
                        "--repo-path",
                        "/caller",
                        "--workspace-mode",
                        "current",
                        "--content-stdin",
                    ],
                    "message",
                ),
                false,
                None,
                &out2,
            )
            .await;
        assert_ne!(refused.exit_code, 0);
        assert!(refused.stderr.starts_with("Error:"));
    }
}

#[cfg(test)]
mod guest_failure_tests {
    use super::*;
    use gb_member::git::Guest as _;

    #[tokio::test]
    async fn member_git_preserves_the_localized_guest_failure() {
        let channel = gb_environment::guest_process::Channel::new();
        let guest = Guest {
            channel: Some(&channel),
            language: "ja",
            deadline: Instant::now() + std::time::Duration::from_secs(1),
        };
        let error = guest
            .run(
                Path::new("/clones/work"),
                vec![],
                BTreeMap::new(),
                vec![],
                1024,
            )
            .await
            .err()
            .expect("invalid process must fail");
        assert!(
            matches!(error, MemberError::CapabilityArgs { key: "capabilities.member_git.git_failed_reason", args } if args["reason"] == crate::i18n::t("ja", "capabilities.member_git.invalid_path", &[]))
        );
    }

    #[test]
    fn guest_failure_stages_render_in_both_languages() {
        for language in ["en", "ja"] {
            for key in [
                "capabilities.member_git.guest_start_failed",
                "capabilities.member_git.guest_stdin_failed",
                "capabilities.member_git.guest_stdout_limit",
                "capabilities.member_git.guest_stderr_limit",
                "capabilities.member_git.guest_execution_failed",
                "capabilities.member_git.guest_stream_closed",
                "capabilities.member_git.guest_timeout",
            ] {
                let text = crate::i18n::say(
                    gb_environment::Sentence::Key(key, vec![("reason", "synthetic reason".into())]),
                    language,
                );
                assert_ne!(text, key);
                assert!(!text.contains("%{"));
                if key.ends_with("failed") || key.ends_with("limit") {
                    assert!(text.contains("synthetic reason"));
                }
            }
        }
    }
}
