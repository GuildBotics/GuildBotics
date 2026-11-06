//! The command and diagnostics screens' read API.

use crate::api::{ApiError, Shared};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use gb_config::{commands::CommandRoots, team::Team};
use gb_observability::Diagnostics;
use serde::Deserialize;
use serde_json::{Value, json};

fn roots(api: &Shared) -> Option<CommandRoots> {
    Some(CommandRoots {
        config: api
            .selected()
            .workspace
            .as_ref()?
            .join(".guildbotics/config"),
        templates: gb_base::home::python_package().join("templates"),
    })
}

fn loaded(api: &Shared) -> anyhow::Result<Team> {
    Team::load(
        &api.selected()
            .workspace
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("No workspace"))?
            .join(".guildbotics/config"),
        &gb_base::home::identity_root()?,
    )
}

fn answer(result: anyhow::Result<Value>) -> Response {
    match result {
        Ok(value) => Json(value).into_response(),
        Err(e) => {
            tracing::error!("cannot read screen data: {e:#}");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

pub(crate) async fn config_status(State(api): State<Shared>) -> Response {
    Json(status(&api).await).into_response()
}

/// Where the selected workspace keeps what, as the setup screens show it.
pub(crate) async fn status(api: &crate::api::Api) -> Value {
    let input_store_problem = api.inputs.problem().await;
    let workspace = &api.selected().workspace;
    let config = workspace.as_ref().map(|w| w.join(".guildbotics/config"));
    let project = config.as_ref().map(|c| c.join("team/project.yml"));
    json!({"workspace_problem":"","input_store_problem":input_store_problem,
        "cwd":api.home.join("Documents/GuildBotics"),
        "workspace":workspace,"config_dir":config,"project_file":project,
        "project_file_exists":project.as_ref().is_some_and(|p| p.exists()),"storage_dir":workspace,
        "machine_state_dir":api.data,
        "workspace_state_dir":workspace.as_ref().map(|w|w.join(".guildbotics/state")),
        "workspace_local_dir":workspace.as_ref().map(|w|w.join(".guildbotics/local"))})
}

/// The team as the command executor picker shows it. A member whose
/// configuration cannot be loaded, or whose identity this machine does not
/// have, is listed too, and the `problem` says why, so that it can be
/// repaired or taken out.
pub(crate) async fn team(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let Some(workspace) = api.selected().workspace.clone() else {
        return Json(empty_team()).into_response();
    };
    let config = workspace.join(".guildbotics/config");
    if !config.join("team/project.yml").exists() {
        return Json(empty_team()).into_response();
    }
    let language = crate::i18n::language(
        headers
            .get("x-guildbotics-language")
            .and_then(|v| v.to_str().ok())
            .unwrap_or(""),
    );
    let identity_root = api.identity_root.clone();
    let stored = match crate::config_api::members(&api, workspace).await {
        Ok(stored) => stored,
        Err(e) => return answer(Err(e)),
    };
    let summary = |member: &gb_setup::PersonSummary| {
        let mut roles = member.roles.clone();
        roles.sort();
        json!({"person_id": member.person_id, "name": member.name, "person_type": member.person_type,
            "is_active": member.is_active, "roles": roles})
    };
    answer((|| match Team::load(&config, &identity_root) {
        Ok(team) => {
            let mut answer = team.screen_summary(&config, &identity_root)?;
            let waiting: Vec<_> = stored.iter().filter(|m| !m.identity).collect();
            let members = answer["members"].as_array_mut().expect("members");
            members.extend(waiting.iter().map(|m| summary(m)));
            answer["problem"] = json!(
                waiting
                    .iter()
                    .map(|m| {
                        let path = identity_root
                            .join(".guildbotics/config/team/members")
                            .join(&m.person_id)
                            .join("identity.yml");
                        crate::i18n::t(
                            language,
                            "member_config.identity_missing",
                            &[("path", &path.display().to_string())],
                        )
                    })
                    .collect::<Vec<_>>()
                    .join("\n")
            );
            Ok(answer)
        }
        Err(error)
            if error
                .downcast_ref::<gb_config::ConfigFile>()
                .is_some_and(|file| {
                    file.path().components().any(|c| c.as_os_str() == "members")
                }) =>
        {
            let project = std::fs::read_to_string(config.join("team/project.yml"))
                .ok()
                .and_then(|text| gb_config::yaml_value(&text).ok())
                .unwrap_or(json!({}));
            let code = gb_config::team::language_code(project["language"].as_str().unwrap_or(""));
            Ok(
                json!({"project": {"name": project["name"].as_str().unwrap_or(""),
                    "language_code": code,
                    "language_name": match code.as_str() {"ja" => "日本語", "en" => "English", other => other}},
                    "members": stored.iter().map(summary).collect::<Vec<_>>(),
                    "default_person_id": "", "problem": format!("{error}")}),
            )
        }
        Err(error) => Err(error),
    })())
}

fn empty_team() -> Value {
    json!({"project":{"name":"","language_code":"en","language_name":"English"},"members":[],"default_person_id":"","problem":""})
}

pub(crate) async fn files(State(api): State<Shared>) -> Response {
    answer((|| {
        let team = loaded(&api)?;
        roots(&api).unwrap().shared_files(&team.language)
    })())
}

pub(crate) async fn file(
    State(api): State<Shared>,
    Path(id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let team = match loaded(&api) {
        Ok(t) => t,
        Err(e) => return answer(Err(e)),
    };
    let roots = roots(&api).unwrap();
    let Some(path) = roots.selected_file(&id, &team.language) else {
        return ApiError::new(StatusCode::NOT_FOUND, "command_file_not_found", &headers)
            .key("command_file_not_found.unknown", vec![("file_id", id)])
            .into_response();
    };
    answer(roots.file_detail(&path, &team.language))
}

#[derive(Default, Deserialize)]
pub(crate) struct Filters {
    source: Option<String>,
    person_id: Option<String>,
    q: Option<String>,
    attr_key: Option<String>,
    attr_value: Option<String>,
    limit: Option<usize>,
}

pub(crate) async fn traces(
    State(api): State<Shared>,
    query: Result<Query<Filters>, axum::extract::rejection::QueryRejection>,
    headers: HeaderMap,
) -> Response {
    let filters = match query {
        Ok(Query(f)) if f.limit.is_none_or(|l| (1..=1000).contains(&l)) => f,
        _ => {
            return ApiError::new(
                StatusCode::UNPROCESSABLE_ENTITY,
                "validation_error",
                &headers,
            )
            .into_response();
        }
    };
    let Some(workspace) = &api.selected().workspace else {
        return Json(json!({"traces":[]})).into_response();
    };
    answer(
        Diagnostics::open(workspace)
            .traces(
                filters.source.as_deref(),
                filters.person_id.as_deref(),
                filters.q.as_deref(),
                filters.attr_key.as_deref(),
                filters.attr_value.as_deref(),
                filters.limit.unwrap_or(200),
            )
            .map_err(Into::into),
    )
}

pub(crate) async fn trace(State(api): State<Shared>, Path(id): Path<String>) -> Response {
    let Some(workspace) = &api.selected().workspace else {
        return Json(
            json!({"trace_id":id,"summary":null,"records":[],"transcript_available":false}),
        )
        .into_response();
    };
    answer(
        Diagnostics::open(workspace)
            .trace_detail(&id)
            .map_err(Into::into),
    )
}
