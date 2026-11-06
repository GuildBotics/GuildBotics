//! Changes to the shared command files from the command screens, as the
//! Python version makes them (`AppRuntime._write_shared_commands`):
//! `POST /api/commands/files`, `PUT` and `DELETE /api/commands/files/{id}`
//! and `POST /api/commands/author/apply`. Each runs under the workspace's
//! shared write lock without comparing config revisions: a change names the
//! revision of the very file it read instead.

use axum::Json;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use gb_base::shared_writes::Writer;
use gb_config::command_writes::{Change, Format, Operation};
use gb_config::commands::CommandRoots;
use gb_config::repository::Revisions;
use serde::Deserialize;
use serde_json::{Value, json};

use crate::api::{ApiError, Body, Shared};
use crate::config_api::{failure, unconfigured, with_setup};

/// The most a request carrying one file may hold: its content escaped as
/// JSON, up to six bytes per character, so that a file too large is refused
/// as one.
pub(crate) const FILE_REQUEST_LIMIT: usize = 6 * gb_config::command_writes::MAX_BYTES + (64 << 10);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CreateRequest {
    command: String,
    format: Format,
    content: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct UpdateRequest {
    content: String,
    expected_revision: String,
}

#[derive(Deserialize)]
pub(crate) struct DeleteQuery {
    expected_revision: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ApplyRequest {
    changes: Vec<Change>,
}

pub(crate) async fn create(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<CreateRequest>,
) -> Response {
    write(&api, &headers, move |roots, writer, language| {
        roots.create_file(
            writer,
            &request.command,
            request.format,
            request.content.as_deref(),
            language,
        )
    })
    .await
}

pub(crate) async fn update(
    State(api): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Body(request): Body<UpdateRequest>,
) -> Response {
    write(&api, &headers, move |roots, writer, language| {
        roots.update_file(
            writer,
            &id,
            &request.content,
            &request.expected_revision,
            language,
        )
    })
    .await
}

pub(crate) async fn delete(
    State(api): State<Shared>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Query(query): Query<DeleteQuery>,
) -> Response {
    let Some(expected) = query.expected_revision else {
        return invalid(&headers, "expected_revision is required");
    };
    write(&api, &headers, move |roots, writer, language| {
        roots.delete_file(writer, &id, &expected, language)
    })
    .await
}

pub(crate) async fn apply(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<ApplyRequest>,
) -> Response {
    if !(1..=10).contains(&request.changes.len()) {
        return invalid(&headers, "changes must hold 1 to 10 changes");
    }
    for change in &request.changes {
        let names_a_file = !change.file_id.is_empty() || !change.expected_revision.is_empty();
        let names_both = !change.file_id.is_empty() && !change.expected_revision.is_empty();
        let valid = match change.operation {
            Operation::Update => names_both,
            Operation::Create => !names_a_file,
        };
        if !valid {
            return invalid(
                &headers,
                "an update names file_id and expected_revision, a create neither",
            );
        }
    }
    write(&api, &headers, move |roots, writer, language| {
        roots.apply_changes(writer, &request.changes, language)
    })
    .await
}

/// Runs `change` on the selected workspace's shared commands under its
/// shared write lock, in the project's language.
async fn write(
    api: &Shared,
    headers: &HeaderMap,
    change: impl FnOnce(&CommandRoots, &mut Writer<'_>, &str) -> anyhow::Result<Value> + Send + 'static,
) -> Response {
    if api.selected().workspace.is_none() {
        return unconfigured(headers, "workspace_not_configured.commands");
    }
    let workspace = match crate::command_catalog::workspace(api, headers).await {
        Ok(workspace) => workspace,
        Err(refused) => return refused,
    };
    let (roots, language) = (workspace.roots, workspace.team.language);
    let written = with_setup(api, workspace.directory, move |setup| {
        setup.repository.write(
            &Revisions::new(),
            |_, writer| change(&roots, writer, &language),
            |_| Ok(Revisions::new()),
        )
    })
    .await;
    match written {
        Ok(receipt) => Json(receipt.result).into_response(),
        Err(error) => failure(error, headers),
    }
}

fn invalid(headers: &HeaderMap, reason: &str) -> Response {
    ApiError::new(
        StatusCode::UNPROCESSABLE_ENTITY,
        "validation_error",
        headers,
    )
    .context(json!({"errors": [{"msg": reason}]}))
    .into_response()
}
