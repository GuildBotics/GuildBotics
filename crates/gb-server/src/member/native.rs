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
                stdout: gb_member::context::help().into(),
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
                subject_url: if matches!(kind, "ticket" | "ticket_mention") {
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
                return match command::parse(&request.arguments){Ok(parsed)=>Reply{metadata:[("plan".into(),if parsed.help{json!({"result":{"exit_code":0,"stdout":gb_member::context::help(),"stderr":""}})}else{parsed.plan()})].into_iter().collect(),..Default::default()},Err(error)=>Reply{exit_code:2,stderr:error.render(|key,args|crate::i18n::t(&language,key,&args.iter().map(|(k,v)|(k.as_str(),v.as_str())).collect::<Vec<_>>())),..Default::default()}};
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
fn public(member: &Value) -> PublicMember {
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
fn integration(
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
                .map_err(|_| MemberError::Capability("capabilities.member_git.git_failed"))?;
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
mod tests {
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
    fn fixture(root: &Path) -> Native {
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
    async fn http(native: Native, request: MemberRequest, path: &str) -> Value {
        let root = tempfile::tempdir().unwrap();
        let api = Arc::new(crate::api::Api {
            member: Some(Arc::new(Backend {
                version: "fixture".into(),
                workspace_id: "workspace".into(),
                executor: Arc::new(native),
                timeout: Duration::from_secs(30),
            })),
            inputs: crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            auth: crate::auth::ServerAuth::open(root.path(), 8765).unwrap(),
            instance_id: "fixture".into(),
            data: root.path().into(),
            workspace: None,
            work: tokio_util::task::TaskTracker::new(),
            cancelled: tokio_util::sync::CancellationToken::new(),
        });
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
            "/api/member/run",
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
            "/api/member/run",
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
            "/api/member/plan",
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
        let reply = http(native.clone(), request(arguments, stdin), "/api/member/run").await;
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
    fn seed_code(native: &Native, root: &Path) -> (PathBuf, PathBuf, String) {
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
