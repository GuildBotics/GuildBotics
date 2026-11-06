//! `POST /api/commands/run`: one command the screens or the command line
//! start, as manual work under a trace of its own, in a microVM of its own,
//! as the Python version runs it (`AppRuntime.run_command` of
//! `guildbotics/app_api/runtime.py`).
//!
//! The command is resolved once, for the member it runs as, before anything
//! is recorded: a command that cannot be resolved never ran and leaves no
//! trace. Screen requests additionally compare the selected shared file's
//! identity and revision with this member's runtime target before starting.

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
use serde::Deserialize;
use serde_json::json;

use crate::api::{ApiError, Body, Shared};
use crate::i18n;

/// Where a run that names no working directory runs: the exchange
/// directory, under the person's home.
const EXCHANGE_DIRECTORY: &str = "Documents/GuildBotics";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RunRequest {
    command: String,
    #[serde(default)]
    args: Vec<String>,
    person: Option<String>,
    #[serde(default)]
    message: String,
    cwd: Option<PathBuf>,
    /// The workspace the caller means, which must still be the selected one.
    expected_workspace: Option<PathBuf>,
    expected_command_file_id: Option<String>,
    expected_command_file_revision: Option<String>,
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

    fn code(&self) -> &str {
        self.code
    }
}

pub(crate) async fn run(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<RunRequest>,
) -> Response {
    start(&api, &headers, request)
        .await
        .unwrap_or_else(|refused| refused)
}

async fn start(
    api: &crate::api::Api,
    headers: &HeaderMap,
    request: RunRequest,
) -> Result<Response, Response> {
    let error = |status, code| ApiError::new(status, code, headers);
    // Admitted for the selected workspace, which cannot be switched until
    // the run is the server's work.
    let admitted = api
        .switching
        .try_read()
        .map_err(|_| error(StatusCode::CONFLICT, "command_workspace_changing").into_response())?;
    let selected = api.selected();
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
        let same = selected.workspace.as_ref().is_some_and(|workspace| {
            std::fs::canonicalize(expected)
                .ok()
                .zip(std::fs::canonicalize(workspace).ok())
                .is_some_and(|(expected, selected)| expected == selected)
        });
        if !same {
            return Err(error(StatusCode::CONFLICT, "command_workspace_changed").into_response());
        }
    }
    let Some(workspace) = &selected.workspace else {
        return Err(error(StatusCode::BAD_REQUEST, "config_not_found")
            .context(json!({"path": ""}))
            .into_response());
    };
    let home = api
        .selected()
        .service
        .host()
        .map(|host| host.home.clone())
        .or_else(|| {
            std::env::var_os("HOME")
                .filter(|home| !home.is_empty())
                .map(PathBuf::from)
        })
        .ok_or_else(|| internal("HOME is not set", "RuntimeError"))?;
    let cwd = match &request.cwd {
        Some(cwd) => working_directory(cwd, &home).map_err(|refused| match refused {
            None => error(StatusCode::BAD_REQUEST, "command_cwd_not_absolute")
                .key(
                    "command_cwd_not_absolute",
                    vec![("cwd", cwd.display().to_string())],
                )
                .into_response(),
            Some(problem) => error(StatusCode::BAD_REQUEST, "command_error")
                .reason(problem)
                .into_response(),
        })?,
        None => home.join(EXCHANGE_DIRECTORY),
    };
    let host = match selected.service.host() {
        Some(host) => host,
        None => std::sync::Arc::new(gb_environment::Host {
            data: api.data.clone(),
            home,
            package: gb_base::home::python_package(),
            identity_root: gb_base::home::identity_root()
                .map_err(|e| internal(&format!("{e:#}"), "RuntimeError"))?,
        }),
    };
    let config = workspace.join(".guildbotics/config");
    let project = config.join("team/project.yml");
    if !project.is_file() {
        return Err(error(StatusCode::BAD_REQUEST, "config_not_found")
            .context(json!({"path": project}))
            .into_response());
    }
    let team = {
        let (config, identity_root) = (config.clone(), host.identity_root.clone());
        tokio::task::spawn_blocking(move || Team::load(&config, &identity_root))
            .await
            .map_err(|e| internal(&e.to_string(), "RuntimeError"))?
            .map_err(|e| internal(&format!("{e:#}"), "ValueError"))?
    };
    let person =
        person(&team, request.person.as_deref(), headers).map_err(|e| e.into_response())?;
    // The one reading of the command: the file it runs and what it declares.
    let roots = CommandRoots {
        config,
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
        let current = gb_config::command_files::revision(
            &std::fs::read(&selected).map_err(|e| internal(&e.to_string(), "OSError"))?,
        );
        if &current != expected {
            return Err(error(StatusCode::CONFLICT, "command_file_changed")
                .key("command_not_runtime_target", vec![])
                .context(json!({"current_revision":current}))
                .into_response());
        }
        if resolved
            .as_ref()
            .ok()
            .is_none_or(|p| p.canonicalize().ok() != selected.canonicalize().ok())
        {
            let shadow = resolved
                .as_ref()
                .ok()
                .map(|p| {
                    if p.starts_with(&roots.templates) {
                        "template"
                    } else if p.starts_with(roots.config.join("commands")) {
                        "workspace"
                    } else {
                        "member"
                    }
                })
                .unwrap_or("workspace");
            return Err(error(StatusCode::CONFLICT, "command_file_shadowed")
                .key("command_not_runtime_target", vec![])
                .context(json!({"shadow_source":shadow}))
                .into_response());
        }
    }
    let resolved = resolved.and_then(|path| Ok((command_access(&path)?, path)));
    let (access, path) = resolved.map_err(|problem| {
        error(StatusCode::BAD_REQUEST, "command_error")
            .reason(problem.to_string())
            .into_response()
    })?;
    // A stop that does not wait for the work refuses work not started yet.
    if api.cancelled.is_cancelled() {
        return Err(error(StatusCode::SERVICE_UNAVAILABLE, "command_cancelled").into_response());
    }
    if request.command == crate::ticket_selector::COMMAND && request.args.is_empty() {
        return manual_ticket(api, headers, &person.person_id).await;
    }
    let trace = Trace::open(&Diagnostics::open(workspace), "manual")
        .person(&person.person_id)
        .attribute("run.entry", "command");
    let trace_id = trace.id().to_owned();
    let command = gb_environment::Command {
        wants_result: false,
        invocation: None,
        person_id: person.person_id.clone(),
        run_id: trace_id.clone(),
        trace_id: trace_id.clone(),
        work_kind: "manual".into(),
        work_identity: trace_id.clone(),
        path,
        name: request.command.clone(),
        access,
        args: request.args,
        cwd,
        pipe: request.message,
    };
    let language = i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    );
    // The run is the server's work, not the request's: it runs to its end
    // when the caller goes away, and a stop waits for it unless it cancels
    // it. A cancelled run's trace says so as it is dropped, and its microVM
    // goes with it.
    let (name, person_id) = (request.command, person.person_id.clone());
    let (workspace, cancelled) = (workspace.clone(), api.cancelled.clone());
    let inputs = api.inputs.mount_if_available().await;
    let member = selected.member.clone();
    let mut admission = crate::execution::Admission {
        generation: None,
        command: name.clone(), workspace: workspace.clone(),
        writes: std::sync::Arc::new(gb_base::shared_writes::SharedWrites::new(host.identity_root.clone(), Some(workspace.clone()))),
        record: serde_json::from_value(json!({"run_id":trace_id,"work_kind":"manual","work_identity":{"run_id":trace_id},"execution_mode":"user_initiated","member_id":person_id,
            "device_id":gb_workspace::identity::ensure_device_identity(&api.data).map_err(|e|internal(&e.to_string(),"RuntimeError"))?.device_id,
            "source":"manual","started_at":chrono::Utc::now().to_rfc3339()})).expect("manual record fields"),
        redactor: crate::host_window::command_redactor(&host, &workspace, &gb_config::secrets::SystemKeychain).map_err(|e|internal(&e,"RuntimeError"))?,
        exclusive: !command.access.read_only, record_start: true, cleanup: api.work.clone(),
    };
    let work = api
        .selected()
        .service
        .execution
        .spawn(move |generation| async move {
            admission.generation = Some(generation.clone());
            let execution =
                crate::execution::Execution::enter(admission, generation.draining.clone()).await?;
            let run = trace.run_command(
                &name,
                &person_id,
                execute(&workspace, async {
                    let roots = gb_config::commands::CommandRoots {
                        config: workspace.join(".guildbotics/config"),
                        templates: host.package.join("templates"),
                    };
                    let device = gb_environment::device::read(&host, &workspace).await;
                    if let Some(refusal) = device.refusal.clone() {
                        return Err(failed(Failure::Command(refusal), language));
                    }
                    let mut services = crate::host_window::prepare_command(
                        &host,
                        &workspace,
                        &command,
                        &roots,
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
                    trace.command_output(&name, &person_id, &reply.text_output);
                    match reply.failure {
                        None => Ok(reply.text_output),
                        Some(failure) => Err(failed_in_environment(failure)),
                    }
                }),
            );
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
    match ran {
        Ok(output) => Ok(Json(json!({"trace_id": trace_id, "output": output})).into_response()),
        Err(Ended {
            answer: Ok(reason), ..
        }) => Err(error(StatusCode::BAD_REQUEST, "command_error")
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
    api: &crate::api::Api,
    headers: &HeaderMap,
    person: &str,
) -> Result<Response, Response> {
    use crate::{dispatcher::DispatchFailure, workflow_runtime::Refused};
    let error = |status, code| ApiError::new(status, code, headers);
    let mut runtime = api
        .selected()
        .service
        .workflow(person, api.work.clone())
        .map_err(|e| internal(&e.to_string(), "RuntimeError"))?;
    let work = api
        .selected()
        .service
        .execution
        .spawn(move |generation| async move {
            runtime.draining = generation.draining.clone();
            runtime.cancelled = generation.cancelled.clone();
            runtime.generation = Some(generation);
            runtime.manual_ticket().await
        })
        .map_err(|_| error(StatusCode::CONFLICT, "work_rejected").into_response())?;
    let result = api
        .work
        .spawn(work)
        .await
        .map_err(|e| internal(&e.to_string(), "RuntimeError"))?
        .map_err(|e| internal(&e.to_string(), "RuntimeError"))?;
    let language = i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    );
    let ended = match result {
        Ok((trace_id, reply)) => {
            return Ok(
                Json(json!({"trace_id":trace_id,"output":reply.text_output})).into_response(),
            );
        }
        Err(Refused::Admission(_)) => {
            return Err(error(StatusCode::CONFLICT, "work_rejected").into_response());
        }
        Err(Refused::Dispatch(DispatchFailure::Cancelled)) => {
            return Err(error(StatusCode::SERVICE_UNAVAILABLE, "command_cancelled").into_response());
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
pub(crate) fn say(sentence: Sentence, language: &str) -> String {
    match sentence {
        Sentence::Text(text) => text,
        Sentence::AtPath(path, reason) => format!("{}: {}", path.display(), say(*reason, language)),
        Sentence::Key(key, params) => {
            let params: Vec<(&str, &str)> = params.iter().map(|(k, v)| (*k, v.as_str())).collect();
            i18n::t(language, key, &params)
        }
    }
}

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
fn internal(what: &str, error_type: &str) -> Response {
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
