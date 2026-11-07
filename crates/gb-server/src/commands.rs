//! `POST /api/commands/run`: one command the screens or the command line
//! start, as manual work under a trace of its own, in a microVM of its own,
//! as the Python version runs it (`AppRuntime.run_command` of
//! `guildbotics/app_api/runtime.py`).
//!
//! The command is resolved once, for the member it runs as, before anything
//! is recorded: a command that cannot be resolved never ran and leaves no
//! trace. Screen requests additionally compare the selected shared file's
//! identity and revision with this member's runtime target before starting.

pub(crate) use crate::i18n::say;
use std::path::{Path, PathBuf};

use axum::Json;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use gb_config::commands::{CommandRoots, command_access};
use gb_config::team::{Member, Team};
use gb_environment::{Failure, Sentence};
use gb_observability::{Diagnostics, Trace};
use gb_wire::{CliAgentExecutionResult, CommandFailure};
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::api::{ApiError, Body, Shared};
use crate::i18n;

/// Where a run that names no working directory runs: the exchange
/// directory, under the person's home.
const EXCHANGE_DIRECTORY: &str = "Documents/GuildBotics";

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RunRequest {
    pub(crate) command: String,
    #[serde(default)]
    pub(crate) args: Vec<String>,
    pub(crate) person: Option<String>,
    #[serde(default)]
    pub(crate) message: String,
    pub(crate) cwd: Option<PathBuf>,
    /// The workspace the caller means, which must still be the selected one.
    pub(crate) expected_workspace: Option<PathBuf>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) expected_command_file_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub(crate) expected_command_file_revision: Option<String>,
}

struct RunState<'a> {
    /// The owner's home and the root of the members' identities.
    home: PathBuf,
    identity_root: PathBuf,
    keychain: std::sync::Arc<dyn gb_config::secrets::Keychain>,
    events: Option<std::sync::Arc<crate::events::EventBus>>,
    /// The one manual run that writes, by its trace, while it runs.
    manual: Option<std::sync::Arc<std::sync::Mutex<Option<String>>>>,
    service: &'a crate::service_runtime::Controller,
    member: Option<std::sync::Arc<crate::member::Backend>>,
    inputs: Option<&'a crate::input_files::InputFileStore>,
    data: &'a Path,
    workspace: Option<&'a PathBuf>,
    work: &'a tokio_util::task::TaskTracker,
    cancelled: &'a tokio_util::sync::CancellationToken,
}

/// How a run that started did not succeed, as its trace records it.
struct Ended {
    error_type: String,
    code: &'static str,
    /// What the caller is told: a failed command (400) or a defect (500).
    answer: Result<String, String>,
}

impl gb_observability::Failure for Ended {
    fn error_type(&self) -> String {
        self.error_type.clone()
    }

    fn message(&self) -> Option<String> {
        self.answer.as_ref().ok().cloned()
    }

    fn code(&self) -> &str {
        self.code
    }
}

pub(crate) async fn run(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<RunRequest>,
) -> Response {
    // Admitted for the selected workspace, which cannot be switched until
    // the run is the server's work.
    let Some(admitted) = api.admit() else {
        return ApiError::new(StatusCode::CONFLICT, "command_workspace_changing", &headers)
            .into_response();
    };
    let launch = Launch::manual(&request.command);
    match served(&api, &headers, request, launch, admitted).await {
        Ok(ran) => Json(json!({"trace_id": ran.trace_id, "output": ran.output})).into_response(),
        Err(refused) => refused,
    }
}

/// Runs `request` as the server's work for the selected workspace,
/// `admitted` for it, started and answered as `launch` says.
pub(crate) async fn served(
    api: &Shared,
    headers: &HeaderMap,
    request: RunRequest,
    launch: Launch,
    admitted: tokio::sync::OwnedRwLockReadGuard<()>,
) -> Result<Ran, Response> {
    let selected = api.selected();
    let state = RunState {
        home: api.home.clone(),
        identity_root: api.identity_root.clone(),
        keychain: api.keychain.clone(),
        events: Some(api.events.clone()),
        manual: Some(api.manual.clone()),
        service: &selected.service,
        member: selected.member.clone(),
        inputs: Some(&api.inputs),
        data: &api.data,
        workspace: selected.workspace.as_ref(),
        work: &api.work,
        cancelled: &api.cancelled,
    };
    start(&state, headers, request, launch, Some(admitted)).await
}

/// How a run is started and answered, beside what was asked: what its trace
/// and events call it, its trace's attributes, the work it is part of, where
/// it runs, how a failed command is answered, and whether its result is read.
pub(crate) struct Launch {
    pub(crate) trace: Option<Trace>,
    pub(crate) diagnostic_check: bool,
    pub(crate) failure_capture: Option<std::sync::Arc<std::sync::Mutex<Option<CommandFailure>>>>,
    pub(crate) label: String,
    pub(crate) attributes: serde_json::Map<String, serde_json::Value>,
    pub(crate) work: Option<(String, String)>,
    pub(crate) cwd: Option<PathBuf>,
    pub(crate) failure: (StatusCode, &'static str),
    /// The result the command must return, by its type's name and whether a
    /// value is one; none when its result is not read.
    pub(crate) result: Option<ResultType>,
}

/// A result's type, by its name and whether a value is one.
pub(crate) type ResultType = (&'static str, fn(&serde_json::Value) -> bool);

impl Launch {
    /// A command run as asked, for the person who asked.
    pub(crate) fn manual(command: &str) -> Launch {
        Launch {
            trace: None,
            diagnostic_check: false,
            failure_capture: None,
            label: command.into(),
            attributes: serde_json::Map::from_iter([("run.entry".into(), "command".into())]),
            work: None,
            cwd: None,
            failure: (StatusCode::BAD_REQUEST, "command_error"),
            result: None,
        }
    }
}

/// Correlate the check's worker logs without publishing command lifecycle events.
pub(crate) async fn run_diagnostic_check<T>(
    trace: &Trace,
    run: impl std::future::Future<Output = T>,
) -> T {
    use tracing::Instrument as _;
    run.instrument(tracing::info_span!(
        "diagnostics_check",
        diagnostics = serde_json::to_string(&trace.event_fields())
            .unwrap()
            .as_str()
    ))
    .await
}

/// A run that ended as its command did.
pub(crate) struct Ran {
    pub(crate) trace_id: String,
    pub(crate) output: String,
    pub(crate) result: serde_json::Value,
}

/// Use the same execution boundary when no server has accepted this run.
pub(crate) async fn run_locally(
    request: RunRequest,
    workspace: &PathBuf,
    data: &Path,
    language: &'static str,
) -> anyhow::Result<String> {
    let work = tokio_util::task::TaskTracker::new();
    let cancelled = tokio_util::sync::CancellationToken::new();
    let service = crate::server::service_controller(Some(workspace), data);
    let member = crate::server::member_backend(workspace, data, &work, None);
    let state = RunState {
        home: std::env::var_os("HOME")
            .filter(|home| !home.is_empty())
            .map(PathBuf::from)
            .ok_or_else(|| anyhow::anyhow!("HOME is not set"))?,
        identity_root: gb_base::home::identity_root()?,
        keychain: std::sync::Arc::new(gb_config::secrets::SystemKeychain),
        events: None,
        manual: None,
        service: &service,
        member,
        inputs: None,
        data,
        workspace: Some(workspace),
        work: &work,
        cancelled: &cancelled,
    };
    let mut headers = HeaderMap::new();
    headers.insert(
        "x-guildbotics-language",
        axum::http::HeaderValue::from_static(language),
    );
    let signal = tokio::spawn({
        let cancelled = cancelled.clone();
        async move {
            if tokio::signal::ctrl_c().await.is_ok() {
                cancelled.cancel();
            }
        }
    });
    let launch = Launch::manual(&request.command);
    let response = match start(&state, &headers, request, launch, None).await {
        Ok(ran) => Json(json!({"trace_id": ran.trace_id, "output": ran.output})).into_response(),
        Err(refused) => refused,
    };
    work.close();
    work.wait().await;
    signal.abort();
    crate::tool_login::drain_private().await;
    gb_environment::worktree::drain_writebacks().await;
    gb_environment::runtime::drain_vms().await;
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX).await?;
    let answer = serde_json::from_slice(&bytes).unwrap_or_default();
    anyhow::ensure!(
        status.is_success(),
        crate::client::response_error(status, &answer)
    );
    crate::client::command_output(answer, language)
}

/// Starts `request`; `admitted` holds the selection until the run is the
/// server's work.
async fn start(
    api: &RunState<'_>,
    headers: &HeaderMap,
    request: RunRequest,
    launch: Launch,
    admitted: Option<tokio::sync::OwnedRwLockReadGuard<()>>,
) -> Result<Ran, Response> {
    let error = |status, code| ApiError::new(status, code, headers);
    if request.command.is_empty() {
        return Err(error(StatusCode::UNPROCESSABLE_ENTITY, "validation_error").into_response());
    }
    if request.expected_command_file_id.is_some()
        != request.expected_command_file_revision.is_some()
    {
        return Err(error(StatusCode::UNPROCESSABLE_ENTITY, "validation_error").into_response());
    }
    // The run is accepted for the workspace it was asked for before anything
    // is derived from it.
    if let Some(expected) = &request.expected_workspace {
        let same = api.workspace.is_some_and(|workspace| {
            std::fs::canonicalize(expected)
                .ok()
                .zip(std::fs::canonicalize(workspace).ok())
                .is_some_and(|(expected, selected)| expected == selected)
        });
        if !same {
            return Err(error(StatusCode::CONFLICT, "command_workspace_changed").into_response());
        }
    }
    let Some(workspace) = api.workspace else {
        return Err(error(StatusCode::BAD_REQUEST, "config_not_found")
            .context(json!({"path": ""}))
            .into_response());
    };
    let host = match api.service.host() {
        Some(host) => host,
        None => std::sync::Arc::new(gb_environment::Host {
            data: api.data.to_path_buf(),
            home: api.home.clone(),
            package: gb_base::home::python_package(),
            identity_root: api.identity_root.clone(),
        }),
    };
    let config = workspace.join(".guildbotics/config");
    let project = config.join("team/project.yml");
    if !project.is_file() {
        return Err(error(StatusCode::BAD_REQUEST, "config_not_found")
            .context(json!({"path": project}))
            .into_response());
    }
    // The run reads the configuration once: from its copy the host decides
    // which file runs and what it may do, and its microVM is given the copy.
    let configuration = std::sync::Arc::new(
        gb_environment::Configuration::copied(&host, workspace)
            .await
            .map_err(|e| crate::config_api::failure(e, headers))?,
    );
    let team = crate::command_catalog::team(
        configuration.path(),
        host.identity_root.clone(),
        headers,
        Some(&configuration),
    )
    .await?;
    // A run refused for its member never started, but the screens are told.
    let person = person(&team, request.person.as_deref(), headers).map_err(|refused| {
        if let Some(events) = &api.events {
            let mut payload = refused.payload();
            payload.insert("command".into(), launch.label.clone().into());
            events.publish("command.failed", payload, crate::events::Origin::default());
        }
        refused.into_response()
    })?;
    let language = i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    );
    if person.person_type == "human" {
        return Err(error(StatusCode::BAD_REQUEST, "human_subject")
            .reason(i18n::t(language, "cli.member.human_subject", &[]))
            .into_response());
    }
    // The one reading of the command: the file it runs and what it declares.
    let roots = CommandRoots {
        config: configuration.path(),
        templates: host.package.join("templates"),
    };
    let resolved = roots.resolve(&request.command, &team.language, Some(&person.person_id));
    if let (Some(id), Some(expected)) = (
        &request.expected_command_file_id,
        &request.expected_command_file_revision,
    ) {
        let selected = roots.selected_file(id, &team.language).ok_or_else(|| {
            error(StatusCode::NOT_FOUND, "command_file_not_found")
                .key(
                    "command_file_not_found.unknown",
                    vec![("file_id", id.clone())],
                )
                .into_response()
        })?;
        let workspace = crate::command_catalog::Workspace {
            roots: CommandRoots {
                config: roots.config.clone(),
                templates: roots.templates.clone(),
            },
            team: team.clone(),
            host: host.clone(),
            directory: workspace.clone(),
            keychain: api.keychain.clone(),
        };
        let (blocked, _) = crate::command_catalog::evaluate(
            &workspace,
            &selected,
            expected,
            &request.command,
            Some(&person.person_id),
            language,
        )
        .await
        .map_err(|e| internal(&e.to_string(), "OSError"))?;
        if let Some((code, context)) = blocked {
            return Err(error(StatusCode::CONFLICT, code)
                .key("command_not_runtime_target", vec![])
                .context(context)
                .into_response());
        }
    }
    // Where the run works, once its member and file are known; a directory
    // of its own is made without following a link on the way.
    let cwd = match (&launch.cwd, &request.cwd) {
        (Some(cwd), _) => {
            gb_base::host_path::open_directory(cwd, true)
                .map_err(|e| internal(&e.to_string(), "OSError"))?;
            cwd.clone()
        }
        (None, Some(cwd)) => {
            working_directory(cwd, &host.home).map_err(|refused| match refused {
                None => error(StatusCode::BAD_REQUEST, "command_cwd_not_absolute")
                    .key(
                        "command_cwd_not_absolute",
                        vec![("cwd", cwd.display().to_string())],
                    )
                    .into_response(),
                Some(problem) => error(StatusCode::BAD_REQUEST, "command_error")
                    .reason(problem)
                    .into_response(),
            })?
        }
        (None, None) => host.home.join(EXCHANGE_DIRECTORY),
    };
    let resolved = resolved.and_then(|path| Ok((command_access(&path)?, path)));
    let (access, path) = resolved.map_err(|problem| {
        error(launch.failure.0, launch.failure.1)
            .reason(configuration.spoken(&problem.to_string()))
            .into_response()
    })?;
    // A stop that does not wait for the work refuses work not started yet.
    if api.cancelled.is_cancelled() {
        return Err(error(StatusCode::SERVICE_UNAVAILABLE, "command_cancelled").into_response());
    }
    // One manual run that writes at a time; it holds its place until it ends.
    let place = match (&api.manual, access.read_only) {
        (Some(manual), false) => Some(Place::take(manual).map_err(|running| {
            error(StatusCode::CONFLICT, "command_already_running")
                .context(json!({ "trace_id": running }))
                .into_response()
        })?),
        _ => None,
    };
    // The screens hear of the run's events, and its place names its trace.
    let announce = api.events.clone().map(|events| {
        std::sync::Arc::new(move |event: &serde_json::Map<String, serde_json::Value>| {
            events.announce(event);
        }) as gb_observability::Announce
    });
    let opened = move |place: &Option<Place>, mut trace: Trace| {
        if let Some(announce) = announce {
            trace = trace.announced(announce);
        }
        if let Some(place) = place {
            place.hold(trace.id());
        }
        trace
    };
    if request.command == crate::ticket_selector::COMMAND && request.args.is_empty() {
        let config = configuration.clone();
        return manual_ticket(api, headers, &person.person_id, config, place, opened).await;
    }
    let diagnostic_check = launch.diagnostic_check;
    let joined = launch.trace.is_some();
    let mut trace = launch.trace.unwrap_or_else(|| {
        Trace::open(&Diagnostics::open(workspace), "manual").person(&person.person_id)
    });
    for (key, value) in launch.attributes {
        trace = trace.attribute(&key, value);
    }
    let trace = opened(&place, trace);
    let trace_id = trace.id().to_owned();
    let run_id = if joined {
        uuid::Uuid::new_v4().simple().to_string()
    } else {
        trace_id.clone()
    };
    let command = gb_environment::Command {
        config: configuration,
        wants_result: launch.result.is_some(),
        invocation: None,
        person_id: person.person_id.clone(),
        run_id: run_id.clone(),
        trace_id: trace_id.clone(),
        work_kind: launch
            .work
            .as_ref()
            .map_or("manual".into(), |w| w.0.clone()),
        work_identity: launch
            .work
            .as_ref()
            .map_or(trace_id.clone(), |w| w.1.clone()),
        path,
        name: request.command.clone(),
        access,
        args: request.args,
        cwd,
        pipe: request.message,
    };
    // The run is the server's work, not the request's: it runs to its end
    // when the caller goes away, and a stop waits for it unless it cancels
    // it. A cancelled run's trace says so as it is dropped, and its microVM
    // goes with it.
    let (name, person_id) = (launch.label, person.person_id.clone());
    let (asked, wanted) = (request.command.clone(), launch.result);
    let failure_capture = launch.failure_capture;
    let worker_capture = failure_capture.clone();
    let (workspace, cancelled) = (workspace.clone(), api.cancelled.clone());
    let inputs = match api.inputs {
        Some(inputs) => inputs.mount_if_available().await,
        None => None,
    };
    let member = api.member.clone();
    let mut admission = crate::execution::Admission {
        generation: None,
        command: name.clone(), workspace: workspace.clone(),
        writes: std::sync::Arc::new(gb_base::shared_writes::SharedWrites::new(host.identity_root.clone(), Some(workspace.clone()))),
        record: serde_json::from_value(json!({"run_id":run_id,"work_kind":"manual","work_identity":{"run_id":trace_id},"execution_mode":"user_initiated","member_id":person_id,
            "device_id":gb_workspace::identity::ensure_device_identity(api.data).map_err(|e|internal(&e.to_string(),"RuntimeError"))?.device_id,
            "source":"manual","started_at":chrono::Utc::now().to_rfc3339()})).expect("manual record fields"),
        redactor: crate::host_window::command_redactor(&host, &workspace, &*api.keychain).map_err(|e|internal(&e,"RuntimeError"))?,
        exclusive: !command.access.read_only, record_start: true, cleanup: api.work.clone(),
    };
    let work = api
        .service
        .execution
        .spawn(move |generation| async move {
            // The place is the run's, not the request's: the run outlives a
            // caller who goes away.
            let _place = place;
            admission.generation = Some(generation.clone());
            let execution =
                crate::execution::Execution::enter(admission, generation.draining.clone()).await?;
            let execute = Box::pin(execute(&workspace, async {
                let device = gb_environment::device::read(&host, &workspace).await;
                if let Some(refusal) = device.refusal.clone() {
                    return Err(failed(Failure::Command(refusal), language));
                }
                let mut services = crate::host_window::prepare_command(
                    &host,
                    &workspace,
                    &command,
                    &device,
                    member.as_deref(),
                    #[cfg(test)]
                    None,
                )
                .await
                .map_err(|reason| failed(Failure::Command(Sentence::Text(reason)), language))?;
                if let Some(inputs) = inputs {
                    services.session.mounts.push(inputs);
                }
                let reply = gb_environment::run(
                    &host,
                    &workspace,
                    command,
                    |warning| {
                        tracing::warn!("{}", say(warning, language));
                    },
                    &services.session,
                )
                .await;
                services.settle().await;
                let reply = reply.map_err(|failure| failed(failure, language))?;
                if !diagnostic_check {
                    trace.command_output(&name, &person_id, &reply.text_output);
                }
                match reply.failure {
                    None => match wanted {
                        Some((kind, valid)) if !valid(&reply.result) => Err(Ended {
                            error_type: "CommandError".into(),
                            code: "",
                            answer: Ok(format!("Command '{asked}' did not return a {kind}.")),
                        }),
                        _ => Ok((reply.text_output, reply.result)),
                    },
                    Some(failure) => {
                        if let Some(capture) = &worker_capture {
                            *capture.lock().unwrap_or_else(|p| p.into_inner()) =
                                Some(failure.clone());
                        }
                        Err(failed_in_environment(failure))
                    }
                }
            }));
            let run = async {
                if diagnostic_check {
                    run_diagnostic_check(&trace, execute).await
                } else {
                    trace.run_command(&name, &person_id, execute).await
                }
            };
            // The run first: once started, it is recorded however it ends.
            let result = tokio::select! {
                biased;
                ran = run => Some(ran),
                () = cancelled.cancelled() => None,
                () = generation.cancelled.cancelled() => None,
            };
            let state = match &result {
                Some(Ok(_)) => gb_observability::runs::RunState::Succeeded,
                Some(Err(_)) => gb_observability::runs::RunState::Failed,
                None => gb_observability::runs::RunState::Cancelled,
            };
            if execution.finish(state).await.is_err() {
                tracing::warn!("cannot finish manual command record");
            }
            Ok::<_, crate::execution::Rejected>(result)
        })
        .map_err(|_| error(StatusCode::CONFLICT, "work_rejected").into_response())?;
    // The server still owns a caller-disconnected request's manual work.
    let work = api.work.spawn(work);
    drop(admitted);

    let ran = match work.await {
        Ok(Ok(Ok(Some(ran)))) => ran,
        Ok(Ok(Ok(None))) => {
            return Err(error(StatusCode::SERVICE_UNAVAILABLE, "command_cancelled")
                .context(json!({"trace_id": trace_id}))
                .into_response());
        }
        Ok(Ok(Err(_))) => return Err(error(StatusCode::CONFLICT, "work_rejected").into_response()),
        Ok(Err(panicked)) | Err(panicked) => {
            return Err(internal(&panicked.to_string(), "RuntimeError"));
        }
    };
    if let (Err(ended), Some(capture)) = (&ran, &failure_capture) {
        let mut capture = capture.lock().unwrap_or_else(|p| p.into_inner());
        if capture.is_none() {
            *capture = Some(CommandFailure {
                command: ended.answer.is_ok(),
                type_: ended.error_type.clone(),
                message: ended
                    .answer
                    .as_ref()
                    .ok()
                    .cloned()
                    .unwrap_or_else(|| ended.error_type.clone()),
                cli_agent: String::new(),
                cli_agent_message: String::new(),
                cli_agent_result: None,
                inference_error_type: String::new(),
                inference_category: String::new(),
            });
        }
    }
    match ran {
        Ok((output, result)) => Ok(Ran {
            trace_id,
            output,
            result,
        }),
        Err(Ended {
            answer: Ok(reason), ..
        }) => Err(error(launch.failure.0, launch.failure.1)
            .reason(reason)
            .context(json!({"trace_id": trace_id}))
            .into_response()),
        Err(Ended {
            answer: Err(defect),
            error_type,
            ..
        }) => Err(internal(&defect, &error_type)),
    }
}

/// The working directory as it was typed, ready to run in: `~` expanded, as
/// a shell would. A relative one names nowhere the person can see (`None`),
/// and one that climbs or holds a NUL is no name of the host's.
async fn execute<T>(workspace: &Path, run: impl std::future::Future<Output = T>) -> T {
    let awake = crate::keep_awake::CommandHold::acquire(workspace).await;
    let result = run.await;
    awake.finish().await;
    result
}

async fn manual_ticket(
    api: &RunState<'_>,
    headers: &HeaderMap,
    person: &str,
    config: std::sync::Arc<gb_environment::Configuration>,
    place: Option<Place>,
    opened: impl FnOnce(&Option<Place>, Trace) -> Trace + Send + 'static,
) -> Result<Ran, Response> {
    use crate::{dispatcher::DispatchFailure, workflow_runtime::Refused};
    let error = |status, code| ApiError::new(status, code, headers);
    let mut runtime = api
        .service
        .workflow(person, api.work.clone())
        .map_err(|e| internal(&e.to_string(), "RuntimeError"))?;
    runtime.config = Some(config);
    let work = api
        .service
        .execution
        .spawn(move |generation| async move {
            runtime.draining = generation.draining.clone();
            runtime.cancelled = generation.cancelled.clone();
            runtime.generation = Some(generation);
            // The place is the run's, as `start`'s is.
            let mut trace_id = None;
            let result = runtime
                .manual_ticket(|trace| {
                    trace_id = Some(trace.id().to_owned());
                    opened(&place, trace)
                })
                .await;
            drop(place);
            (trace_id, result)
        })
        .map_err(|_| error(StatusCode::CONFLICT, "work_rejected").into_response())?;
    let (trace_id, result) = api
        .work
        .spawn(work)
        .await
        .map_err(|e| internal(&e.to_string(), "RuntimeError"))?
        .map_err(|e| internal(&e.to_string(), "RuntimeError"))?;
    let traced = json!({ "trace_id": trace_id });
    let language = i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    );
    let ended = match result {
        Ok((trace_id, reply)) => {
            return Ok(Ran {
                trace_id,
                output: reply.text_output,
                result: reply.result,
            });
        }
        Err(Refused::Admission(_)) => {
            return Err(error(StatusCode::CONFLICT, "work_rejected").into_response());
        }
        Err(Refused::Dispatch(DispatchFailure::Cancelled)) => {
            return Err(error(StatusCode::SERVICE_UNAVAILABLE, "command_cancelled")
                .context(traced)
                .into_response());
        }
        Err(Refused::Dispatch(DispatchFailure::Guest(failure))) => failed_in_environment(*failure),
        Err(Refused::Dispatch(DispatchFailure::Environment(failure))) => failed(failure, language),
        Err(Refused::Dispatch(DispatchFailure::Service(problem))) => {
            let arguments = problem.translation_args();
            let arguments = arguments
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str()))
                .collect::<Vec<_>>();
            Ended {
                error_type: "CommandError".into(),
                code: "",
                answer: Ok(crate::i18n::t(
                    language,
                    problem.translation_key(),
                    &arguments,
                )),
            }
        }
        Err(_) => {
            return Err(error(StatusCode::INTERNAL_SERVER_ERROR, "internal_error").into_response());
        }
    };
    match ended.answer {
        Ok(reason) => Err(error(StatusCode::BAD_REQUEST, "command_error")
            .reason(reason)
            .context(traced)
            .into_response()),
        Err(defect) => Err(internal(&defect, &ended.error_type)),
    }
}

fn working_directory(cwd: &Path, home: &Path) -> Result<PathBuf, Option<String>> {
    let expanded = match cwd.strip_prefix("~") {
        Ok(rest) => home.join(rest),
        Err(_) => cwd.to_path_buf(),
    };
    if !expanded.is_absolute() {
        return Err(None);
    }
    gb_base::host_path::normalize(&expanded).map_err(|problem| Some(problem.to_string()))
}

/// The place of the one manual run that writes, held until it is dropped.
struct Place(std::sync::Arc<std::sync::Mutex<Option<String>>>);

impl Place {
    /// Takes the place, or says which run holds it.
    fn take(slot: &std::sync::Arc<std::sync::Mutex<Option<String>>>) -> Result<Place, String> {
        let mut held = slot.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(running) = held.as_ref() {
            return Err(running.clone());
        }
        *held = Some(String::new());
        Ok(Place(slot.clone()))
    }

    /// Says the place is held by the run of `trace`.
    fn hold(&self, trace: &str) {
        *self.0.lock().unwrap_or_else(|p| p.into_inner()) = Some(trace.into());
    }
}

impl Drop for Place {
    fn drop(&mut self) {
        *self.0.lock().unwrap_or_else(|p| p.into_inner()) = None;
    }
}

/// The member a run executes as: the one named, or the team's default.
fn person<'a>(
    team: &'a Team,
    named: Option<&str>,
    headers: &HeaderMap,
) -> Result<&'a Member, ApiError> {
    let mut available: Vec<String> = team
        .members
        .iter()
        .map(|m| {
            if !m.name.is_empty() && m.name.to_lowercase() != m.person_id.to_lowercase() {
                format!("{} ({})", m.person_id, m.name)
            } else {
                m.person_id.clone()
            }
        })
        .collect();
    available.sort();
    let listed = if available.is_empty() {
        "none".to_owned()
    } else {
        available.join(", ")
    };
    let Some(identifier) = named.or(team.default_person_id()) else {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "person_selection_required",
            headers,
        )
        .key("person_selection_required", vec![("available", listed)])
        .context(json!({"available": available})));
    };
    team.find(identifier).ok_or_else(|| {
        ApiError::new(StatusCode::BAD_REQUEST, "person_not_found", headers)
            .key(
                "person_not_found.with_available",
                vec![("person", identifier.to_owned()), ("available", listed)],
            )
            .context(json!({"identifier": identifier, "available": available}))
    })
}

/// A run its environment did not let end: refused or failed as a command,
/// or failed otherwise, which the caller is not told the words of.
fn failed(failure: Failure, language: &str) -> Ended {
    match failure {
        Failure::Command(sentence) => Ended {
            error_type: "CommandError".into(),
            code: "",
            answer: Ok(say(sentence, language)),
        },
        // The Python version's `AgentRuntimeError`, which reaches the caller
        // as a defect.
        Failure::Runtime(sentence) => Ended {
            error_type: "AgentRuntimeError".into(),
            code: "",
            answer: Err(say(sentence, "en")),
        },
    }
}

/// A command that failed in its environment, as the entry said: its own
/// failure or an AI CLI tool's, which the caller is told, or another error of
/// its code, which it is not. The tool's failure counts only as the tool
/// reported it in full; otherwise it is another error.
fn failed_in_environment(failure: CommandFailure) -> Ended {
    let tool: Option<CliAgentExecutionResult> = failure
        .cli_agent_result
        .clone()
        .and_then(|result| serde_json::from_value(result.into()).ok());
    let authentication = tool
        .as_ref()
        .is_some_and(|result| result.error_category == "authentication");
    let tool_failed = tool.is_some() && failure.type_ == "CliAgentExecutionError";
    Ended {
        // The host rebuilds a command's own failure as a `CommandError`, an AI
        // CLI tool's as the tool's, and names any other by what the command
        // raised.
        error_type: if failure.command {
            "CommandError".into()
        } else {
            failure.type_.clone()
        },
        code: if authentication {
            "cli_agent_authentication"
        } else {
            ""
        },
        answer: if failure.command || tool_failed {
            Ok(failure.message)
        } else {
            Err(format!("{}: {}", failure.type_, failure.message))
        },
    }
}

/// A defect: logged, and answered without its words, as the Python version's
/// server answers an error no layer handled.
pub(crate) fn internal(what: &str, error_type: &str) -> Response {
    tracing::error!("cannot run the command: {what}");
    ApiError::new(
        StatusCode::INTERNAL_SERVER_ERROR,
        "internal_error",
        &HeaderMap::new(),
    )
    .reason("An unexpected app API error occurred.".into())
    .context(json!({"error_type": error_type}))
    .into_response()
}

#[cfg(test)]
mod tests {
    #[test]
    fn run_member_selection_preserves_the_explicit_configured_and_implicit_order() {
        use axum::http::HeaderMap;
        use gb_config::team::Team;

        let dir = tempfile::tempdir().unwrap();
        let config = dir.path().join("config");
        let identity = dir.path().join("identity");
        let project = config.join("team/project.yml");
        for (id, name, active, kind) in [
            ("yuki", "Yuki", true, "agent"),
            ("akira", "Akira", true, "agent"),
            ("aiko", "Aiko", false, "agent"),
            ("ai", "Ai", true, "human"),
        ] {
            gb_base::files::atomic_write(
                &config.join(format!("team/members/{id}/member.yml")),
                format!("is_active: {active}\n").as_bytes(),
            )
            .unwrap();
            gb_base::files::atomic_write(
                &identity.join(format!(
                    ".guildbotics/config/team/members/{id}/identity.yml"
                )),
                format!("person_id: {id}\nname: {name}\nperson_type: {kind}\n").as_bytes(),
            )
            .unwrap();
        }
        let load = |default: &str| {
            gb_base::files::atomic_write(
                &project,
                format!("name: Test\ndefault_person_id: '{default}'\n").as_bytes(),
            )
            .unwrap();
            Team::load(&config, &identity).unwrap()
        };
        let headers = HeaderMap::new();
        let selected = |team: &Team, named| {
            super::person(team, named, &headers)
                .map(|member| member.person_id.clone())
                .unwrap_or_else(|_| panic!("expected a member: {named:?}"))
        };
        let mut team = load("");
        assert_eq!(selected(&team, Some("Aiko")), "aiko");
        assert_eq!(selected(&team, None), "akira");
        team.members.retain(|member| member.person_id != "akira");
        assert_eq!(selected(&team, None), "yuki");
        team.members.retain(|member| member.person_id != "yuki");
        assert!(super::person(&team, None, &headers).is_err());
        let team = load("yuki");
        assert_eq!(selected(&team, None), "yuki");
        assert_eq!(selected(&team, Some("akira")), "akira");
        assert!(super::person(&team, Some("unknown"), &headers).is_err());
        assert!(super::person(&load("removed"), None, &headers).is_err());
    }

    #[tokio::test]
    async fn command_entry_holds_during_success_failure_and_cancellation() {
        use std::sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        };
        let root = tempfile::tempdir().unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let _factory = crate::keep_awake::test_count(root.path(), count.clone());
        for fail in [false, true] {
            let result = super::execute(root.path(), async {
                assert_eq!(count.load(Ordering::SeqCst), 1);
                if fail { Err(()) } else { Ok(()) }
            })
            .await;
            assert_eq!(result.is_err(), fail);
            assert_eq!(count.load(Ordering::SeqCst), 0);
        }
        let entered = tokio_util::sync::CancellationToken::new();
        let ready = entered.clone();
        let held = count.clone();
        let workspace = root.path().to_owned();
        let task = tokio::spawn(async move {
            super::execute(&workspace, async {
                assert_eq!(held.load(Ordering::SeqCst), 1);
                ready.cancel();
                std::future::pending::<()>().await;
            })
            .await;
        });
        entered.cancelled().await;
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        crate::keep_awake::wait_released(&count).await;
    }

    #[tokio::test]
    async fn manual_http_entry_acquires_and_releases_even_when_device_preflight_refuses() {
        use std::sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        };
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let native = crate::member::native::tests::fixture(root.path());
        let workspace = native.workspace.clone();
        let path = workspace.join(".guildbotics/config/commands/functions/probe.py");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, "async def main(context):\n    return 'done'\n").unwrap();
        let host = Arc::new(gb_environment::Host {
            data: native.data.clone(),
            home: root.path().join("home"),
            package: native.package.clone(),
            identity_root: native.identity_root.clone(),
        });
        let count = Arc::new(AtomicUsize::new(0));
        let factory = crate::keep_awake::test_count(&workspace, count.clone());
        let api = Arc::new(crate::api::Api::new(
            crate::api::Selected {
                workspace: Some(workspace),
                service: crate::service_runtime::Controller::new(native.clone(), host),
                ..Default::default()
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            crate::auth::ServerAuth::open(&native.data, 8765).unwrap(),
            "fixture".into(),
            native.data,
        ));
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let response = crate::api::router(api.clone())
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/api/commands/run")
                    .header("host", "127.0.0.1:8765")
                    .header("content-type", "application/json")
                    .header("authorization", format!("Bearer {}", token.trim()))
                    .body(axum::body::Body::from(
                        serde_json::json!({"command":"functions/probe", "person":"aiko"})
                            .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::BAD_REQUEST);
        assert_eq!(factory.1.load(Ordering::SeqCst), 1);
        assert_eq!(count.load(Ordering::SeqCst), 0);
        api.work.close();
        api.work.wait().await;
    }

    #[test]
    fn image_status_diagnostics_are_rendered_in_the_requested_language() {
        use gb_config::agent_environment::{BaseImage, DeclarationProblem, ImageIssue};
        let image = BaseImage {
            reference: "local/agent:1".into(),
            digests: [("arm64".into(), format!("sha256:{}", "a".repeat(64)))].into(),
        };
        let command =
            gb_environment::image::load_command(std::path::Path::new("/home/a user's")).unwrap();
        let mismatch = gb_environment::image::status(
            &image,
            "arm64",
            Some(format!("sha256:{}", "b".repeat(64))),
            &command,
        );
        let missing = gb_environment::image::status(&image, "arm64", None, &command);
        let undeclared =
            gb_environment::image::status(&image, "amd64", Some("sha256:bbbb".into()), &command);
        let mut sentences = vec![
            missing.refusal.unwrap(),
            mismatch.warning.unwrap(),
            undeclared.warning.unwrap(),
        ];
        for issue in [
            ImageIssue::Reference("-bad-reference".into()),
            ImageIssue::Digest("bad-digest".into()),
            ImageIssue::Architecture("ARM".into()),
            ImageIssue::NoDigests,
        ] {
            sentences.push(gb_environment::image::declaration_problem(
                DeclarationProblem::ImageField("/config/agent_environment.yml".into(), issue),
            ));
        }
        sentences.push(gb_environment::image::declaration_problem(
            DeclarationProblem::Image("/config/file.yml".into()),
        ));
        for sentence in sentences {
            let guidance = matches!(&sentence, Sentence::Key(key, _) if key.ends_with("image.missing") || key.ends_with("image.mismatch"));
            let source = match &sentence {
                Sentence::AtPath(path, _) => Some(path.display().to_string()),
                _ => None,
            };
            let en = super::say(sentence.clone(), "en");
            let ja = super::say(sentence, "ja");
            assert_ne!(en, ja);
            for text in [&en, &ja] {
                if guidance {
                    assert!(text.contains(&command), "missing CLI guidance: {text}");
                }
                if let Some(path) = &source {
                    assert!(text.starts_with(&format!("{path}: ")), "{text}");
                }
                assert!(!text.contains("%{"), "{text}");
                assert!(!text.starts_with("intelligences."), "{text}");
            }
        }
        let sentence = gb_environment::image::status(&image, "arm64", None, &command)
            .refusal
            .unwrap();
        for language in ["ja", "en"] {
            let text = super::say(sentence.clone(), language);
            assert!(text.contains("local/agent:1") && text.contains("arm64"));
            assert!(text.contains(&command), "missing CLI guidance: {text}");
        }
    }
    use super::*;

    fn failure(command: bool, type_: &str, result: Option<serde_json::Value>) -> CommandFailure {
        CommandFailure {
            cli_agent: "codex".into(),
            cli_agent_message: "refused".into(),
            cli_agent_result: result.map(|r| r.as_object().unwrap().clone()),
            command,
            message: "it failed".into(),
            type_: type_.into(),
            inference_error_type: String::new(),
            inference_category: String::new(),
        }
    }

    fn tool(category: &str) -> Option<serde_json::Value> {
        Some(json!({"stdout": "", "stderr": "", "returncode": 1, "error_category": category}))
    }

    /// As the Python version rebuilds a failure in the environment
    /// (`command_runner._failure`) and classifies it
    /// (`command_failure_payload`).
    #[test]
    fn a_failure_in_the_environment_is_named_and_answered_as_the_host_rebuilds_it() {
        let cases = [
            // A command's own failure, whatever its type, is a CommandError.
            (
                failure(true, "PersonNotFoundError", None),
                "CommandError",
                "",
                true,
            ),
            // An AI CLI tool's, reported in full, is the tool's; its
            // refused login is what the credential alert reads.
            (
                failure(false, "CliAgentExecutionError", tool("authentication")),
                "CliAgentExecutionError",
                "cli_agent_authentication",
                true,
            ),
            (
                failure(false, "CliAgentExecutionError", tool("rate_limit")),
                "CliAgentExecutionError",
                "",
                true,
            ),
            // A command's own failure from a tool's still names the login.
            (
                failure(true, "CommandError", tool("authentication")),
                "CommandError",
                "cli_agent_authentication",
                true,
            ),
            // A tool's failure not reported in full is any other error.
            (
                failure(false, "CliAgentExecutionError", Some(json!({"stdout": ""}))),
                "CliAgentExecutionError",
                "",
                false,
            ),
            (failure(false, "ValueError", None), "ValueError", "", false),
        ];
        for (failure, error_type, code, told) in cases {
            let ended = failed_in_environment(failure);
            assert_eq!(
                (ended.error_type.as_str(), ended.code, ended.answer.is_ok()),
                (error_type, code, told)
            );
        }
    }

    #[test]
    fn only_an_answered_failure_carries_its_message() {
        use gb_observability::Failure as _;
        let ended = |answer| Ended {
            error_type: "CommandError".into(),
            code: "",
            answer,
        };
        assert_eq!(ended(Ok("boom".into())).message(), Some("boom".into()));
        assert_eq!(ended(Err("secret details".into())).message(), None);
    }

    #[tokio::test]
    async fn a_defect_is_answered_without_its_words() {
        let response = internal("secret details", "AgentRuntimeError");
        assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<serde_json::Value>(&body).unwrap(),
            json!({
                "code": "internal_error",
                "message": "An unexpected app API error occurred.",
                "context": {"error_type": "AgentRuntimeError"},
            })
        );
    }
}
