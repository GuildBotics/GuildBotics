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
        config: api.workspace.as_ref()?.join(".guildbotics/config"),
        templates: gb_base::home::python_package().join("templates"),
    })
}

fn loaded(api: &Shared) -> anyhow::Result<Team> {
    Team::load(
        &api.workspace
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
    let input_store_problem = api.inputs.problem().await;
    let workspace = &api.workspace;
    let config = workspace.as_ref().map(|w| w.join(".guildbotics/config"));
    let project = config.as_ref().map(|c| c.join("team/project.yml"));
    Json(json!({"workspace_problem":"","input_store_problem":input_store_problem,
        "cwd":std::env::var_os("HOME").map(std::path::PathBuf::from).map(|h| h.join("Documents/GuildBotics")),
        "workspace":workspace,"config_dir":config,"project_file":project,
        "project_file_exists":project.as_ref().is_some_and(|p| p.exists()),"storage_dir":workspace,
        "machine_state_dir":api.data,
        "workspace_state_dir":workspace.as_ref().map(|w|w.join(".guildbotics/state")),
        "workspace_local_dir":workspace.as_ref().map(|w|w.join(".guildbotics/local"))})).into_response()
}

pub(crate) async fn team(State(api): State<Shared>) -> Response {
    if api.workspace.is_none()
        || !api
            .workspace
            .as_ref()
            .unwrap()
            .join(".guildbotics/config/team/project.yml")
            .exists()
    {
        return Json(json!({"project":{"name":"","language_code":"en","language_name":"English"},"members":[],"default_person_id":"","problem":""})).into_response();
    }
    answer((|| {
        loaded(&api)?.screen_summary(
            &roots(&api).unwrap().config,
            &gb_base::home::identity_root()?,
        )
    })())
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
    let Some(workspace) = &api.workspace else {
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
    let Some(workspace) = &api.workspace else {
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
