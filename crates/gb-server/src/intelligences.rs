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
fn language(headers: &HeaderMap) -> &'static str {
    ApiError::new(
        StatusCode::BAD_REQUEST,
        "invalid_filesystem_grants",
        headers,
    )
    .language
}
fn grant_error(error: impl std::fmt::Display, where_: &str, language: &str) -> anyhow::Error {
    self::error(
        "invalid_filesystem_grants",
        crate::i18n::t(
            language,
            "intelligences.agent_environment.grants.invalid",
            &[("where", where_), ("error", &error.to_string())],
        ),
    )
}
fn grants(
    workspace: &Path,
    shared: Option<Value>,
    local: Option<Value>,
    language: &str,
) -> anyhow::Result<(Value, Value)> {
    fn load<T: serde::de::DeserializeOwned + serde::Serialize>(
        value: Option<Value>,
        path: PathBuf,
        where_: &str,
        language: &str,
    ) -> anyhow::Result<T> {
        let value = match value {
            Some(v) => v,
            None => read(&path, json!({})).map_err(|e| grant_error(e, where_, language))?,
        };
        // Empty/non-mapping stored YAML has the frozen reader's empty default.
        serde_json::from_value(if value.is_object() { value } else { json!({}) })
            .map_err(|e| grant_error(e, where_, language))
    }
    let shared: gb_environment::contract::Shared = load(
        shared,
        workspace.join(".guildbotics/config").join(GRANTS),
        GRANTS,
        language,
    )?;
    let local_where = format!("local/{LOCAL}");
    let local: gb_environment::contract::Local = load(
        local,
        workspace.join(".guildbotics/local").join(LOCAL),
        &local_where,
        language,
    )?;
    for (s, l, where_) in [
        (&shared, &gb_environment::contract::Local::default(), GRANTS),
        (
            &gb_environment::contract::Shared::default(),
            &local,
            local_where.as_str(),
        ),
    ] {
        gb_environment::contract::validate_names(s, l, workspace)
            .map_err(|e| grant_error(crate::i18n::say(e.sentence, language), where_, language))?;
    }
    Ok((json!(shared), json!(local)))
}

// Wire values are typed and checked before revision comparison. The setup
// service carries their normalized JSON without depending on the environment.
struct ProposedShared(gb_environment::contract::Shared);
struct ProposedLocal(gb_environment::contract::Local);
struct ProposedDeclaration(Value);
fn grant_input<E: serde::de::Error>(v: &Value, rows: &str) -> Result<(), E> {
    if !v.is_object()
        || v.get(rows).is_some_and(|rows| {
            !rows
                .as_array()
                .is_some_and(|a| a.iter().all(Value::is_object))
        })
    {
        return Err(E::custom("grants and grant entries must be mappings"));
    }
    Ok(())
}
impl<'de> Deserialize<'de> for ProposedShared {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = Value::deserialize(d)?;
        grant_input::<D::Error>(&v, "documents")?;
        let value = serde_json::from_value(v).map_err(serde::de::Error::custom)?;
        gb_environment::contract::validate_names(&value, &Default::default(), Path::new("/"))
            .map_err(serde::de::Error::custom)?;
        Ok(Self(value))
    }
}
impl<'de> Deserialize<'de> for ProposedLocal {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = Value::deserialize(d)?;
        grant_input::<D::Error>(&v, "paths")?;
        let value = serde_json::from_value(v).map_err(serde::de::Error::custom)?;
        gb_environment::contract::validate_names(&Default::default(), &value, Path::new("/"))
            .map_err(serde::de::Error::custom)?;
        Ok(Self(value))
    }
}
impl<'de> Deserialize<'de> for ProposedDeclaration {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        normalize_declaration(&Value::deserialize(d)?, "en")
            .map(Self)
            .map_err(serde::de::Error::custom)
    }
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
    let lang = language(&headers);
    let result = with_setup(&api, workspace.clone(), move |setup| {
        let mut value = IntelligenceConfig {
            repository: setup.repository,
            templates: setup.templates,
        }
        .read(scope.person_id.as_deref())?;
        let (shared, local) = grants(&workspace, None, None, lang)?;
        value["filesystem_grants"] = shared;
        value["local_grants"] = local;
        value["agent_environment"] = declaration(
            &workspace.join(".guildbotics/config"),
            setup.templates,
            lang,
        )?;
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
    filesystem_grants: Option<ProposedShared>,
    local_grants: Option<ProposedLocal>,
    agent_environment: Option<ProposedDeclaration>,
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
    request.update.filesystem_grants = request.filesystem_grants.map(|v| json!(v.0));
    request.update.local_grants = request.local_grants.map(|v| json!(v.0));
    request.update.agent_environment = request.agent_environment.map(|v| v.0);
    let (home, data) = (api.home.clone(), api.data.clone());
    let lang = language(&headers);
    let result = with_setup(&api, workspace.clone(), move |setup| {
        let service = IntelligenceConfig {
            repository: setup.repository,
            templates: setup.templates,
        };
        let receipt = service.update_validated(
            &mut request.update,
            &request.expected_revisions,
            |update| {
                if update.person_id.is_some() && !update.inherit_team_defaults {
                    grants(&workspace, None, None, lang)?;
                    declaration(
                        &workspace.join(".guildbotics/config"),
                        setup.templates,
                        lang,
                    )?;
                }
                validate_update(update, &workspace, &home, &data, lang)
            },
        )?;
        Ok(json!({"intelligence": {"files": receipt.result}, "revisions": receipt.revisions}))
    })
    .await;
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => failure(e, &headers),
    }
}
fn declaration(config: &Path, templates: &Path, language: &str) -> anyhow::Result<Value> {
    let own = read(
        &config.join(gb_config::agent_environment::PATH),
        Value::Null,
    )
    .map_err(|e| {
        error(
            "invalid_agent_environment",
            format!(
                "{}: {}",
                gb_config::agent_environment::PATH,
                crate::i18n::t(
                    language,
                    "intelligences.agent_environment.declaration.invalid",
                    &[("error", &e.to_string())]
                )
            ),
        )
    })?;
    let v = if !own.is_object() || own == json!({}) {
        read(
            &templates.join(gb_config::agent_environment::PATH),
            Value::Null,
        )?
    } else {
        own
    };
    normalize_declaration(&v, language)
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
    .map_err(grant_problem)
}
fn grant_problem(e: gb_environment::contract::GrantProblem) -> anyhow::Error {
    match e.sentence {
        gb_environment::Sentence::Key(key, params) => {
            let mut refused = SetupError::new("invalid_filesystem_grants", key);
            for (k, v) in params {
                refused = refused.with(k, v);
            }
            refused.into()
        }
        other => SetupError::new(
            "invalid_filesystem_grants",
            "intelligences.agent_environment.grants.invalid",
        )
        .with("where", GRANTS)
        .with("error", crate::i18n::say(other, "en"))
        .into(),
    }
}
fn grant_reason(e: &anyhow::Error, language: &str) -> String {
    match e.downcast_ref::<SetupError>().map(|e| &e.sentence) {
        Some(gb_setup::Sentence::Key(key, params)) => crate::i18n::t(
            language,
            key,
            &params
                .iter()
                .map(|(k, v)| (k.as_str(), v.as_str()))
                .collect::<Vec<_>>(),
        ),
        Some(gb_setup::Sentence::Text(text)) => text.clone(),
        None => e.to_string(),
    }
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
    let lang = language(&headers);
    let access = if query.scope == "deny" {
        ""
    } else {
        &query.access
    };
    let mut answer = json!({"scope":query.scope,"path":query.path,"access":access,"valid":false,"present":false,"reason":""});
    // Syntax refusal precedes workspace selection and all filesystem reads.
    let proposed = (|| -> anyhow::Result<()> {
        let (shared, local) = match query.scope.as_str() {
            "document" => (
                json!({"documents":[{"path":query.path,"access":query.access}]}),
                json!({}),
            ),
            "local" => (
                json!({}),
                json!({"paths":[{"path":query.path,"access":query.access}]}),
            ),
            _ => (json!({}), json!({"deny":[query.path]})),
        };
        let parse = |e| grant_error(e, GRANTS, lang);
        gb_environment::contract::validate_names(
            &serde_json::from_value(shared).map_err(parse)?,
            &serde_json::from_value(local).map_err(parse)?,
            &api.home,
        )
        .map_err(grant_problem)
    })();
    if let Err(e) = proposed {
        answer["reason"] = json!(grant_reason(&e, lang));
        return Json(answer).into_response();
    }
    let Some(workspace) = api.selected().workspace.clone() else {
        return ApiError::new(StatusCode::CONFLICT, "workspace_not_configured", &headers)
            .into_response();
    };
    let result = (|| -> anyhow::Result<Value> {
        let (mut shared, mut local) = grants(&workspace, None, None, lang)?;
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
            answer["reason"] = json!(grant_reason(&e, lang));
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

fn normalize_declaration(v: &Value, language: &str) -> anyhow::Result<Value> {
    let mut m = v.as_object().cloned().ok_or_else(|| {
        error(
            "invalid_agent_environment",
            crate::i18n::t(
                language,
                "intelligences.agent_environment.declaration.not_a_mapping",
                &[("where", gb_config::agent_environment::PATH)],
            ),
        )
    })?;
    let bad = |key: &str, params: &[(&str, &str)]| {
        error(
            "invalid_agent_environment",
            format!(
                "{}: {}",
                gb_config::agent_environment::PATH,
                crate::i18n::t(
                    language,
                    &format!("intelligences.agent_environment.{key}"),
                    params
                )
            ),
        )
    };
    if m.keys()
        .any(|k| !["image", "resources", "network", "dns"].contains(&k.as_str()))
    {
        return Err(bad("declaration.invalid_field", &[]));
    }
    if m.get("network").is_some_and(|v| !v.is_object()) {
        return Err(bad("declaration.invalid_field", &[]));
    }
    let network: gb_environment::contract::Network =
        serde_json::from_value(m.get("network").cloned().unwrap_or(json!({})))
            .map_err(|e| bad("declaration.invalid", &[("error", &e.to_string())]))?;
    for domain in &network.allowed_domains {
        if domain.trim().is_empty()
            || domain.contains('/')
            || domain.chars().any(char::is_whitespace)
        {
            return Err(bad("grants.not_a_domain", &[("domain", domain)]));
        }
    }
    if network.mode == gb_environment::contract::Mode::Allowlist
        && network.allowed_domains.is_empty()
    {
        return Err(bad("grants.allowlist_needs_domain", &[]));
    }
    if network.mode != gb_environment::contract::Mode::Allowlist
        && !network.allowed_domains.is_empty()
    {
        return Err(bad("grants.domains_need_allowlist", &[]));
    }
    m.insert("network".into(),json!({"mode":match network.mode {gb_environment::contract::Mode::Deny=>"deny",gb_environment::contract::Mode::Allowlist=>"allowlist",gb_environment::contract::Mode::Unrestricted=>"unrestricted"},"allowed_domains":network.allowed_domains,"allow_local_network":network.allow_local_network}));
    let resources = m.entry("resources").or_insert(json!({}));
    let resources = resources
        .as_object_mut()
        .ok_or_else(|| bad("declaration.invalid_resources", &[]))?;
    if resources
        .keys()
        .any(|k| !["memory_mib", "cpus"].contains(&k.as_str()))
    {
        return Err(bad("declaration.invalid_resources", &[]));
    }
    for (name, default) in [("memory_mib", 4096), ("cpus", 2)] {
        let value = resources.entry(name).or_insert(json!(default));
        if value.as_u64().is_none_or(|n| n == 0) {
            return Err(bad("declaration.invalid_resources", &[]));
        }
    }
    let dns = m
        .get("dns")
        .and_then(Value::as_object)
        .ok_or_else(|| bad("declaration.invalid_dns", &[]))?;
    if dns.keys().any(|k| k != "nameservers") {
        return Err(bad("declaration.invalid_dns", &[]));
    }
    if dns.get("nameservers") != Some(&json!("host")) {
        let nameservers = dns
            .get("nameservers")
            .and_then(Value::as_array)
            .ok_or_else(|| bad("declaration.invalid_dns", &[]))?;
        if nameservers.is_empty() {
            return Err(bad("declaration.no_nameservers", &[("host", "host")]));
        }
        for value in nameservers {
            let name = value
                .as_str()
                .ok_or_else(|| bad("declaration.invalid_dns", &[]))?;
            if name.parse::<std::net::Ipv4Addr>().is_err() {
                return Err(bad("declaration.not_ipv4", &[("nameserver", name)]));
            }
        }
    }
    let image = m.entry("image").or_insert(Value::Null);
    if !image.is_null() {
        if !image.is_object() {
            return Err(bad("declaration.invalid_field", &[]));
        }
        let parsed: gb_config::agent_environment::BaseImage = serde_json::from_value(image.clone())
            .map_err(|e| bad("declaration.invalid", &[("error", &e.to_string())]))?;
        if parsed.reference.is_empty()
            || parsed.reference.starts_with('-')
            || parsed.reference.chars().any(char::is_whitespace)
        {
            return Err(bad(
                "declaration.not_an_image",
                &[("reference", &parsed.reference)],
            ));
        }
        if parsed.digests.is_empty() {
            return Err(bad("declaration.no_digests", &[]));
        }
        for (arch, digest) in parsed.digests {
            if arch.is_empty()
                || !arch
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
            {
                return Err(bad(
                    "declaration.not_an_architecture",
                    &[("architecture", &arch)],
                ));
            }
            if !digest.strip_prefix("sha256:").is_some_and(|s| {
                s.len() == 64
                    && s.bytes()
                        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
            }) {
                return Err(bad("declaration.not_a_digest", &[("digest", &digest)]));
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
    language: &str,
) -> anyhow::Result<()> {
    if update.filesystem_grants.is_some() || update.local_grants.is_some() {
        let (shared, local) = grants(
            workspace,
            update.filesystem_grants.clone(),
            update.local_grants.clone(),
            language,
        )?;
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
        let mut normalized = normalize_declaration(value, language)
            .map_err(|e| gb_setup::InvalidInput(e.to_string()))?;
        if normalized["image"].is_null() {
            normalized.as_object_mut().unwrap().remove("image");
        }
        update.agent_environment = Some(normalized);
    }
    Ok(())
}

#[cfg(test)]
mod validation_tests {
    use super::*;

    #[test]
    fn declaration_semantic_errors_use_locale_keys_and_include_the_file() {
        for (patch, key, params) in [
            (
                json!({"dns":{"nameservers":[]}}),
                "declaration.no_nameservers",
                vec![("host", "host")],
            ),
            (
                json!({"dns":{"nameservers":["8.8.8.8x"]}}),
                "declaration.not_ipv4",
                vec![("nameserver", "8.8.8.8x")],
            ),
            (
                json!({"image":{"reference":"-bad","digests":{}}}),
                "declaration.not_an_image",
                vec![("reference", "-bad")],
            ),
            (
                json!({"image":{"reference":"image","digests":{}}}),
                "declaration.no_digests",
                vec![],
            ),
            (
                json!({"image":{"reference":"image","digests":{"BAD":"invalid"}}}),
                "declaration.not_an_architecture",
                vec![("architecture", "BAD")],
            ),
            (
                json!({"image":{"reference":"image","digests":{"amd64":"invalid"}}}),
                "declaration.not_a_digest",
                vec![("digest", "invalid")],
            ),
            (
                json!({"network":{"mode":"allowlist"}}),
                "grants.allowlist_needs_domain",
                vec![],
            ),
            (
                json!({"network":{"allowed_domains":["example.com"]}}),
                "grants.domains_need_allowlist",
                vec![],
            ),
            (
                json!({"network":{"mode":"allowlist","allowed_domains":["bad/domain"]}}),
                "grants.not_a_domain",
                vec![("domain", "bad/domain")],
            ),
        ] {
            for language in ["en", "ja"] {
                let mut value = json!({"dns":{"nameservers":"host"}});
                value
                    .as_object_mut()
                    .unwrap()
                    .extend(patch.as_object().unwrap().clone());
                let error = normalize_declaration(&value, language).unwrap_err();
                let key = format!("intelligences.agent_environment.{key}");
                let expected = crate::i18n::t(language, &key, &params);
                assert_ne!(expected, key);
                assert_eq!(
                    grant_reason(&error, language),
                    format!("{}: {expected}", gb_config::agent_environment::PATH)
                );
            }
        }
    }
}
