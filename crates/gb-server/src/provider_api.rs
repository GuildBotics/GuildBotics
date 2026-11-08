//! Provider setup operations and the browser's one-time consent callback.
use crate::api::{Body, Shared};
use crate::setup_providers::Registry;
use axum::{
    Json,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{Html, IntoResponse, Response},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    time::{Duration, Instant},
};

pub(crate) struct Consent {
    verifier: String,
    language: String,
    redirect: String,
    pub(crate) created: Instant,
    outcome: Option<Result<(), String>>,
}
impl Consent {
    fn expired_pending(&self) -> bool {
        self.outcome.is_none() && self.created.elapsed() > Duration::from_secs(600)
    }
}
fn language(headers: &HeaderMap) -> &str {
    headers
        .get("x-guildbotics-language")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("en")
}
fn text(headers: &HeaderMap, key: &str) -> String {
    crate::i18n::t(language(headers), key, &[])
}
fn error(e: impl Into<anyhow::Error>, headers: &HeaderMap) -> Response {
    crate::config_api::failure(e.into(), headers)
}
fn unavailable() -> gb_setup::SetupError {
    gb_setup::SetupError::new("branvil", "integrations.branvil.api.not_connected")
}
async fn admin(api: &Shared) -> Result<branvil::api::MemberApi, gb_setup::SetupError> {
    Registry(Some(api.clone()))
        .admin()
        .await?
        .map(|(_, m)| m)
        .ok_or_else(unavailable)
}
pub(crate) async fn providers() -> Json<Value> {
    Json(json!([
    {"name":"branvil","title":"Branvil","kinds":["ticket_service","code_hosting_service"],"avatar":false},
    {"name":"slack","title":"Slack","kinds":["chat_service"],"avatar":true}
    ]))
}
#[derive(Deserialize)]
pub(crate) struct Empty {}
pub(crate) async fn connect(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(_): Body<Empty>,
) -> Response {
    let Some(data) = api.branvil.get() else {
        return error(unavailable(), &headers);
    };
    if let Err(e) = data.health() {
        return error(
            super::setup_providers::accounts::refused_for(e, "health"),
            &headers,
        );
    }
    let state = loopback_auth::random_secret::<32>();
    let verifier = loopback_auth::random_secret::<48>();
    let redirect = format!("{}/providers/branvil/pages/connected", api.auth.base_url());
    let mut url =
        url::Url::parse(&format!("{}/-/authorize", data.base_url())).expect("service origin");
    url.query_pairs_mut()
        .append_pair("client", "GuildBotics")
        .append_pair("redirect_uri", &redirect)
        .append_pair("state", &state)
        .append_pair(
            "code_challenge",
            &URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes())),
        )
        .append_pair("code_challenge_method", "S256");
    let mut consents = api.consents.lock().await;
    consents.retain(|_, c| c.created.elapsed() <= Duration::from_secs(600));
    consents.insert(
        state.clone(),
        Consent {
            verifier,
            language: language(&headers).to_owned(),
            redirect,
            created: Instant::now(),
            outcome: None,
        },
    );
    Json(json!({"state":state,"url":url.as_str()})).into_response()
}
#[derive(Deserialize)]
pub(crate) struct ConnectionQuery {
    #[serde(default)]
    state: String,
}
pub(crate) async fn connection(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(query): Body<ConnectionQuery>,
) -> Response {
    let empty = |status: &str, message: String| json!({"status":status,"base_url":api.branvil.get().map(|d|d.base_url()).unwrap_or(""),"owner":"","message":message,"git_config":""});
    {
        let mut consents = api.consents.lock().await;
        if let Some(c) = consents.get(&query.state) {
            if c.expired_pending() {
                let reason = crate::i18n::t(&c.language, "integrations.branvil.setup.expired", &[]);
                consents.remove(&query.state);
                return Json(empty("disconnected", reason)).into_response();
            }
            match &c.outcome {
                None => return Json(empty("pending", String::new())).into_response(),
                Some(Err(reason)) => {
                    let reason = reason.clone();
                    consents.remove(&query.state);
                    return Json(empty("disconnected", reason)).into_response();
                }
                Some(Ok(())) => {
                    consents.remove(&query.state);
                }
            }
        }
    }
    let binary = api.home.join(".guildbotics/bin/guildbotics");
    match Registry(Some(api.clone())).admin().await {
        Ok(Some((data,member)))=>match member.identity().await {
            Ok(owner)=>Json(json!({"status":"connected","base_url":data.base_url(),"owner":owner.name,"message":"","git_config":git_config(data.base_url(),&binary)})).into_response(),
            Err(e) => {
                let failure = crate::setup_providers::accounts::refused_for(e, "identity");
                if failure.status >= 500 { error(failure, &headers) } else {
                    Json(empty("disconnected", crate::setup_providers::accounts::render(failure, language(&headers)))).into_response()
                }
            },
        },
        Ok(None)=>Json(empty("disconnected",String::new())).into_response(),
        Err(e) if e.status >= 500 => error(e, &headers),
        Err(e)=>{let message=crate::setup_providers::accounts::render(e, language(&headers));Json(empty("disconnected",message)).into_response()}
    }
}
fn escaped(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}
pub(crate) async fn connected(
    State(api): State<Shared>,
    headers: HeaderMap,
    Query(query): Query<BTreeMap<String, String>>,
) -> Response {
    let work = api.work.clone();
    let error_headers = headers.clone();
    match work.spawn(connected_task(api, headers, query)).await {
        Ok(response) => response,
        Err(_) => crate::api::ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            &error_headers,
        )
        .into_response(),
    }
}
async fn connected_task(
    api: Shared,
    headers: HeaderMap,
    query: BTreeMap<String, String>,
) -> Response {
    let mut consents = api.consents.lock().await;
    let Some(c) = consents.get_mut(query.get("state").map(String::as_str).unwrap_or("")) else {
        return page(
            &headers,
            Err(text(&headers, "integrations.branvil.setup.expired")),
        );
    };
    let mut headers = headers;
    if let Ok(value) = c.language.parse() {
        headers.insert("x-guildbotics-language", value);
    }
    if c.expired_pending() {
        return page(
            &headers,
            Err(text(&headers, "integrations.branvil.setup.expired")),
        );
    }
    if c.outcome.is_none() {
        let outcome =
            if query.contains_key("error") || query.get("code").is_none_or(|c| c.is_empty()) {
                Err(text(&headers, "integrations.branvil.setup.denied"))
            } else if let Some(data) = api.branvil.get() {
                match data
                    .exchange(branvil::api::TokenRequest {
                        code: query["code"].clone(),
                        redirect_uri: c.redirect.clone(),
                        code_verifier: c.verifier.clone(),
                    })
                    .await
                {
                    Err(e) => Err(crate::setup_providers::accounts::message_for(
                        e,
                        "exchange",
                        language(&headers),
                    )),
                    Ok(token) => {
                        let (api, base) = (api.clone(), data.base_url().to_owned());
                        match tokio::task::spawn_blocking(move || {
                            keep(&api, &base, &token.issued.token)
                        })
                        .await
                        {
                            Ok(Ok(())) => Ok(()),
                            _ => Err(text(&headers, "integrations.branvil.setup.not_kept")),
                        }
                    }
                }
            } else {
                Err(text(&headers, "integrations.branvil.api.not_connected"))
            };
        c.outcome = Some(outcome);
    }
    page(&headers, c.outcome.clone().expect("completed exchange"))
}
fn page(headers: &HeaderMap, outcome: Result<(), String>) -> Response {
    let failed = outcome.is_err();
    let (status, title, detail) = match outcome {
        Ok(()) => (
            StatusCode::OK,
            text(headers, "integrations.branvil.setup.connected"),
            text(headers, "integrations.branvil.setup.close"),
        ),
        Err(reason) => (
            StatusCode::BAD_REQUEST,
            text(headers, "integrations.branvil.setup.failed"),
            reason,
        ),
    };
    let retry = if failed {
        format!(
            "<p>{}</p>",
            escaped(&text(headers, "integrations.branvil.setup.retry"))
        )
    } else {
        String::new()
    };
    (status,Html(format!("<!doctype html><html><meta charset=\"utf-8\"><title>{}</title><h1>{}</h1><p>{}</p>{}</html>",escaped(&title),escaped(&title),escaped(&detail),retry))).into_response()
}
fn keep(api: &Shared, base: &str, token: &str) -> anyhow::Result<()> {
    use gb_config::{
        repository::{ConfigRepository, Revisions},
        secrets::DeviceKey,
    };
    let mut writes = gb_base::shared_writes::SharedWrites::new(api.identity_root.clone(), None);
    writes.timeout = api.write_timeout;
    let repository = ConfigRepository::for_identity(writes);
    let device = DeviceKey {
        machine_root: api.data.parent().unwrap_or(&api.data).to_path_buf(),
        keychain: &*api.keychain,
    };
    repository.write(
        &Revisions::new(),
        |view, writer| {
            repository
                .identity_secrets(device)?
                .update_with(writer, |values| {
                    values.insert("BRANVIL_ADMIN_TOKEN".into(), token.into());
                })?;
            writer.write_json(
                &view.path("connections/branvil.yml")?,
                &json!({"base_url":base}),
            )
        },
        |_| Ok(Revisions::new()),
    )?;
    Ok(())
}
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
    let member = admin(api).await?;
    let mut cursor = None;
    let mut items = vec![];
    loop {
        let page = member
            .page("setup-workspaces".into(), Some(100), cursor.as_deref())
            .map_err(|e| super::setup_providers::accounts::refused_for(e, "page"))?;
        let page = member.repos_list_workspaces(page).await.map_err(|e| {
            super::setup_providers::accounts::refused_for(e, "repos_list_workspaces")
        })?;
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
        || !gb_integrations::valid_repository_path(&format!("{}/name", input.name))
    {
        return error(
            gb_setup::InvalidInput("workspace must be one name, such as acme".into()),
            &headers,
        );
    }
    let action = async {
        let m = admin(&api).await?;
        m.repos_create_workspace(branvil::api::repos::NewWorkspace { name: input.name })
            .await
            .map_err(|e| {
                super::setup_providers::accounts::refused_for(e, "repos_create_workspace")
            })?;
        list_workspaces(&api).await
    };
    match action.await {
        Ok(items) => Json(json!({"workspaces":items})).into_response(),
        Err(e) => error(e, &headers),
    }
}
pub(crate) async fn agents(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(_): Body<Empty>,
) -> Response {
    let action = async {
        let m = admin(&api).await?;
        let mut items = vec![];
        let mut cursor = None;
        loop {
            let p = m
                .page("setup-agents".into(), Some(100), cursor.as_deref())
                .map_err(|e| super::setup_providers::accounts::refused_for(e, "page"))?;
            let p = m
                .members_list(p)
                .await
                .map_err(|e| super::setup_providers::accounts::refused_for(e, "members_list"))?;
            items.extend(
                p.items
                    .into_iter()
                    .filter(|m| m.kind == branvil::api::MemberKind::Agent)
                    .map(|m| m.name),
            );
            cursor = p.next_cursor;
            if cursor.is_none() {
                return Ok::<_, gb_setup::SetupError>(items);
            }
        }
    };
    match action.await {
        Ok(items) => Json(json!({"agents":items})).into_response(),
        Err(e) => error(e, &headers),
    }
}
pub(crate) async fn entry(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(_): Body<Empty>,
) -> Response {
    match async {
        admin(&api)
            .await?
            .entry_link()
            .await
            .map_err(|e| super::setup_providers::accounts::refused_for(e, "entry_link"))
    }
    .await
    {
        Ok(link) => Json(link).into_response(),
        Err(e) => error(e, &headers),
    }
}

pub(crate) mod slack;

pub(crate) const CALLBACK_PATH: &str = "/providers/branvil/pages/connected";
#[cfg(test)]
pub(crate) const CONFIG_WRITERS: &[(&str, &str)] = &[
    ("get", CALLBACK_PATH),
    ("post", "/config/members/{name}/avatar/{provider}"),
];

fn git_config(origin: &str, binary: &std::path::Path) -> String {
    let helper = format!(
        "!{}",
        gb_base::shell::managed_cli_command(binary, &["git-credential"])
    )
    .replace('\\', "\\\\")
    .replace('"', "\\\"");
    format!("[credential \"{origin}\"]\n\thelper =\n\thelper = \"{helper}\"\n")
}
