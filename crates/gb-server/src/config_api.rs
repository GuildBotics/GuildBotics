//! The setup screens' API for the workspace, the project and its members,
//! as the Python version's (`guildbotics/app_api/api.py`, `runtime.py`).
//! Every change of the shared config goes through the [`ConfigRepository`]:
//! a screen that read older revisions is refused, and a lock held elsewhere
//! answers that nothing was saved.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::Json;
use axum::extract::{Multipart, Path as RoutePath, Query, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use gb_base::shared_writes::{SharedWriteBusyError, SharedWrites};
use gb_config::repository::{
    ConfigRepository, InvalidRevisionPath, Revisions, StaleConfigWriteError,
};
use gb_config::team::valid_person_id;
use gb_setup::{
    InvalidInput, PersonSetupInput, ProjectSetupInput, SecretsUnavailable, Sentence, Setup,
    SetupError,
};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::api::{ApiError, Body, Selected, Shared};
use crate::events::Origin;

/// What the setup of the workspace `workspace` writes through.
struct Context {
    repository: ConfigRepository,
    providers: crate::setup_providers::Registry,
    templates: PathBuf,
    machine_root: PathBuf,
    keychain: Arc<dyn gb_config::secrets::Keychain>,
}
impl Context {
    fn setup(&self) -> Setup<'_> {
        Setup {
            repository: &self.repository,
            providers: &self.providers,
            templates: &self.templates,
            device: gb_config::secrets::DeviceKey {
                machine_root: self.machine_root.clone(),
                keychain: &*self.keychain,
            },
        }
    }
}

/// Runs `apply` on the setup of `workspace` on a blocking thread, where a
/// lock may be waited for and a provider's part awaited.
pub(crate) async fn with_setup<T: Send + 'static>(
    api: &Shared,
    workspace: PathBuf,
    apply: impl FnOnce(&Setup<'_>) -> anyhow::Result<T> + Send + 'static,
) -> anyhow::Result<T> {
    let mut writes = SharedWrites::new(api.identity_root.clone(), Some(workspace));
    writes.timeout = api.write_timeout;
    let context = Context {
        repository: ConfigRepository::new(writes)?,
        providers: crate::setup_providers::Registry(Some(api.clone())),
        templates: gb_base::home::python_package().join("templates"),
        machine_root: api.data.parent().unwrap_or(&api.data).to_path_buf(),
        keychain: api.keychain.clone(),
    };
    tokio::task::spawn_blocking(move || apply(&context.setup())).await?
}

/// Awaits a provider's part of a setup from the blocking thread it runs on.
fn wait<T>(future: impl Future<Output = T>) -> T {
    tokio::runtime::Handle::current().block_on(future)
}

/// How a refused change is answered: a stale screen, a revision outside
/// config, a lock held elsewhere, what the setup refuses, a form the request
/// got wrong, a secret store that cannot be used, or a defect.
pub(crate) fn failure(error: anyhow::Error, headers: &HeaderMap) -> Response {
    let answer = |status, code: &str| ApiError::new(status, code, headers);
    if let Some(stale) = error.downcast_ref::<StaleConfigWriteError>() {
        return answer(StatusCode::CONFLICT, "config_changed")
            .context(json!({"path": stale.relative_path, "revisions": stale.revisions}))
            .into_response();
    }
    if let Some(invalid) = error.downcast_ref::<InvalidRevisionPath>() {
        return answer(StatusCode::BAD_REQUEST, "config_revision_invalid")
            .reason(invalid.0.clone())
            .into_response();
    }
    if error.is::<SharedWriteBusyError>() {
        return answer(StatusCode::SERVICE_UNAVAILABLE, "config_busy").into_response();
    }
    if let Some(refused) = error.downcast_ref::<SetupError>() {
        let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::BAD_REQUEST);
        let answer = answer(status, refused.code.as_str());
        return match &refused.sentence {
            Sentence::Key(key, params) => answer.locale(key, params.clone()),
            Sentence::Text(text) => answer.reason(text.clone()),
        }
        .into_response();
    }
    if let Some(refused) = error.downcast_ref::<gb_config::command_writes::Refused>() {
        let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::BAD_REQUEST);
        let answer = answer(status, refused.code).context(refused.context.clone());
        return match &refused.reason {
            Some(reason) => answer.reason(reason.clone()),
            None => answer.key(refused.key, refused.params.clone()),
        }
        .into_response();
    }
    if let Some(invalid) = error.downcast_ref::<InvalidInput>() {
        return answer(StatusCode::UNPROCESSABLE_ENTITY, "validation_error")
            .context(json!({"errors": [{"msg": invalid.0}]}))
            .into_response();
    }
    if let Some(unavailable) = error.downcast_ref::<SecretsUnavailable>() {
        return answer(StatusCode::SERVICE_UNAVAILABLE, "secret_store_unavailable")
            .key(
                "secret_store_unavailable",
                vec![("reason", unavailable.0.clone())],
            )
            .into_response();
    }
    tracing::error!("cannot change the configuration: {error:#}");
    answer(StatusCode::INTERNAL_SERVER_ERROR, "internal_error")
        .context(json!({"error_type": "RuntimeError"}))
        .into_response()
}

fn written(
    result: anyhow::Result<gb_config::repository::ConfigWriteReceipt<()>>,
    headers: &HeaderMap,
) -> Response {
    match result {
        Ok(receipt) => Json(json!({"revisions": receipt.revisions})).into_response(),
        Err(error) => failure(error, headers),
    }
}

/// The selected workspace, which a member's configuration needs.
fn workspace(api: &Shared, headers: &HeaderMap) -> Result<PathBuf, Box<Response>> {
    api.selected().workspace.clone().ok_or_else(|| {
        Box::new(
            ApiError::new(StatusCode::BAD_REQUEST, "project_not_found", headers)
                .context(json!({"project": "None"}))
                .into_response(),
        )
    })
}

/// The selected workspace, which the project's configuration needs to
/// have been created in.
pub(crate) fn project(api: &Shared, headers: &HeaderMap) -> Result<PathBuf, Box<Response>> {
    let workspace = workspace(api, headers)?;
    let file = workspace.join(".guildbotics/config/team/project.yml");
    if file.is_file() {
        return Ok(workspace);
    }
    Err(Box::new(
        ApiError::new(StatusCode::BAD_REQUEST, "project_not_found", headers)
            .context(json!({"project": file}))
            .into_response(),
    ))
}

/// `workspace`, the selected one, when `config_dir` names its config. A save
/// composed against a read names the config it read, since revisions tell
/// the files of two workspaces apart only by their bytes: a form read in
/// another workspace is stale here, however alike their files are.
pub(crate) fn composed_in(
    workspace: PathBuf,
    config_dir: &Path,
    headers: &HeaderMap,
) -> Result<PathBuf, Box<Response>> {
    let normalized =
        |path: &Path| gb_base::host_path::normalize(path).unwrap_or_else(|_| path.to_path_buf());
    let config = normalized(&workspace.join(".guildbotics/config"));
    let named = normalized(config_dir);
    if named == config || same_directory(&named, &config) {
        return Ok(workspace);
    }
    let stale = StaleConfigWriteError {
        relative_path: "config".into(),
        revisions: Revisions::new(),
    };
    Err(Box::new(failure(stale.into(), headers)))
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

fn refused_location(
    refused: gb_environment::Sentence,
    headers: &HeaderMap,
    code: &str,
) -> Response {
    let answer = ApiError::new(StatusCode::BAD_REQUEST, code, headers);
    match refused {
        gb_environment::Sentence::Key(key, params) => answer.locale(
            key,
            params
                .into_iter()
                .map(|(name, value)| (name.into(), value))
                .collect(),
        ),
        other => {
            let language = answer.language;
            answer.reason(crate::i18n::say(other, language))
        }
    }
    .into_response()
}

#[derive(Deserialize)]
pub(crate) struct InitRequest {
    config_dir: PathBuf,
    #[serde(flatten)]
    form: ProjectSetupInput,
}

/// Creates a workspace's project: the workspace `config_dir` names is
/// registered on this machine, not selected.
pub(crate) async fn init(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<InitRequest>,
) -> Response {
    let shaped = request.config_dir.file_name() == Some("config".as_ref())
        && request.config_dir.parent().and_then(Path::file_name) == Some(".guildbotics".as_ref());
    let Some(workspace) = shaped
        .then(|| request.config_dir.parent().and_then(Path::parent))
        .flatten()
    else {
        return invalid(
            &headers,
            "config_dir must be <workspace>/.guildbotics/config",
        );
    };
    let workspace = match gb_environment::contract::new_workspace_location(workspace, &api.home) {
        Ok(workspace) => workspace,
        Err(refused) => return refused_location(refused, &headers, "invalid_workspace_location"),
    };
    let (form, data) = (request.form, api.data.clone());
    written(
        with_setup(&api, workspace.clone(), move |setup| {
            // The form is checked before the workspace is registered.
            setup.check_project(&form)?;
            gb_workspace::selection::register(&data, &workspace).map_err(|error| {
                SetupError::text("invalid_workspace_location", format!("{error:#}"))
            })?;
            setup.init_project(&form)
        })
        .await,
        &headers,
    )
}

#[derive(Deserialize)]
pub(crate) struct WorkspaceRequest {
    workspace_dir: PathBuf,
}

/// What runs for the selected workspace, as a refused switch reports it:
/// its service, and the work it admitted (`admitted` when an admission
/// such as a member run holds the selection).
async fn active(selected: &Selected, admitted: bool) -> Option<Value> {
    let view = selected.service.view().await;
    let count = view["active_works"].as_array().map_or(0, Vec::len);
    (admitted || view["has_active_work"] == true).then(|| {
        json!({
            "active_work_count": if admitted { count.max(1) } else { count },
            "scheduler_state": view["scheduler"]["state"],
            "events_state": view["events"]["state"],
        })
    })
}

fn blocked(headers: &HeaderMap, context: Value) -> Response {
    ApiError::new(
        StatusCode::CONFLICT,
        "workspace_switch_blocked_by_active_work",
        headers,
    )
    .context(context)
    .into_response()
}

/// Selects another workspace, once nothing runs for the selected one. The
/// switch is the server's work, not the request's: it runs to its end holding
/// the switch even when the client goes away, so the selection it persisted
/// is the one that is open, and a server that stops waits for it.
pub(crate) async fn select_workspace(
    State(api): State<Shared>,
    axum::Extension(host): axum::Extension<crate::api::EmbeddedHost>,
    headers: HeaderMap,
    Body(request): Body<WorkspaceRequest>,
) -> Response {
    let Ok(switching) = api.switching.clone().try_write_owned() else {
        let selected = api.selected();
        return blocked(&headers, active(&selected, true).await.unwrap_or_default());
    };
    let work = api.work.clone();
    let switch = work.spawn(switch(
        api,
        host,
        headers.clone(),
        request.workspace_dir,
        switching,
    ));
    switch
        .await
        .unwrap_or_else(|error| failure(error.into(), &headers))
}

/// The switch to `requested`, holding `_switching` throughout: the selected
/// workspace's idle service is stopped, its system transcript finished, and
/// the new one is persisted, opened and announced.
async fn switch(
    api: Shared,
    host: crate::api::EmbeddedHost,
    headers: HeaderMap,
    requested: PathBuf,
    _switching: tokio::sync::OwnedRwLockWriteGuard<()>,
) -> Response {
    let selected = api.selected();
    if let Some(context) = active(&selected, false).await {
        return blocked(&headers, context);
    }
    let resolved = resolve(&requested);
    let not = |code: &str| {
        ApiError::new(StatusCode::BAD_REQUEST, code, &headers)
            .context(json!({"workspace_dir": resolved}))
            .into_response()
    };
    if !resolved.exists() && resolved.symlink_metadata().is_err() {
        return not("workspace_not_found");
    }
    if !resolved.is_dir() {
        return not("workspace_not_directory");
    }
    let workspace = match gb_environment::contract::workspace_location(&resolved, &api.home) {
        Ok(workspace) => workspace,
        Err(refused) => return refused_location(refused, &headers, "unsafe_host_path"),
    };
    selected.service.stop(true).await;
    if let Some(context) = active(&selected, false).await {
        return blocked(&headers, context);
    }
    let instance = api.instance_id.clone();
    let (data, previous, next) = (
        api.data.clone(),
        selected.workspace.clone(),
        workspace.clone(),
    );
    let switched = tokio::task::spawn_blocking(move || -> anyhow::Result<()> {
        if let Some(previous) = previous {
            gb_observability::Diagnostics::open(&previous).finish_system_session()?;
        }
        gb_workspace::selection::write_active(&data, &next)?;
        Ok(())
    })
    .await;
    if let Err(error) = switched.map_err(anyhow::Error::from).and_then(|r| r) {
        return failure(error, &headers);
    }
    api.select(Selected::open(
        Some(workspace.clone()),
        &api,
        api.branvil.get().cloned(),
    ));
    let diagnostics = gb_observability::Diagnostics::open(&workspace);
    match tokio::task::spawn_blocking(move || diagnostics.start_system_session(&instance)).await {
        Ok(Ok(())) => (),
        result => tracing::warn!("cannot start system transcript: {result:?}"),
    }
    let mut payload = serde_json::Map::new();
    payload.insert("workspace".into(), json!(workspace));
    api.events
        .publish("workspace.changed", payload, Origin::default());
    Json(crate::screens::status(&api, host).await).into_response()
}

/// The absolute name `path` means, its `..` taken back as the Python
/// version's `Path.resolve()` takes them.
fn resolve(path: &Path) -> PathBuf {
    let absolute = gb_base::host_path::normalize(path).unwrap_or_else(|_| path.to_path_buf());
    let mut resolved = PathBuf::new();
    for part in absolute.components() {
        match part {
            std::path::Component::ParentDir => {
                resolved.pop();
            }
            std::path::Component::CurDir => {}
            other => resolved.push(other),
        }
    }
    resolved
}

fn registry_unreadable(error: anyhow::Error, api: &Shared, headers: &HeaderMap) -> Response {
    ApiError::new(StatusCode::BAD_REQUEST, "unsafe_host_path", headers)
        .locale(
            "safe_paths.registry",
            vec![
                (
                    "path".into(),
                    api.data.join("workspaces.json").display().to_string(),
                ),
                ("reason".into(), format!("{error:#}")),
            ],
        )
        .into_response()
}

/// The workspaces this machine has registered.
pub(crate) async fn workspaces(State(api): State<Shared>, headers: HeaderMap) -> Response {
    match gb_workspace::selection::registered(&api.data) {
        Ok(found) => Json(found).into_response(),
        Err(error) => registry_unreadable(error, &api, &headers),
    }
}

/// Whether two names reach the same directory, as far as both can be
/// reached: a registration that no longer opens is never the selected one.
fn same_directory(a: &Path, b: &Path) -> bool {
    match (std::fs::metadata(a), std::fs::metadata(b)) {
        #[cfg(unix)]
        (Ok(a), Ok(b)) => {
            use std::os::unix::fs::MetadataExt;
            (a.dev(), a.ino()) == (b.dev(), b.ino())
        }
        #[cfg(not(unix))]
        (Ok(_), Ok(_)) => std::fs::canonicalize(a).ok() == std::fs::canonicalize(b).ok(),
        _ => false,
    }
}

/// Forgets a registered workspace other than the selected one, without
/// touching its directory. A switch in progress is waited for, so the
/// workspace it selects is never the one forgotten.
pub(crate) async fn forget_workspace(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<WorkspaceRequest>,
) -> Response {
    let named = gb_base::host_path::normalize(&request.workspace_dir)
        .unwrap_or_else(|_| request.workspace_dir.clone());
    let _switching = api.switching.read().await;
    if let Some(selected) = api.selected().workspace.as_deref()
        && (named == selected || same_directory(&named, selected))
    {
        return ApiError::new(StatusCode::BAD_REQUEST, "unsafe_host_path", &headers)
            .locale("safe_paths.active_workspace", vec![])
            .into_response();
    }
    match gb_workspace::selection::unregister(&api.data, &named) {
        Ok(left) => Json(left).into_response(),
        Err(error) => registry_unreadable(error, &api, &headers),
    }
}

/// The project as its screen edits it.
pub(crate) async fn read_project(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let workspace = match project(&api, &headers) {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    let config = workspace.join(".guildbotics/config");
    match with_setup(&api, workspace, |setup| setup.read_project()).await {
        Ok(snapshot) => {
            let mut answer = serde_json::to_value(snapshot).expect("a project snapshot");
            answer["config_dir"] = json!(config);
            Json(answer).into_response()
        }
        Err(error) => failure(error, &headers),
    }
}

#[derive(Deserialize)]
pub(crate) struct ProjectRequest {
    config_dir: PathBuf,
    #[serde(flatten)]
    form: ProjectSetupInput,
    #[serde(default)]
    expected_revisions: Revisions,
}

pub(crate) async fn save_project(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<ProjectRequest>,
) -> Response {
    let workspace = match project(&api, &headers)
        .and_then(|workspace| composed_in(workspace, &request.config_dir, &headers))
    {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    let (form, expected) = (request.form, request.expected_revisions);
    written(
        with_setup(&api, workspace, move |setup| {
            setup.update_project(&form, &expected)
        })
        .await,
        &headers,
    )
}

#[derive(Deserialize)]
pub(crate) struct DefaultPersonRequest {
    #[serde(default)]
    person_id: String,
}

pub(crate) async fn default_person(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<DefaultPersonRequest>,
) -> Response {
    if !request.person_id.is_empty() && !valid_person_id(&request.person_id) {
        return invalid(&headers, "person_id is not a member ID");
    }
    let workspace = match project(&api, &headers) {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    let id = request.person_id;
    written(
        with_setup(&api, workspace, move |setup| setup.set_default_person(&id)).await,
        &headers,
    )
}

#[derive(Deserialize)]
pub(crate) struct RolesQuery {
    #[serde(default = "japanese")]
    language: String,
}
fn japanese() -> String {
    "ja".into()
}

/// The roles a member can take, as the template of `language` lists them.
pub(crate) async fn roles(
    headers: HeaderMap,
    query: Result<Query<RolesQuery>, axum::extract::rejection::QueryRejection>,
) -> Response {
    let Ok(Query(RolesQuery { language })) = query else {
        return invalid(&headers, "language must match ^(en|ja)$");
    };
    if !["en", "ja"].contains(&language.as_str()) {
        return invalid(&headers, "language must match ^(en|ja)$");
    }
    let path =
        gb_base::home::python_package().join(format!("templates/roles/default.{language}.yml"));
    let parsed = std::fs::read_to_string(&path)
        .map_err(anyhow::Error::from)
        .and_then(|text| gb_config::yaml_value(&text));
    let roles = match parsed {
        Ok(value) => value["roles"]
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(role_id, role)| {
                let role = role.as_object()?;
                let text = |key: &str| match role.get(key) {
                    Some(Value::String(s)) => s.clone(),
                    Some(Value::Null) | None => String::new(),
                    Some(other) => other.to_string(),
                };
                Some(json!({"role_id": role_id, "summary": text("summary"), "description": text("description")}))
            })
            .collect::<Vec<_>>(),
        Err(error) => return failure(error, &headers),
    };
    Json(json!({"roles": roles})).into_response()
}

#[derive(Deserialize)]
pub(crate) struct MemberRequest {
    config_dir: PathBuf,
    #[serde(flatten)]
    form: PersonSetupInput,
    #[serde(default)]
    expected_revisions: Revisions,
}

/// Adds a member to the selected workspace: neither of its files may exist
/// unless the form read them.
pub(crate) async fn create_member(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<MemberRequest>,
) -> Response {
    let workspace = match workspace(&api, &headers)
        .and_then(|workspace| composed_in(workspace, &request.config_dir, &headers))
    {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    let id = request.form.person_id.clone();
    let mut expected = Revisions::new();
    if valid_person_id(&id) {
        for file in ["identity", "member"] {
            expected.insert(gb_setup::member_path(&id, file), String::new());
        }
    }
    expected.extend(request.expected_revisions);
    let form = request.form;
    written(
        with_setup(&api, workspace, move |setup| {
            wait(setup.save_member(&id, form, &expected))
        })
        .await,
        &headers,
    )
}

pub(crate) async fn read_member(
    State(api): State<Shared>,
    headers: HeaderMap,
    RoutePath(name): RoutePath<String>,
) -> Response {
    let workspace = match workspace(&api, &headers) {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    let config = workspace.join(".guildbotics/config");
    match with_setup(&api, workspace, move |setup| setup.read_member(&name)).await {
        Ok(snapshot) => {
            // Where the form is composed, which its save names, as the
            // project's read says it.
            let mut answer = serde_json::to_value(snapshot).expect("a member snapshot");
            answer["config_dir"] = json!(config);
            Json(answer).into_response()
        }
        Err(error) => failure(error, &headers),
    }
}

pub(crate) async fn save_member(
    State(api): State<Shared>,
    headers: HeaderMap,
    RoutePath(id): RoutePath<String>,
    Body(request): Body<MemberRequest>,
) -> Response {
    if !valid_person_id(&id) {
        return invalid(&headers, "person_id is not a member ID");
    }
    let workspace = match workspace(&api, &headers)
        .and_then(|workspace| composed_in(workspace, &request.config_dir, &headers))
    {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    let (form, expected) = (request.form, request.expected_revisions);
    written(
        with_setup(&api, workspace, move |setup| {
            wait(setup.save_member(&id, form, &expected))
        })
        .await,
        &headers,
    )
}

/// Takes a member out of the selected workspace; its identity stays.
pub(crate) async fn remove_member(
    State(api): State<Shared>,
    headers: HeaderMap,
    RoutePath(name): RoutePath<String>,
) -> Response {
    let workspace = match workspace(&api, &headers) {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    written(
        with_setup(&api, workspace, move |setup| setup.remove_member(&name)).await,
        &headers,
    )
}

/// The members `workspace` stores, including those that cannot be loaded.
pub(crate) async fn members(
    api: &Shared,
    workspace: PathBuf,
) -> anyhow::Result<Vec<gb_setup::PersonSummary>> {
    with_setup(api, workspace, |setup| setup.list_members()).await
}

/// Every identity on this machine, and whether the selected workspace has it.
pub(crate) async fn identities(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let workspace = match workspace(&api, &headers) {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    match with_setup(&api, workspace, |setup| setup.list_identities()).await {
        Ok(found) => Json(found).into_response(),
        Err(error) => failure(error, &headers),
    }
}

/// Deletes a member everywhere: its accounts, its place in the selected
/// workspace, its secrets and its identity.
pub(crate) async fn delete_identity(
    State(api): State<Shared>,
    headers: HeaderMap,
    RoutePath(id): RoutePath<String>,
) -> Response {
    if !valid_person_id(&id) {
        return invalid(&headers, "person_id is not a member ID");
    }
    let workspace = match workspace(&api, &headers) {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    written(
        with_setup(&api, workspace, move |setup| {
            wait(setup.delete_identity(&id))
        })
        .await,
        &headers,
    )
}

/// The member's picture, kept with its identity.
pub(crate) async fn avatar(
    State(api): State<Shared>,
    headers: HeaderMap,
    RoutePath(id): RoutePath<String>,
) -> Response {
    if !valid_person_id(&id) {
        return invalid(&headers, "person_id is not a member ID");
    }
    let found = gb_setup::avatar_in(&api.identity_root, &id)
        .and_then(|path| Some((std::fs::read(&path).ok()?, path)));
    let Some((bytes, path)) = found else {
        return ApiError::new(StatusCode::NOT_FOUND, "avatar_not_found", &headers).into_response();
    };
    let kind = mime_guess::from_path(&path).first_or_octet_stream();
    let mut response = bytes.into_response();
    if let Ok(value) = HeaderValue::from_str(kind.as_ref()) {
        response.headers_mut().insert(header::CONTENT_TYPE, value);
    }
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    response
}

/// Replaces the member's picture with the upload's `file`.
pub(crate) async fn upload_avatar(
    State(api): State<Shared>,
    headers: HeaderMap,
    RoutePath(id): RoutePath<String>,
    mut multipart: Multipart,
) -> Response {
    if !valid_person_id(&id) {
        return invalid(&headers, "person_id is not a member ID");
    }
    let mut upload = None;
    while let Ok(Some(field)) = multipart.next_field().await {
        if field.name() == Some("file") {
            let name = field.file_name().unwrap_or("").to_owned();
            match field.bytes().await {
                Ok(bytes) => upload = Some((name, bytes)),
                Err(_) => return invalid(&headers, "the file could not be read"),
            }
            break;
        }
    }
    let Some((name, bytes)) = upload else {
        return invalid(&headers, "file is required");
    };
    let workspace = match workspace(&api, &headers) {
        Ok(workspace) => workspace,
        Err(refused) => return *refused,
    };
    let changed = with_setup(&api, workspace, move |setup| {
        wait(setup.change_avatar(&id, &bytes, &name))
    })
    .await;
    match changed {
        Ok(timestamp) => Json(json!({"avatar_timestamp": timestamp})).into_response(),
        Err(error)
            if error.is::<SetupError>()
                || error.is::<SharedWriteBusyError>()
                || error.is::<InvalidInput>() =>
        {
            failure(error, &headers)
        }
        Err(error) => {
            tracing::error!("cannot save the avatar: {error:#}");
            ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "avatar_save_failed",
                &headers,
            )
            .into_response()
        }
    }
}

const TRANSCRIPTS_FILE: &str = "transcripts.yml";
const DEFAULT_RETENTION_DAYS: i64 = 30;
/// The size past which the diagnostics index is rewritten.
const INDEX_REWRITE_THRESHOLD: u64 = 8 * 1024 * 1024;
/// The size each member's memory journal may grow to.
const MEMORY_JOURNAL_LIMIT: u64 = 8 * 1024 * 1024;

fn sizes(dir: &Path, suffix: &str) -> (u64, u64) {
    let mut total = (0, 0);
    for entry in std::fs::read_dir(dir).into_iter().flatten().flatten() {
        if entry.file_name().to_string_lossy().ends_with(suffix)
            && let Ok(metadata) = entry.metadata()
            && metadata.is_file()
        {
            total = (total.0 + metadata.len(), total.1 + 1);
        }
    }
    total
}

/// How much of what is run is recorded and for how long, and how much the
/// records take.
fn transcripts(workspace: &Path) -> Value {
    let stored =
        std::fs::read_to_string(workspace.join(".guildbotics/config").join(TRANSCRIPTS_FILE))
            .ok()
            .and_then(|text| gb_config::yaml_value(&text).ok())
            .unwrap_or(json!({}));
    let detail = match stored["detail"].as_str() {
        Some(detail) if detail.trim().eq_ignore_ascii_case("full") => "full",
        _ => "standard",
    };
    let retention = transcript_retention_days(&stored);
    let run = workspace.join(".guildbotics/local/run");
    let (memory, journals) = sizes(
        &workspace.join(".guildbotics/state/documents/memory_events"),
        ".jsonl",
    );
    json!({
        "detail": detail,
        "retention_days": retention,
        "sessions_dir": run.join("sessions"),
        "total_size_bytes": sizes(&run.join("sessions"), ".jsonl").0,
        "index_size_bytes": std::fs::metadata(run.join("diagnostics.jsonl")).map_or(0, |m| m.len()),
        "index_rewrite_threshold_bytes": INDEX_REWRITE_THRESHOLD,
        "memory_size_bytes": memory,
        "memory_max_size_bytes": MEMORY_JOURNAL_LIMIT * journals.max(1),
    })
}

/// The shared transcript setting used by both the screen and pruning.
pub(crate) fn transcript_retention_days(stored: &Value) -> i64 {
    match &stored["retention_days"] {
        Value::Number(n) => n.as_i64().filter(|days| *days != 0),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
    .unwrap_or(DEFAULT_RETENTION_DAYS)
    .max(1)
}

#[test]
fn transcript_retention_matches_frozen_integer_and_whitespace_rules() {
    for (value, days) in [
        (json!(" 7 "), 7),
        (json!("0"), 1),
        (json!(0), 30),
        (json!(-2), 1),
        (json!("invalid"), 30),
        (Value::Null, 30),
    ] {
        assert_eq!(
            transcript_retention_days(&json!({"retention_days": value})),
            days
        );
    }
}

pub(crate) fn unconfigured(headers: &HeaderMap, sentence: &'static str) -> Response {
    ApiError::new(StatusCode::CONFLICT, "workspace_not_configured", headers)
        .key(sentence, vec![])
        .into_response()
}

pub(crate) async fn transcript_settings(State(api): State<Shared>, headers: HeaderMap) -> Response {
    match api.selected().workspace.as_deref() {
        Some(workspace) => Json(transcripts(workspace)).into_response(),
        None => unconfigured(&headers, "workspace_not_configured.plain"),
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum Detail {
    Standard,
    Full,
}

#[derive(Deserialize)]
pub(crate) struct TranscriptRequest {
    detail: Detail,
    retention_days: i64,
}

pub(crate) async fn save_transcript_settings(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<TranscriptRequest>,
) -> Response {
    if !(1..=3650).contains(&request.retention_days) {
        return invalid(&headers, "retention_days must be between 1 and 3650");
    }
    let Some(workspace) = api.selected().workspace.clone() else {
        return unconfigured(&headers, "workspace_not_configured.transcripts");
    };
    let settings = json!({
        "detail": match request.detail { Detail::Standard => "standard", Detail::Full => "full" },
        "retention_days": request.retention_days,
    });
    let saved = with_setup(&api, workspace.clone(), move |setup| {
        setup.repository.write(
            &Revisions::new(),
            |view, writer| writer.write_json(&view.path(TRANSCRIPTS_FILE)?, &settings),
            |_| Ok(Revisions::new()),
        )
    })
    .await;
    match saved {
        Ok(_) => Json(transcripts(&workspace)).into_response(),
        Err(error) => failure(error, &headers),
    }
}

#[cfg(test)]
pub(crate) mod tests;
