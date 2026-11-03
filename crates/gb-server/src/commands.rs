//! `POST /api/commands/run`: one command the screens or the command line
//! start, as manual work under a trace of its own, in a microVM of its own,
//! as the Python version runs it (`AppRuntime.run_command` of
//! `guildbotics/app_api/runtime.py`).
//!
//! The command is resolved once, for the member it runs as, before anything
//! is recorded: a command that cannot be resolved never ran and leaves no
//! trace. What the screens add on top -- the check that the file shown is
//! the file run, the files handed over -- is not served yet, and a request
//! that asks for it is refused rather than run without it.

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
    if request.command.is_empty() {
        return Err(error(StatusCode::UNPROCESSABLE_ENTITY, "validation_error").into_response());
    }
    // The run is accepted for the workspace it was asked for before anything
    // is derived from it.
    if let Some(expected) = &request.expected_workspace {
        let same = api.workspace.as_ref().is_some_and(|workspace| {
            std::fs::canonicalize(expected)
                .ok()
                .zip(std::fs::canonicalize(workspace).ok())
                .is_some_and(|(expected, selected)| expected == selected)
        });
        if !same {
            return Err(error(StatusCode::CONFLICT, "command_workspace_changed").into_response());
        }
    }
    let Some(workspace) = &api.workspace else {
        return Err(error(StatusCode::BAD_REQUEST, "config_not_found")
            .context(json!({"path": ""}))
            .into_response());
    };
    let home = std::env::var_os("HOME")
        .filter(|home| !home.is_empty())
        .map(PathBuf::from)
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
    let host = gb_environment::Host {
        data: api.data.clone(),
        home,
        package: gb_base::home::python_package(),
        identity_root: gb_base::home::identity_root()
            .map_err(|e| internal(&format!("{e:#}"), "RuntimeError"))?,
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
    let resolved = roots
        .resolve(&request.command, &team.language, Some(&person.person_id))
        .and_then(|path| Ok((command_access(&path)?, path)));
    let (access, path) = resolved.map_err(|problem| {
        error(StatusCode::BAD_REQUEST, "command_error")
            .reason(problem.to_string())
            .into_response()
    })?;
    // A stop that does not wait for the work refuses work not started yet.
    if api.cancelled.is_cancelled() {
        return Err(error(StatusCode::SERVICE_UNAVAILABLE, "command_cancelled").into_response());
    }
    let trace = Trace::open(&Diagnostics::open(workspace), "manual")
        .person(&person.person_id)
        .attribute("run.entry", "command");
    let trace_id = trace.id().to_owned();
    let command = gb_environment::Command {
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
    let work = api.work.spawn(async move {
        let run = trace.run_command(&name, &person_id, async {
            let reply = gb_environment::run(&host, &workspace, command)
                .await
                .map_err(|failure| failed(failure, language))?;
            match reply.failure {
                None => Ok(reply.text_output),
                Some(failure) => Err(failed_in_environment(failure)),
            }
        });
        // The run first: once started, it is recorded however it ends.
        tokio::select! {
            biased;
            ran = run => Some(ran),
            () = cancelled.cancelled() => None,
        }
    });
    let ran = match work.await {
        Ok(Some(ran)) => ran,
        Ok(None) => {
            return Err(error(StatusCode::SERVICE_UNAVAILABLE, "command_cancelled")
                .context(json!({"trace_id": trace_id}))
                .into_response());
        }
        Err(panicked) => return Err(internal(&panicked.to_string(), "RuntimeError")),
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
fn failed(failure: Failure, language: &str) -> Ended {
    let say = |sentence: Sentence, language: &str| match sentence {
        Sentence::Text(text) => text,
        Sentence::Key(key, params) => {
            let params: Vec<(&str, &str)> = params.iter().map(|(k, v)| (*k, v.as_str())).collect();
            i18n::t(language, key, &params)
        }
    };
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
