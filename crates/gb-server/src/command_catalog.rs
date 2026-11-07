//! What the command screens offer and whether a selected file can run, as
//! the Python version answers them (`guildbotics/app_api/command_catalog.py`):
//! `GET /api/commands/options`, `GET /api/commands/routine-options` and
//! `GET /api/commands/files/{id}/execution-status`. What a command needs is
//! satisfied as this device and the project can provide it, read once per
//! answer.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::Json;
use axum::extract::{Path as UrlPath, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use gb_config::catalog::{Needs, validate_source};
use gb_config::command_files::{logical_name, revision};
use gb_config::commands::CommandRoots;
use gb_config::team::Team;
use gb_environment::{Configuration, Host};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::api::{ApiError, Shared};
use crate::i18n;

/// The command a routine runs when the screen picks none.
const TICKET_WORKFLOW: &str = "workflows/ticket_driven_workflow";

/// The selected workspace's commands and team, and the host they run on.
pub(crate) struct Workspace {
    pub(crate) roots: CommandRoots,
    pub(crate) team: Team,
    pub(crate) host: Arc<Host>,
    pub(crate) directory: PathBuf,
    /// The keychain holding the machine's key to its secret stores.
    pub(crate) keychain: Arc<dyn gb_config::secrets::Keychain>,
}

/// The host the selected workspace's commands run on.
pub(crate) fn host(api: &crate::api::Api) -> anyhow::Result<Arc<Host>> {
    if let Some(host) = api.selected().service.host() {
        return Ok(host);
    }
    Ok(Arc::new(Host {
        data: api.data.clone(),
        home: api.home.clone(),
        package: gb_base::home::python_package(),
        identity_root: api.identity_root.clone(),
    }))
}

/// The team of `config`, or why it cannot be read: a member's broken file is
/// the person's to fix, as the Python version's `MemberConfigError` is. Read
/// from a run's `copy`, a file is named as the one the person edits.
pub(crate) async fn team(
    config: PathBuf,
    identity_root: PathBuf,
    headers: &HeaderMap,
    copy: Option<&Configuration>,
) -> Result<Team, Response> {
    let spoken = |text: String| match copy {
        Some(copy) => copy.spoken(&text),
        None => text,
    };
    tokio::task::spawn_blocking(move || Team::load(&config, &identity_root))
        .await
        .map_err(|e| crate::commands::internal(&e.to_string(), "RuntimeError"))?
        .map_err(|e| match e.downcast_ref::<gb_config::ConfigFile>() {
            Some(file) => ApiError::new(StatusCode::BAD_REQUEST, "member_config_invalid", headers)
                .reason(spoken(file.to_string()))
                .context(json!({ "path": copy
                    .and_then(|copy| copy.original(file.path()))
                    .unwrap_or_else(|| file.path().to_owned()) }))
                .into_response(),
            None => crate::commands::internal(&spoken(format!("{e:#}")), "ValueError"),
        })
}

/// The selected workspace, or why there is none to read: none is selected
/// or it has no project.
pub(crate) async fn workspace(api: &Shared, headers: &HeaderMap) -> Result<Workspace, Response> {
    let not_found = |path: Value| {
        ApiError::new(StatusCode::BAD_REQUEST, "config_not_found", headers)
            .context(json!({ "path": path }))
            .into_response()
    };
    let Some(directory) = api.selected().workspace.clone() else {
        return Err(not_found(json!("")));
    };
    let config = directory.join(".guildbotics/config");
    let project = config.join("team/project.yml");
    if !project.is_file() {
        return Err(not_found(json!(project)));
    }
    let host =
        host(api).map_err(|e| crate::commands::internal(&format!("{e:#}"), "RuntimeError"))?;
    let team = team(config.clone(), host.identity_root.clone(), headers, None).await?;
    let roots = CommandRoots {
        config,
        templates: host.package.join("templates"),
    };
    Ok(Workspace {
        roots,
        team,
        host,
        directory,
        keychain: api.keychain.clone(),
    })
}

/// What this device and the project provide that commands need, read once.
pub(crate) struct Facts {
    /// Why no command can run on this device; "" when one can.
    environment: String,
    /// Why each AI CLI tool cannot start a turn; "" for one that can.
    tools: BTreeMap<String, String>,
    project: Value,
    /// Whether an LLM provider's API key is set.
    llm: bool,
}

impl Facts {
    pub(crate) async fn read(
        host: &Host,
        workspace: &Path,
        config: &Path,
        keychain: &dyn gb_config::secrets::Keychain,
        language: &str,
    ) -> Facts {
        let device = gb_environment::device::read(host, workspace).await;
        let project = std::fs::read_to_string(config.join("team/project.yml"))
            .ok()
            .and_then(|text| gb_config::yaml_value(&text).ok())
            .unwrap_or(Value::Null);
        let llm = gb_setup::provider_env_keys(
            &host.package.join("templates"),
            &config.join("intelligences/models"),
        )
        .values()
        .any(|key| {
            matches!(
                crate::host_window::host_secret_in(host, workspace, key, keychain),
                Ok(Some(_))
            )
        });
        Facts {
            environment: device
                .refusal
                .map(|s| crate::commands::say(s, language))
                .unwrap_or_default(),
            tools: crate::environment::tool_refusals(host, keychain, language).unwrap_or_default(),
            project,
            llm,
        }
    }

    /// Why the need `kind` (with `tool`) is not met, or `None` when it is.
    fn unmet(&self, kind: &str, tool: &str) -> Option<String> {
        let unless = |met: bool, reason: &str| (!met).then(|| reason.to_owned());
        match kind {
            "environment" => unless(self.environment.is_empty(), &self.environment),
            // A tool the catalog does not know cannot start a turn either.
            "cli_agent" if tool.is_empty() => None,
            "cli_agent" => match self.tools.get(tool) {
                Some(reason) => unless(reason.is_empty(), reason),
                None => Some(String::new()),
            },
            // A name that is there is a service, as `str(name)` reads it.
            "code_hosting_service" | "ticket_service" | "chat_service" => unless(
                self.project
                    .pointer(&format!("/services/{kind}/name"))
                    .is_some_and(|name| name != ""),
                "",
            ),
            "llm" => unless(self.llm, ""),
            _ => None,
        }
    }

    /// Each kind of `needs`, by kind: met when all of its needs are, with
    /// the first reason one is not.
    pub(crate) fn requirements(&self, needs: &Needs) -> Vec<Value> {
        let mut kinds: BTreeMap<&str, (bool, String)> = BTreeMap::new();
        for (kind, tool) in needs {
            let entry = kinds.entry(kind).or_insert((true, String::new()));
            if let Some(reason) = self.unmet(kind, tool) {
                entry.0 = false;
                if entry.1.is_empty() {
                    entry.1 = reason;
                }
            }
        }
        kinds
            .into_iter()
            .map(|(kind, (satisfied, message))| {
                json!({"kind": kind, "satisfied": satisfied, "message": message})
            })
            .collect()
    }
}

/// Why the selected file `path` is not what `command` runs for `person`
/// as the revision `expected` the screen read, with its context, and what it
/// needs: what a screen's run of it is refused for. A run evaluates it in its
/// copy of the configuration, which is what it then runs.
pub(crate) async fn evaluate(
    workspace: &Workspace,
    path: &Path,
    expected: &str,
    command: &str,
    person: Option<&str>,
    language: &str,
) -> anyhow::Result<(Option<(&'static str, Value)>, Vec<Value>)> {
    let bytes = std::fs::read(path)?;
    let current = revision(&bytes);
    if current != expected {
        return Ok((
            Some(("command_file_changed", json!({"current_revision": current}))),
            vec![],
        ));
    }
    let roots = &workspace.roots;
    let runs = roots
        .resolve(command, &workspace.team.language, person)
        .ok();
    let same = runs
        .as_deref()
        .and_then(|runs| runs.canonicalize().ok())
        .zip(path.canonicalize().ok())
        .is_some_and(|(runs, selected)| runs == selected);
    if !same {
        return Ok((
            Some((
                "command_file_shadowed",
                json!({"shadow_source": roots.shadow_source(runs.as_deref())}),
            )),
            vec![],
        ));
    }
    let Ok(text) = String::from_utf8(bytes) else {
        return Ok((
            Some((
                "command_file_invalid_source",
                json!({"message": "Command source must be valid UTF-8.", "reason": "invalid_utf8"}),
            )),
            vec![],
        ));
    };
    let extension = path
        .extension()
        .map(|e| format!(".{}", e.to_string_lossy()))
        .unwrap_or_default();
    if let Err(invalid) = validate_source(&extension, &text) {
        let mut context = invalid.context;
        context.insert("message".into(), invalid.message.into());
        return Ok((Some((invalid.code, Value::Object(context))), vec![]));
    }
    let needs = roots.needs(path, &workspace.team.language, person);
    let facts = Facts::read(
        &workspace.host,
        &workspace.directory,
        &roots.config,
        &*workspace.keychain,
        language,
    )
    .await;
    let requirements = facts.requirements(&needs);
    let missing = requirements.iter().any(|r| r["satisfied"] == false);
    Ok((
        missing.then(|| ("command_requirement_missing", json!({}))),
        requirements,
    ))
}

#[derive(Deserialize)]
pub(crate) struct PersonQuery {
    person: Option<String>,
}

pub(crate) async fn options(
    State(api): State<Shared>,
    headers: HeaderMap,
    Query(query): Query<PersonQuery>,
) -> Response {
    list(&api, &headers, query.person, false).await
}

pub(crate) async fn routine_options(
    State(api): State<Shared>,
    headers: HeaderMap,
    Query(query): Query<PersonQuery>,
) -> Response {
    list(&api, &headers, query.person, true).await
}

/// The commands the member `person` may pick (the routines, for `routine`),
/// each with what it needs.
async fn list(
    api: &Shared,
    headers: &HeaderMap,
    person: Option<String>,
    routine: bool,
) -> Response {
    let workspace = match workspace(api, headers).await {
        Ok(workspace) => workspace,
        Err(refused) => return refused,
    };
    let person = match person.filter(|p| !p.is_empty()) {
        None => None,
        Some(named) => match workspace
            .team
            .members
            .iter()
            .find(|m| m.person_id == named || m.name == named)
        {
            Some(member) => Some(member.person_id.clone()),
            None => {
                let available: Vec<_> = workspace
                    .team
                    .members
                    .iter()
                    .map(|m| m.person_id.clone())
                    .collect();
                return ApiError::new(StatusCode::BAD_REQUEST, "person_not_found", headers)
                    .key("person_not_found.plain", vec![("person", named.clone())])
                    .context(json!({"identifier": named, "available": available}))
                    .into_response();
            }
        },
    };
    let language = language(headers);
    let options = {
        let (roots, project) = (
            CommandRoots {
                config: workspace.roots.config.clone(),
                templates: workspace.roots.templates.clone(),
            },
            workspace.team.language.clone(),
        );
        let person = person.clone();
        tokio::task::spawn_blocking(move || roots.options(&project, person.as_deref(), routine))
            .await
    };
    let options = match options {
        Ok(Ok(options)) => options,
        Ok(Err(e)) => return crate::commands::internal(&format!("{e:#}"), "OSError"),
        Err(e) => return crate::commands::internal(&e.to_string(), "RuntimeError"),
    };
    let facts = Facts::read(
        &workspace.host,
        &workspace.directory,
        &workspace.roots.config,
        &*workspace.keychain,
        language,
    )
    .await;
    let options: Vec<Value> = options
        .into_iter()
        .map(|mut listed| {
            listed.option["requirements"] = facts.requirements(&listed.needs).into();
            listed.option
        })
        .collect();
    if !routine {
        return Json(json!({ "options": options })).into_response();
    }
    let eligible: Vec<&str> = options
        .iter()
        .filter(|o| o["routine_eligible"] == true)
        .filter_map(|o| o["command"].as_str())
        .collect();
    let default = if eligible.contains(&TICKET_WORKFLOW) {
        TICKET_WORKFLOW
    } else {
        eligible.first().copied().unwrap_or("")
    };
    Json(json!({"options": options, "default_command": default})).into_response()
}

#[derive(Deserialize)]
pub(crate) struct StatusQuery {
    expected_revision: Option<String>,
    person: Option<String>,
}

/// Whether the shared file `id`, as the revision the screen read, is what
/// its name runs for the member and can run now.
pub(crate) async fn execution_status(
    State(api): State<Shared>,
    headers: HeaderMap,
    UrlPath(id): UrlPath<String>,
    Query(query): Query<StatusQuery>,
) -> Response {
    let Some(expected) = query.expected_revision else {
        return ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .context(
            json!({"errors": [{"msg": "Field required", "loc": ["query", "expected_revision"]}]}),
        )
        .into_response();
    };
    let workspace = match workspace(&api, &headers).await {
        Ok(workspace) => workspace,
        Err(refused) => return refused,
    };
    let named = query
        .person
        .filter(|p| !p.is_empty())
        .or_else(|| workspace.team.default_person_id().map(str::to_owned));
    let person = match &named {
        None => None,
        Some(named) => match workspace
            .team
            .members
            .iter()
            .find(|m| m.person_id == *named || m.name == *named)
        {
            Some(member) => Some(member.person_id.clone()),
            None => {
                return Json(json!({"matches_selected_file": false, "requirements": [],
                    "blocking_code": "person_not_found", "blocking_context": {"identifier": named}}))
                .into_response();
            }
        },
    };
    let project = &workspace.team.language;
    let Some(path) = workspace.roots.selected_file(&id, project) else {
        return ApiError::new(StatusCode::NOT_FOUND, "command_file_not_found", &headers)
            .key("command_file_not_found.unknown", vec![("file_id", id)])
            .into_response();
    };
    let relative = path
        .strip_prefix(workspace.roots.config.join("commands"))
        .unwrap_or(&path);
    let command = logical_name(relative, project).unwrap_or_default();
    match evaluate(
        &workspace,
        &path,
        &expected,
        &command,
        person.as_deref(),
        language(&headers),
    )
    .await
    {
        Ok((blocked, requirements)) => {
            let (code, context) = blocked.map_or((Value::Null, json!({})), |(code, context)| {
                (json!(code), context)
            });
            Json(
                json!({"matches_selected_file": code.is_null(), "requirements": requirements,
                "blocking_code": code, "blocking_context": context}),
            )
            .into_response()
        }
        Err(e) => crate::commands::internal(&format!("{e:#}"), "OSError"),
    }
}

fn language(headers: &HeaderMap) -> &'static str {
    i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    )
}
