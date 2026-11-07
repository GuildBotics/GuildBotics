//! Owner-only intelligence settings, credential preparation and grant preview.
use crate::{
    api::{ApiError, Body, Shared},
    config_api::{composed_in, failure, project, with_setup},
};
use axum::{
    Json,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use gb_config::repository::Revisions;
use gb_setup::{
    SetupError,
    intelligences::{IntelligenceConfig, Update},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

const GRANTS: &str = "intelligences/cli_agent_filesystem_grants.yml";
const LOCAL: &str = "cli_agent_filesystem_grants.yml";
const CREDENTIAL: &str = "TYPESAFE_API_KEY";
fn read(path: &Path, default: Value) -> anyhow::Result<Value> {
    match std::fs::read_to_string(path) {
        Ok(s) => Ok(gb_config::yaml_value(&s)?),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(default),
        Err(e) => Err(e.into()),
    }
}
fn error(code: &str, error: impl std::fmt::Display) -> anyhow::Error {
    SetupError::text(code, error.to_string()).into()
}
fn grants(workspace: &Path) -> anyhow::Result<(Value, Value)> {
    let shared = read(
        &workspace.join(".guildbotics/config").join(GRANTS),
        json!({"documents":[]}),
    )?;
    let local = read(
        &workspace.join(".guildbotics/local").join(LOCAL),
        json!({"paths":[],"deny":[]}),
    )?;
    let shared: gb_environment::contract::Shared = serde_json::from_value(if shared.is_object() {
        shared
    } else {
        json!({})
    })
    .map_err(|e| error("invalid_filesystem_grants", e))?;
    let local: gb_environment::contract::Local =
        serde_json::from_value(if local.is_object() { local } else { json!({}) })
            .map_err(|e| error("invalid_filesystem_grants", e))?;
    gb_environment::contract::validate_names(&shared, &local, workspace)
        .map_err(|e| error("invalid_filesystem_grants", e))?;
    Ok((json!(shared), json!(local)))
}

#[derive(Deserialize)]
pub(crate) struct Scope {
    person_id: Option<String>,
}
pub(crate) async fn get(
    State(api): State<Shared>,
    headers: HeaderMap,
    Query(scope): Query<Scope>,
) -> Response {
    if scope
        .person_id
        .as_deref()
        .is_some_and(|p| !gb_config::team::valid_person_id(p))
    {
        return failure(
            gb_setup::InvalidInput("invalid person_id".into()).into(),
            &headers,
        );
    }
    let workspace = match project(&api, &headers) {
        Ok(w) => w,
        Err(e) => return *e,
    };
    let result = with_setup(&api, workspace.clone(), move |setup| {
        let mut value = IntelligenceConfig {
            repository: setup.repository,
            templates: setup.templates,
        }
        .read(scope.person_id.as_deref())?;
        let (shared, local) = grants(&workspace)?;
        value["filesystem_grants"] = shared;
        value["local_grants"] = local;
        value["agent_environment"] =
            declaration(&workspace.join(".guildbotics/config"), setup.templates)?;
        value["platform"] = json!(if cfg!(target_os = "macos") {
            "darwin"
        } else if cfg!(windows) {
            "win32"
        } else {
            "linux"
        });
        Ok(value)
    })
    .await;
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => failure(e, &headers),
    }
}
#[derive(Deserialize)]
pub(crate) struct Save {
    config_dir: PathBuf,
    #[serde(default)]
    expected_revisions: Revisions,
    #[serde(flatten)]
    update: Update,
}
pub(crate) async fn put(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(mut request): Body<Save>,
) -> Response {
    let workspace =
        match project(&api, &headers).and_then(|w| composed_in(w, &request.config_dir, &headers)) {
            Ok(w) => w,
            Err(e) => return *e,
        };
    let (home, data) = (api.home.clone(), api.data.clone());
    let result = with_setup(&api, workspace.clone(), move |setup| {
        let service = IntelligenceConfig {
            repository: setup.repository,
            templates: setup.templates,
        };
        let receipt = service.update_validated(
            &mut request.update,
            &request.expected_revisions,
            |update| validate_update(update, &workspace, &home, &data),
        )?;
        Ok(json!({"intelligence": {"files": receipt.result}, "revisions": receipt.revisions}))
    })
    .await;
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => failure(e, &headers),
    }
}
fn declaration(config: &Path, templates: &Path) -> anyhow::Result<Value> {
    let own = read(
        &config.join(gb_config::agent_environment::PATH),
        Value::Null,
    )?;
    let v = if !own.is_object() || own == json!({}) {
        read(
            &templates.join(gb_config::agent_environment::PATH),
            Value::Null,
        )?
    } else {
        own
    };
    normalize_declaration(&v)
}

fn resolve(
    workspace: &Path,
    home: &Path,
    data: &Path,
    shared: Value,
    local: Value,
) -> anyhow::Result<gb_environment::contract::Resolved> {
    let registered = gb_workspace::selection::registered(data)?;
    gb_environment::contract::resolve_values(
        workspace,
        home,
        &registered,
        serde_json::from_value(shared).map_err(|e| error("invalid_filesystem_grants", e))?,
        serde_json::from_value(local).map_err(|e| error("invalid_filesystem_grants", e))?,
        false,
        false,
        |p| std::fs::read_dir(p).map(|_| ()).map_err(|e| e.to_string()),
    )
    .map_err(|e| match e.sentence {
        gb_environment::Sentence::Key(key, params) => {
            let mut refused = SetupError::new("invalid_filesystem_grants", key);
            for (k, v) in params {
                refused = refused.with(k, v);
            }
            refused.into()
        }
        gb_environment::Sentence::Text(text) => error("invalid_filesystem_grants", text),
        other => error("invalid_filesystem_grants", crate::i18n::say(other, "en")),
    })
}
#[derive(Deserialize)]
pub(crate) struct Credential {
    value: String,
}
pub(crate) async fn options(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let workspace = match project(&api, &headers) {
        Ok(w) => w,
        Err(e) => return *e,
    };
    let result = with_setup(&api,workspace,|setup| Ok(json!({"credential_present":setup.repository.workspace_secrets(setup.device.clone())?.get(CREDENTIAL)?.is_some_and(|v|!v.is_empty())}))).await;
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => failure(e, &headers),
    }
}
pub(crate) async fn credential(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<Credential>,
) -> Response {
    let workspace = match project(&api, &headers) {
        Ok(w) => w,
        Err(e) => return *e,
    };
    let result = with_setup(&api, workspace, move |setup| {
        let value = request.value.trim();
        if value.is_empty() {
            return Ok(false);
        }
        Ok(setup
            .repository
            .workspace_secrets(setup.device.clone())
            .and_then(|s| s.set(CREDENTIAL, value))
            .is_ok())
    })
    .await;
    match result {
        Ok(true) => Json(json!({"state":"unverified"})).into_response(),
        Ok(false) => ApiError::new(StatusCode::BAD_REQUEST, "invalid_decision", &headers)
            .locale("intelligences.decisions.credentials_missing", vec![])
            .into_response(),
        Err(e) => failure(e, &headers),
    }
}
#[derive(Deserialize)]
pub(crate) struct Preview {
    scope: String,
    path: String,
    #[serde(default = "default_access")]
    access: String,
}
fn default_access() -> String {
    "read".into()
}
pub(crate) async fn preview(
    State(api): State<Shared>,
    headers: HeaderMap,
    Query(query): Query<Preview>,
) -> Response {
    if !["document", "local", "deny"].contains(&query.scope.as_str()) {
        return ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .into_response();
    }
    let workspace = api
        .selected()
        .workspace
        .clone()
        .unwrap_or_else(|| api.data.join("unconfigured"));
    let access = if query.scope == "deny" {
        ""
    } else {
        &query.access
    };
    let mut answer = json!({"scope":query.scope,"path":query.path,"access":access,"valid":false,"present":false,"reason":""});
    let result = (|| -> anyhow::Result<Value> {
        let (mut shared, mut local) = grants(&workspace)?;
        match query.scope.as_str() {
            "document" => shared["documents"]
                .as_array_mut()
                .unwrap()
                .push(json!({"path":query.path,"access":query.access})),
            "local" => local["paths"]
                .as_array_mut()
                .unwrap()
                .push(json!({"path":query.path,"access":query.access})),
            _ => local["deny"]
                .as_array_mut()
                .unwrap()
                .push(json!(query.path)),
        }
        let resolved = resolve(&workspace, &api.home, &api.data, shared, local)?;
        if query.scope == "deny" {
            return Ok(json!({"present":true,"grant":query.path}));
        }
        let rows = resolved.view[if query.scope == "document" {
            "documents"
        } else {
            "paths"
        }]
        .as_array()
        .unwrap();
        let row = rows.last().unwrap();
        if query.scope == "local" && row["present"] == false {
            return Err(SetupError::new(
                "invalid_filesystem_grants",
                "intelligences.agent_environment.grants.local_path_missing",
            )
            .with("path", query.path.clone())
            .into());
        }
        Ok(row.clone())
    })();
    match result {
        Ok(row) => {
            answer["valid"] = json!(true);
            answer["present"] = row["present"].clone();
            if query.scope == "local" {
                answer["path"] = row["grant"].clone();
            }
        }
        Err(e) => {
            let language = ApiError::new(
                StatusCode::BAD_REQUEST,
                "invalid_filesystem_grants",
                &headers,
            )
            .language;
            answer["reason"] = json!(match e.downcast_ref::<SetupError>().map(|e| &e.sentence) {
                Some(gb_setup::Sentence::Key(key, params)) => crate::i18n::t(
                    language,
                    key,
                    &params
                        .iter()
                        .map(|(k, v)| (k.as_str(), v.as_str()))
                        .collect::<Vec<_>>()
                ),
                Some(gb_setup::Sentence::Text(text)) => text.clone(),
                None => e.to_string(),
            });
        }
    }
    Json(answer).into_response()
}
pub(crate) async fn providers(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let templates = gb_base::home::python_package().join("templates");
    let mut roots = vec![templates.join("intelligences/models")];
    if let Some(workspace) = &api.selected().workspace {
        roots.insert(
            0,
            workspace.join(".guildbotics/config/intelligences/models"),
        );
    }
    let result = gb_setup::intelligences::providers(&roots);
    match result {
        Ok(providers) => Json(json!({"providers":providers})).into_response(),
        Err(e) => failure(e, &headers),
    }
}

fn normalize_declaration(v: &Value) -> anyhow::Result<Value> {
    let mut m = v
        .as_object()
        .cloned()
        .ok_or_else(|| error("invalid_agent_environment", "declaration must be a mapping"))?;
    let bad = |reason: &str| error("invalid_agent_environment", reason);
    if m.keys()
        .any(|k| !["image", "resources", "network", "dns"].contains(&k.as_str()))
    {
        return Err(bad("invalid declaration field"));
    }
    let network: gb_environment::contract::Network =
        serde_json::from_value(m.get("network").cloned().unwrap_or(json!({})))
            .map_err(|e| bad(&e.to_string()))?;
    network.validate().map_err(|e| bad(&e))?;
    m.insert("network".into(),json!({"mode":match network.mode {gb_environment::contract::Mode::Deny=>"deny",gb_environment::contract::Mode::Allowlist=>"allowlist",gb_environment::contract::Mode::Unrestricted=>"unrestricted"},"allowed_domains":network.allowed_domains,"allow_local_network":network.allow_local_network}));
    let resources = m.entry("resources").or_insert(json!({}));
    let resources = resources
        .as_object_mut()
        .ok_or_else(|| bad("resources must be a mapping"))?;
    if resources
        .keys()
        .any(|k| !["memory_mib", "cpus"].contains(&k.as_str()))
    {
        return Err(bad("invalid resources field"));
    }
    for (name, default) in [("memory_mib", 4096), ("cpus", 2)] {
        let value = resources.entry(name).or_insert(json!(default));
        if value.as_u64().is_none_or(|n| n == 0) {
            return Err(bad("resources must be positive integers"));
        }
    }
    let dns = m
        .get("dns")
        .and_then(Value::as_object)
        .ok_or_else(|| bad("dns is required"))?;
    if dns.keys().any(|k| k != "nameservers") {
        return Err(bad("invalid dns field"));
    }
    if dns.get("nameservers") != Some(&json!("host"))
        && !dns
            .get("nameservers")
            .and_then(Value::as_array)
            .is_some_and(|a| {
                !a.is_empty()
                    && a.iter().all(|v| {
                        v.as_str()
                            .is_some_and(|s| s.parse::<std::net::Ipv4Addr>().is_ok())
                    })
            })
    {
        return Err(bad("nameservers must be host or IPv4 addresses"));
    }
    let image = m.entry("image").or_insert(Value::Null);
    if !image.is_null() {
        let parsed: gb_config::agent_environment::BaseImage =
            serde_json::from_value(image.clone()).map_err(|e| bad(&e.to_string()))?;
        if parsed.reference.is_empty()
            || parsed.reference.starts_with('-')
            || parsed.reference.chars().any(char::is_whitespace)
            || parsed.digests.is_empty()
        {
            return Err(bad("invalid image reference or digests"));
        }
        for (arch, digest) in parsed.digests {
            if arch.is_empty()
                || !arch
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
                || !digest.strip_prefix("sha256:").is_some_and(|s| {
                    s.len() == 64
                        && s.bytes()
                            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                })
            {
                return Err(bad("invalid image architecture or digest"));
            }
        }
    }
    Ok(Value::Object(m))
}

fn spell(value: &Value, home: &Path) -> Value {
    let path = Path::new(value.as_str().unwrap_or_default());
    json!(
        path.strip_prefix(home)
            .ok()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or(path)
    )
}

fn validate_update(
    update: &mut Update,
    workspace: &Path,
    home: &Path,
    data: &Path,
) -> anyhow::Result<()> {
    if update.filesystem_grants.is_some() || update.local_grants.is_some() {
        let (shared, local) = grants(workspace)?;
        let shared = update.filesystem_grants.clone().unwrap_or(shared);
        let local = update.local_grants.clone().unwrap_or(local);
        let resolved = resolve(workspace, home, data, shared, local)?;
        if let Some(row) = resolved.view["paths"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| p["present"] == false)
        {
            return Err(SetupError::new(
                "invalid_filesystem_grants",
                "intelligences.agent_environment.grants.local_path_missing",
            )
            .with("path", row["grant"].as_str().unwrap_or_default())
            .into());
        }
        if update.local_grants.is_some() {
            let paths: Vec<_> = resolved.view["paths"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| json!({"path":spell(&p["path"],home), "access":p["access"]}))
                .collect();
            let deny: Vec<_> = resolved.view["denied"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|p| p["builtin"] == false)
                .map(|p| spell(&p["path"], home))
                .collect();
            update.local_grants = Some(json!({"paths":paths, "deny":deny}));
        }
    }
    if let Some(value) = &update.agent_environment {
        let mut normalized =
            normalize_declaration(value).map_err(|e| gb_setup::InvalidInput(e.to_string()))?;
        if normalized["image"].is_null() {
            normalized.as_object_mut().unwrap().remove("image");
        }
        update.agent_environment = Some(normalized);
    }
    Ok(())
}
