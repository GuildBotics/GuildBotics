//! Provider setup operations.
use crate::api::{Body, Shared};
use axum::{
    Json,
    extract::State,
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::Duration;

fn error(e: impl Into<anyhow::Error>, headers: &HeaderMap) -> Response {
    crate::config_api::failure(e.into(), headers)
}
fn refused(e: branvil::api::AppError, operation: &str) -> gb_setup::SetupError {
    crate::setup_providers::refused_for(e, operation)
}
/// The embedded service, acted on as its owner.
fn owner(api: &Shared) -> Result<branvil::api::MemberApi, gb_setup::SetupError> {
    api.branvil
        .get()
        .map(|data| data.owner())
        .ok_or_else(|| gb_setup::SetupError::new("branvil", "integrations.branvil.api.stopped"))
}
pub(crate) async fn providers() -> Json<Value> {
    Json(json!([
    {"name":"branvil","title":"Branvil","kinds":["ticket_service","code_hosting_service"],"avatar":false},
    {"name":"slack","title":"Slack","kinds":["chat_service"],"avatar":true}
    ]))
}
#[derive(Deserialize)]
pub(crate) struct Empty {}
pub(crate) async fn workspaces(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(_): Body<Empty>,
) -> Response {
    match list_workspaces(&api).await {
        Ok(items) => Json(json!({"workspaces":items})).into_response(),
        Err(e) => error(e, &headers),
    }
}
async fn list_workspaces(api: &Shared) -> Result<Vec<String>, gb_setup::SetupError> {
    let member = owner(api)?;
    let mut cursor = None;
    let mut items = vec![];
    loop {
        let page = member
            .page("setup-workspaces".into(), Some(100), cursor.as_deref())
            .map_err(|e| refused(e, "page"))?;
        let page = member
            .repos_list_workspaces(page)
            .await
            .map_err(|e| refused(e, "repos_list_workspaces"))?;
        items.extend(page.items.into_iter().map(|v| v.name));
        cursor = page.next_cursor;
        if cursor.is_none() {
            return Ok(items);
        }
    }
}
#[derive(Deserialize)]
pub(crate) struct NewWorkspace {
    name: String,
}
pub(crate) async fn create_workspace(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(input): Body<NewWorkspace>,
) -> Response {
    if input.name.contains('/')
        || !branvil::agent::port::valid_repository_path(&format!("{}/name", input.name))
    {
        return error(
            gb_setup::InvalidInput("workspace must be one name, such as acme".into()),
            &headers,
        );
    }
    let action = async {
        let m = owner(&api)?;
        m.repos_create_workspace(branvil::api::repos::NewWorkspace { name: input.name })
            .await
            .map_err(|e| refused(e, "repos_create_workspace"))?;
        list_workspaces(&api).await
    };
    match action.await {
        Ok(items) => Json(json!({"workspaces":items})).into_response(),
        Err(e) => error(e, &headers),
    }
}
/// What the owner adds to their git configuration for their own git to
/// authenticate to the embedded service through this machine's GuildBotics.
pub(crate) async fn git_config(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(_): Body<Empty>,
) -> Response {
    match api.branvil.get() {
        Some(data) => Json(json!({"git_config": git_config_text(
            data.base_url(),
            &gb_base::home::managed_cli(api.data.parent().unwrap_or(&api.data)),
        )}))
        .into_response(),
        None => error(
            gb_setup::SetupError::new("branvil", "integrations.branvil.api.stopped"),
            &headers,
        ),
    }
}
pub(crate) mod slack;

#[cfg(test)]
pub(crate) const CONFIG_WRITERS: &[(&str, &str)] =
    &[("post", "/config/members/{name}/avatar/{provider}")];

fn git_config_text(origin: &str, binary: &std::path::Path) -> String {
    let helper = format!(
        "!{}",
        gb_base::shell::managed_cli_command(binary, &["git-credential"])
    )
    .replace('\\', "\\\\")
    .replace('"', "\\\"");
    format!("[credential \"{origin}\"]\n\thelper =\n\thelper = \"{helper}\"\n")
}
