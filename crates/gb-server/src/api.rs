//! The server's HTTP API under `/-/guildbotics/api`, as far as the Python version's
//! (`guildbotics/app_api/api.py`) has been carried over. Every request passes
//! the Host and Origin checks first, every response gets the security
//! headers, and every route is the owner's except the few that say why they
//! cannot be.

use std::path::PathBuf;
use std::sync::Arc;

use axum::extract::{FromRequest, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, patch, post, put};
use axum::{Json, Router};
use loopback_auth::Refusal;
use serde::Deserialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

use gb_workspace::identity;

use crate::auth::{Denied, SESSION_SECONDS, ServerAuth};
use crate::i18n;

/// Asks for the language of the sentences in an answer.
const LANGUAGE_HEADER: &str = "x-guildbotics-language";

/// The screens' policy: scripts only from the server itself.
use loopback_auth::CSP;

/// What every request of the server shares.
pub struct Api {
    pub(crate) health_lock: tokio::sync::Mutex<()>,
    pub(crate) activity_sync:
        std::sync::Mutex<std::collections::BTreeMap<(PathBuf, String, String), std::time::Instant>>,
    #[cfg(test)]
    pub(crate) diagnostics_runner: std::sync::Mutex<Option<crate::diagnostics::CheckRunner>>,
    #[cfg(test)]
    pub(crate) activity_runner: std::sync::Mutex<Option<crate::activity::RefreshRunner>>,
    #[cfg(test)]
    pub(crate) usage_runner: std::sync::Mutex<Option<crate::environment::UsageRunner>>,
    selection: std::sync::RwLock<Arc<Selected>>,
    /// Read while work for the selected workspace is admitted, and written
    /// while another workspace is selected, so that none is admitted for a
    /// workspace on its way out.
    pub(crate) switching: Arc<tokio::sync::RwLock<()>>,
    /// The code host the server runs, which the selected workspace's member
    /// backend and service reach in process.
    pub branvil: std::sync::OnceLock<branvil::api::DataApi>,
    pub(crate) consents:
        tokio::sync::Mutex<std::collections::BTreeMap<String, crate::provider_api::Consent>>,
    pub events: Arc<crate::events::EventBus>,
    /// The one manual run that writes, by its trace, while it runs.
    pub(crate) manual: Arc<std::sync::Mutex<Option<String>>>,
    pub inputs: crate::input_files::InputFileStore,
    pub auth: ServerAuth,
    /// This run of the server, so a screen sees a restart.
    pub instance_id: String,
    /// The machine's own records: `<machine root>/data`.
    pub data: PathBuf,
    /// The work the server started, which outlives the request that started
    /// it and which a stop waits for.
    pub work: tokio_util::task::TaskTracker,
    /// Cancelled when the server stops without waiting for its work.
    pub cancelled: tokio_util::sync::CancellationToken,
    /// The owner's home, holding the exchange directory.
    pub(crate) home: PathBuf,
    /// The root of the members' identities, which every workspace shares.
    pub(crate) identity_root: PathBuf,
    /// The keychain holding the machine's key to its secret stores.
    pub(crate) keychain: Arc<dyn gb_config::secrets::Keychain>,
    /// How long a config change waits for a lock held elsewhere.
    pub(crate) write_timeout: std::time::Duration,
    pub(crate) shutdown: crate::server::Shutdown,
}

/// The workspace the server opened, if any, and what it built from it,
/// replaced whole when another workspace is selected.
#[derive(Default)]
pub struct Selected {
    pub workspace: Option<PathBuf>,
    pub member: Option<Arc<crate::member::Backend>>,
    pub service: crate::service_runtime::Controller,
}

impl Api {
    pub fn new(
        selected: Selected,
        inputs: crate::input_files::InputFileStore,
        auth: ServerAuth,
        instance_id: String,
        data: PathBuf,
    ) -> Api {
        let events = Arc::new(crate::events::EventBus::default());
        selected.service.service.attach_events(events.clone());
        crate::diagnostic_logging::attach(&events);
        if let Some(workspace) = &selected.workspace {
            crate::diagnostics::prune(workspace);
        }
        events.record_in(
            selected
                .workspace
                .as_deref()
                .map(gb_observability::Diagnostics::open),
        );
        Api {
            health_lock: Default::default(),
            activity_sync: Default::default(),
            #[cfg(test)]
            diagnostics_runner: Default::default(),
            #[cfg(test)]
            activity_runner: Default::default(),
            #[cfg(test)]
            usage_runner: Default::default(),
            selection: std::sync::RwLock::new(Arc::new(selected)),
            switching: Arc::default(),
            branvil: std::sync::OnceLock::new(),
            consents: Default::default(),
            events,
            manual: Arc::default(),
            inputs,
            auth,
            instance_id,
            work: tokio_util::task::TaskTracker::new(),
            cancelled: tokio_util::sync::CancellationToken::new(),
            home: std::env::var_os("HOME")
                .map(PathBuf::from)
                .unwrap_or_default(),
            identity_root: gb_base::home::identity_root()
                .unwrap_or_else(|_| data.parent().unwrap_or(&data).join("identity")),
            keychain: Arc::new(gb_config::secrets::SystemKeychain),
            write_timeout: gb_base::shared_writes::SharedWrites::new(PathBuf::new(), None).timeout,
            shutdown: crate::server::Shutdown::default(),
            data,
        }
    }

    /// Admits work for the selected workspace: a command, a member run or the
    /// service's start, none of which a switch may overtake while it holds
    /// the guard. None while a switch is under way.
    pub(crate) fn admit(&self) -> Option<tokio::sync::OwnedRwLockReadGuard<()>> {
        self.switching.clone().try_read_owned().ok()
    }

    /// The selected workspace and what was built from it.
    pub fn selected(&self) -> Arc<Selected> {
        self.selection
            .read()
            .unwrap_or_else(|p| p.into_inner())
            .clone()
    }

    /// Selects `selected`, whose events are recorded in its workspace.
    pub(crate) fn select(&self, selected: Selected) {
        selected.service.service.attach_events(self.events.clone());
        let debug = gb_config::debug::read(selected.workspace.as_deref());
        crate::diagnostic_logging::level(debug.configured.then_some(debug.log_level.as_str()));
        if let Some(workspace) = &selected.workspace {
            crate::diagnostics::prune(workspace);
        }
        self.events.record_in(
            selected
                .workspace
                .as_deref()
                .map(gb_observability::Diagnostics::open),
        );
        *self.selection.write().unwrap_or_else(|p| p.into_inner()) = Arc::new(selected);
    }
}

pub(crate) type Shared = Arc<Api>;

/// An answer that is not a success: its code, its sentence in the language
/// the request asked for, and what the screen reads beside it.
pub(crate) struct ApiError {
    status: StatusCode,
    code: String,
    pub(crate) language: &'static str,
    sentence: Sentence,
    context: Box<Value>,
}

/// A sentence of `app_api.errors` (the code's own by default), or one a
/// lower layer already wrote.
enum Sentence {
    Key(String, Vec<(String, String)>),
    Reason(String),
}

impl ApiError {
    pub(crate) fn new(
        status: StatusCode,
        code: impl Into<String>,
        headers: &HeaderMap,
    ) -> ApiError {
        let code = code.into();
        let asked = headers
            .get(LANGUAGE_HEADER)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        ApiError {
            status,
            sentence: Sentence::Key(format!("app_api.errors.{code}"), vec![]),
            code,
            language: i18n::language(asked),
            context: Box::new(json!({})),
        }
    }

    /// Says it with the sentence `key`, given `params`.
    pub(crate) fn key(
        mut self,
        key: &'static str,
        params: Vec<(&'static str, String)>,
    ) -> ApiError {
        self.sentence = Sentence::Key(
            format!("app_api.errors.{key}"),
            params.into_iter().map(|(k, v)| (k.to_owned(), v)).collect(),
        );
        self
    }

    /// Says it with the sentence of the locale key `key`, given `params`.
    pub(crate) fn locale(mut self, key: &str, params: Vec<(String, String)>) -> ApiError {
        self.sentence = Sentence::Key(key.into(), params);
        self
    }

    /// Says it with a sentence a lower layer wrote.
    pub(crate) fn reason(mut self, reason: String) -> ApiError {
        self.sentence = Sentence::Reason(reason);
        self
    }

    /// What a screen is told of the refusal as an event: its code and context.
    pub(crate) fn payload(&self) -> serde_json::Map<String, Value> {
        let mut payload = self.context.as_object().cloned().unwrap_or_default();
        payload.insert("code".into(), self.code.clone().into());
        payload
    }

    pub(crate) fn context(mut self, context: Value) -> ApiError {
        self.context = Box::new(context);
        self
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let message = match self.sentence {
            Sentence::Key(key, params) => {
                let params: Vec<(&str, &str)> = params
                    .iter()
                    .map(|(k, v)| (k.as_str(), v.as_str()))
                    .collect();
                i18n::t(self.language, &key, &params)
            }
            Sentence::Reason(reason) => reason,
        };
        (
            self.status,
            Json(json!({"code": self.code, "message": message, "context": self.context})),
        )
            .into_response()
    }
}

/// A JSON body, refused as `validation_error` when it does not match.
pub(crate) struct Body<T>(pub(crate) T);

impl<T: DeserializeOwned, S: Send + Sync> FromRequest<S> for Body<T> {
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        let headers = req.headers().clone();
        let bytes = axum::body::Bytes::from_request(req, state)
            .await
            .map_err(|e| invalid(&headers, &e.body_text()))?;
        Body::parse(&bytes, &headers)
    }
}

impl<T: DeserializeOwned> Body<T> {
    /// The request's `bytes` as JSON, an empty one as `{}`.
    pub(crate) fn parse(bytes: &[u8], headers: &HeaderMap) -> Result<Self, ApiError> {
        serde_json::from_slice(if bytes.is_empty() { b"{}" } else { bytes })
            .map(Body)
            .map_err(|e| invalid(headers, &e.to_string()))
    }
}

/// What was wrong with a request, as the Python version's `errors` list
/// names it.
pub(crate) fn invalid(headers: &HeaderMap, reason: &str) -> ApiError {
    ApiError::new(
        StatusCode::UNPROCESSABLE_ENTITY,
        "validation_error",
        headers,
    )
    .context(json!({"errors": [{"msg": reason}]}))
}

// Register routes and expose the very same routing table to boundary tests.
// New API endpoints belong here, including ones outside /config.
macro_rules! routes {
    ($builder:ident, $table:ident, $(($method:ident, $path:literal, $handler:path $(, $limit:expr)?)),* $(,)?) => {
        fn $builder() -> Router<Shared> {
            Router::new()$(.route($path, $method($handler) $(.layer(axum::extract::DefaultBodyLimit::max($limit)))?))*
        }
        #[cfg(test)]
        const $table: &[(&str, &str)] = &[$((stringify!($method), $path)),*];
    };
}
routes!(
    owner_routes,
    OWNER_ROUTES,
    (
        post,
        "/config/providers/slack/app-registrations",
        crate::provider_api::slack::registration
    ),
    (
        post,
        "/config/providers/slack/verify-tokens",
        crate::provider_api::slack::verify
    ),
    (
        post,
        "/config/members/{name}/avatar/{provider}",
        crate::provider_api::slack::avatar
    ),
    (get, "/config/providers", crate::provider_api::providers),
    (
        post,
        "/config/providers/branvil/connect",
        crate::provider_api::connect
    ),
    (
        post,
        "/config/providers/branvil/connection",
        crate::provider_api::connection
    ),
    (
        post,
        "/config/providers/branvil/workspaces",
        crate::provider_api::workspaces
    ),
    (
        post,
        "/config/providers/branvil/workspaces/create",
        crate::provider_api::create_workspace
    ),
    (
        post,
        "/config/providers/branvil/agents",
        crate::provider_api::agents
    ),
    (
        post,
        "/config/providers/branvil/entry",
        crate::provider_api::entry
    ),
    (get, "/health", health),
    (get, "/activity/history", crate::activity::history),
    (post, "/shutdown", crate::server::shutdown),
    (get, "/scheduler/status", crate::service_runtime::status),
    (post, "/scheduler/start", crate::service_runtime::start),
    (post, "/scheduler/stop", crate::service_runtime::stop),
    (post, "/chat/receive-state/reset", crate::activity::reset),
    (get, "/service/settings", crate::service_settings::get),
    (patch, "/service/settings", crate::service_settings::patch),
    (post, "/apps/authorizations", authorize_app),
    (get, "/session", session),
    (post, "/commands/run", crate::commands::run),
    (post, "/member/run", crate::member::execute, 4 << 20),
    (post, "/member/plan", crate::member::describe, 4 << 20),
    (
        post,
        "/member/git",
        crate::member::exchange,
        (1 << 30) + (4 << 20) + 1
    ),
    (
        post,
        "/commands/input-files",
        crate::input_files::upload,
        crate::input_files::REQUEST_LIMIT
    ),
    (
        post,
        "/intelligences/agent-environment/build",
        crate::environment::build_route
    ),
    (
        get,
        "/intelligences/agent-environment",
        crate::environment::get
    ),
    (
        get,
        "/intelligences/agent-environment/images",
        crate::environment::images
    ),
    (
        get,
        "/intelligences/cli-agents/{agent}/usage",
        crate::environment::usage
    ),
    (get, "/config/status", crate::screens::config_status),
    (post, "/config/init", crate::config_api::init),
    (post, "/workspace", crate::config_api::select_workspace),
    (get, "/device/workspaces", crate::config_api::workspaces),
    (
        delete,
        "/device/workspaces",
        crate::config_api::forget_workspace
    ),
    (get, "/config/project", crate::config_api::read_project),
    (get, "/config/intelligences", crate::intelligences::get),
    (put, "/config/intelligences", crate::intelligences::put),
    (
        get,
        "/intelligences/model-providers",
        crate::intelligences::providers
    ),
    (
        get,
        "/intelligences/grant-evaluation",
        crate::intelligences::preview
    ),
    (
        get,
        "/intelligences/decisions/options",
        crate::intelligences::options
    ),
    (
        post,
        "/intelligences/decisions/credential",
        crate::intelligences::credential
    ),
    (put, "/config/project", crate::config_api::save_project),
    (
        put,
        "/config/project/default-person",
        crate::config_api::default_person
    ),
    (get, "/config/roles", crate::config_api::roles),
    (post, "/config/members", crate::config_api::create_member),
    (
        get,
        "/config/members/{name}",
        crate::config_api::read_member
    ),
    (
        put,
        "/config/members/{name}",
        crate::config_api::save_member
    ),
    (
        delete,
        "/config/members/{name}",
        crate::config_api::remove_member
    ),
    (
        get,
        "/config/members/{name}/avatar",
        crate::config_api::avatar
    ),
    (
        post,
        "/config/members/{name}/avatar",
        crate::config_api::upload_avatar,
        gb_setup::AVATAR_LIMIT + (64 << 10)
    ),
    (get, "/identities", crate::config_api::identities),
    (
        delete,
        "/identities/{name}",
        crate::config_api::delete_identity
    ),
    (
        get,
        "/transcripts/settings",
        crate::config_api::transcript_settings
    ),
    (
        put,
        "/transcripts/settings",
        crate::config_api::save_transcript_settings
    ),
    (get, "/team", crate::screens::team),
    (get, "/commands/options", crate::command_catalog::options),
    (post, "/verify", crate::verify::verify),
    (post, "/commands/author", crate::command_authoring::author),
    (
        get,
        "/commands/routine-options",
        crate::command_catalog::routine_options
    ),
    (
        get,
        "/commands/files/{id}/execution-status",
        crate::command_catalog::execution_status
    ),
    (get, "/commands/files", crate::screens::files),
    (
        post,
        "/commands/files",
        crate::command_files_api::create,
        crate::command_files_api::FILE_REQUEST_LIMIT
    ),
    (
        put,
        "/commands/files/{id}",
        crate::command_files_api::update,
        crate::command_files_api::FILE_REQUEST_LIMIT
    ),
    (
        delete,
        "/commands/files/{id}",
        crate::command_files_api::delete
    ),
    (
        post,
        "/commands/author/apply",
        crate::command_files_api::apply,
        10 * crate::command_files_api::FILE_REQUEST_LIMIT
    ),
    (get, "/commands/files/{id}", crate::screens::file),
    (get, "/diagnostics/traces", crate::screens::traces),
    (
        get,
        "/intelligences/cli-agents/last-turns",
        crate::health::turns
    ),
    (
        get,
        "/intelligences/inference-failures",
        crate::health::failures
    ),
    (get, "/system-alerts", crate::health::alerts),
    (post, "/system-alerts/dismiss", crate::health::dismiss),
    (post, "/diagnostics/scenario", crate::diagnostics::scenario),
    (get, "/diagnostics/global", crate::diagnostics::global),
    (
        post,
        "/diagnostics/troubleshoot",
        crate::diagnostics::troubleshoot
    ),
    (
        get,
        "/diagnostics/memory-events",
        crate::diagnostics::memory_events
    ),
    (get, "/runtime/debug", crate::diagnostics::debug),
    (put, "/runtime/debug", crate::diagnostics::update_debug),
    (get, "/diagnostics/traces/{id}", crate::screens::trace),
);
routes!(
    public_routes,
    PUBLIC_ROUTES,
    // A client asks before trusting the port enough to send its token.
    (post, "/local-api/proof", proof),
    // The entry code is the credential; a browser holds nothing else yet.
    (post, "/session", start_session),
    (post, "/apps/token", app_token),
    (get, "/events", crate::events::stream),
    // This route admits an application token as well as the owner.
    (post, "/session/links", entry_link),
);

#[cfg(test)]
pub fn router(api: Shared) -> Router {
    router_with_host(api, None)
}

pub(crate) fn router_with_host(api: Shared, host: Option<Router>) -> Router {
    let owners =
        owner_routes().route_layer(middleware::from_fn_with_state(api.clone(), authenticate));
    let anyone = public_routes();
    let host = host.unwrap_or_else(|| {
        Router::new().route(
            "/",
            get(|| async { axum::response::Redirect::temporary("/-/guildbotics/") }),
        )
    });
    Router::new()
        .nest("/-/guildbotics/api", owners.merge(anyone))
        .route(
            crate::provider_api::CALLBACK_PATH,
            get(crate::provider_api::connected),
        )
        .merge(crate::ui::router(crate::ui::directory()))
        .route(
            "/-/guildbotics/{*path}",
            axum::routing::any(|| async { StatusCode::NOT_FOUND }),
        )
        .with_state(api.clone())
        .merge(host)
        .layer(middleware::from_fn_with_state(api, guard))
}

/// Refuses a request for another host or from another origin before
/// anything runs for it, and gives every response the security headers.
async fn guard(State(api): State<Shared>, req: Request, next: Next) -> Response {
    let websocket = req.uri().path() == "/-/guildbotics/api/events"
        && req.method() == axum::http::Method::GET
        && req
            .headers()
            .get(header::UPGRADE)
            .is_some_and(|v| v.as_bytes().eq_ignore_ascii_case(b"websocket"));
    let checked = if websocket {
        Ok(())
    } else {
        loopback_auth::check_request(api.auth.base_url(), req.headers())
    };
    let mut response = match checked {
        Ok(()) => next.run(req).await,
        Err(refusal) => {
            let code = match refusal {
                Refusal::Misdirected => "misdirected",
                Refusal::CrossOrigin => "origin_mismatch",
            };
            ApiError::new(refusal.status(), code, req.headers()).into_response()
        }
    };
    loopback_auth::secure(response.headers_mut(), CSP);
    response
}

async fn authenticate(State(api): State<Shared>, req: Request, next: Next) -> Response {
    match api.auth.authenticate(req.method(), req.headers()) {
        Ok(()) => next.run(req).await,
        Err(denied) => auth_denial(denied, req.headers()),
    }
}

fn auth_denial(denied: Denied, headers: &HeaderMap) -> Response {
    let (status, code) = match denied {
        Denied::Unauthorized => (StatusCode::UNAUTHORIZED, "unauthorized"),
        Denied::CsrfMismatch => (StatusCode::FORBIDDEN, "csrf_mismatch"),
    };
    ApiError::new(status, code, headers).into_response()
}

/// The server, the workspace it opened by the identity a client compares
/// with the one it means, and this machine by its name. The identities are
/// read, or created on first use, on every answer.
async fn health(State(api): State<Shared>) -> Response {
    let selected = api.selected();
    let identities = {
        // The identity of the very workspace this answer names.
        let (data, workspace) = (api.data.clone(), selected.workspace.clone());
        tokio::task::spawn_blocking(move || -> anyhow::Result<_> {
            let device = identity::ensure_device_identity(&data)?;
            let workspace = match &workspace {
                Some(workspace) => Some(identity::ensure_workspace_identity(workspace)?),
                None => None,
            };
            Ok((device.display_name, workspace.map(|w| w.workspace_id)))
        })
        .await
    };
    let (machine_name, workspace_id) = match identities {
        Ok(Ok(identities)) => identities,
        Ok(Err(e)) => {
            tracing::error!("cannot read the identities: {e:#}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
        Err(e) => {
            tracing::error!("cannot read the identities: {e}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    Json(json!({
        "status": "ok",
        "version": selected.member.as_ref().map(|v|v.version.as_str()).unwrap_or(env!("CARGO_PKG_VERSION")),
        "workspace_id": selected.member.as_ref().map(|v|v.workspace_id.as_str()).or(workspace_id.as_deref()),
        "service_instance_id": api.instance_id,
        "workspace": selected.workspace,
        "machine_name": machine_name,
    }))
    .into_response()
}

#[derive(Deserialize)]
struct ProofRequest {
    token_id: String,
    nonce: String,
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

async fn proof(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(input): Body<ProofRequest>,
) -> Response {
    if !lower_hex(&input.token_id, 16) || !lower_hex(&input.nonce, 64) {
        return ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .into_response();
    }
    match api.auth.proof(&input.token_id, &input.nonce) {
        Some(proof) => Json(json!({"proof": proof})).into_response(),
        None => ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized", &headers).into_response(),
    }
}

#[derive(Deserialize)]
struct EntryLinkRequest {
    /// Where the browser lands once in: a path of this server.
    #[serde(default = "root")]
    next: String,
}

fn root() -> String {
    "/-/guildbotics/".into()
}

/// A path of this server: not `//host` or `/\host`, which a browser reads as
/// another host.
fn same_origin_path(path: &str) -> bool {
    path.chars().count() <= 2048
        && path.starts_with('/')
        && !matches!(path.as_bytes().get(1), Some(b'/' | b'\\'))
        && !path.chars().any(|c| c.is_whitespace() || c == '#')
}

async fn authorize_app(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<crate::auth::Authorization>,
) -> Response {
    match api.auth.authorize(request) {
        Some(redirect) => {
            (StatusCode::CREATED, Json(json!({ "redirect": redirect }))).into_response()
        }
        None => ApiError::new(StatusCode::BAD_REQUEST, "invalid", &headers).into_response(),
    }
}

async fn app_token(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(request): Body<crate::auth::Exchange>,
) -> Response {
    match api.auth.app_token(request) {
        Ok(Some(token)) => (StatusCode::CREATED, Json(token)).into_response(),
        Ok(None) => ApiError::new(StatusCode::BAD_REQUEST, "invalid", &headers).into_response(),
        Err(error) => {
            tracing::error!("cannot save app token: {error:#}");
            ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "internal_error",
                &headers,
            )
            .into_response()
        }
    }
}

async fn entry_link(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(input): Body<EntryLinkRequest>,
) -> Response {
    if let Err(denied) = api
        .auth
        .authenticate_for(&axum::http::Method::POST, &headers, true)
    {
        return auth_denial(denied, &headers);
    }
    if !same_origin_path(&input.next) {
        return ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .into_response();
    }
    (StatusCode::CREATED, Json(api.auth.entry_link(&input.next))).into_response()
}

#[derive(Deserialize)]
struct SessionRequest {
    code: String,
}

async fn start_session(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(input): Body<SessionRequest>,
) -> Response {
    if !(1..=128).contains(&input.code.len()) {
        return ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
            &headers,
        )
        .into_response();
    }
    let started = match api.auth.start_session(&input.code) {
        Ok(started) => started,
        Err(e) => {
            tracing::error!("cannot save the session: {e:#}");
            return StatusCode::INTERNAL_SERVER_ERROR.into_response();
        }
    };
    let Some((cookie, csrf)) = started else {
        return ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized", &headers).into_response();
    };
    let mut response = Json(json!({"csrf": csrf})).into_response();
    let set_cookie = loopback_auth::session_set_cookie(&api.auth.cookie, &cookie, SESSION_SECONDS);
    if let Ok(value) = HeaderValue::from_str(&set_cookie) {
        response.headers_mut().insert(header::SET_COOKIE, value);
    }
    response
}

async fn session(State(api): State<Shared>, headers: HeaderMap) -> Response {
    match api.auth.session_csrf(&headers) {
        Some(csrf) => Json(json!({"csrf": csrf})).into_response(),
        None => ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized", &headers).into_response(),
    }
}

#[cfg(test)]
mod tests {
    #[tokio::test]
    async fn old_routes_are_absent_and_root_redirects_under_the_guard() {
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let api = Arc::new(Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), 8765).unwrap(),
            "instance".into(),
            root.path().into(),
        ));
        let app = router(api);
        let variable = regex::Regex::new(r"\{[^}]+\}").unwrap();
        let mut paths = vec![
            "/index.html".to_owned(),
            "/-/enter".into(),
            "/-/authorize".into(),
            "/assets/app.js".into(),
            "/providers/branvil/pages/connected".into(),
        ];
        paths.extend(
            OWNER_ROUTES
                .iter()
                .chain(PUBLIC_ROUTES)
                .map(|(_, path)| format!("/api{}", variable.replace_all(path, "missing"))),
        );
        for path in paths {
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::get(&path)
                        .header("host", "127.0.0.1:8765")
                        .body(axum::body::Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::NOT_FOUND, "{path}");
        }
        for (host, status) in [
            ("127.0.0.1:8765", StatusCode::TEMPORARY_REDIRECT),
            ("evil.example", StatusCode::MISDIRECTED_REQUEST),
        ] {
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::get("/")
                        .header("host", host)
                        .body(axum::body::Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), status);
            assert!(response.headers().contains_key("content-security-policy"));
            if status == StatusCode::TEMPORARY_REDIRECT {
                assert_eq!(response.headers()["location"], "/-/guildbotics/");
            }
        }
    }

    #[tokio::test]
    async fn every_owner_route_refuses_missing_and_unknown_credentials_before_running() {
        use super::*;
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let api = Arc::new(Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), 8765).unwrap(),
            "instance".into(),
            root.path().into(),
        ));
        let app = router(api);
        let variable = regex::Regex::new(r"\{[^}]+\}").unwrap();
        for (method, path) in OWNER_ROUTES {
            for unknown in [false, true] {
                let mut request = axum::http::Request::builder()
                    .method(method.to_uppercase().as_str())
                    .uri(format!(
                        "/-/guildbotics/api{}",
                        variable.replace_all(path, "missing")
                    ))
                    .header("host", "127.0.0.1:8765");
                if unknown {
                    request = request.header("authorization", "Bearer gbt_0123456789abcdef_wrong");
                }
                let response = app
                    .clone()
                    .oneshot(request.body(axum::body::Body::empty()).unwrap())
                    .await
                    .unwrap();
                assert_eq!(
                    response.status(),
                    StatusCode::UNAUTHORIZED,
                    "{method} {path}"
                );
                assert_eq!(response.headers()["x-content-type-options"], "nosniff");
                assert!(
                    !response
                        .headers()
                        .keys()
                        .any(|h| h.as_str().starts_with("access-control-"))
                );
                let body: Value = serde_json::from_slice(
                    &axum::body::to_bytes(response.into_body(), 4096)
                        .await
                        .unwrap(),
                )
                .unwrap();
                assert_eq!(
                    body,
                    json!({"code":"unauthorized","message":i18n::t("en","app_api.errors.unauthorized",&[]),"context":{}})
                );
            }
        }
    }
    #[tokio::test]
    async fn error_sentences_follow_request_language_and_explicit_reasons_pass_through() {
        use super::*;
        for language in ["en", "ja"] {
            let mut headers = HeaderMap::new();
            headers.insert(LANGUAGE_HEADER, language.parse().unwrap());
            for reason in [None, Some("Already translated")] {
                let mut error = ApiError::new(
                    StatusCode::BAD_REQUEST,
                    "invalid_activity_history_range",
                    &headers,
                );
                if let Some(reason) = reason {
                    error = error.reason(reason.into());
                }
                let response = error.into_response();
                assert_eq!(response.status(), StatusCode::BAD_REQUEST);
                let body: Value = serde_json::from_slice(
                    &axum::body::to_bytes(response.into_body(), 4096)
                        .await
                        .unwrap(),
                )
                .unwrap();
                assert_eq!(
                    body["message"],
                    reason.map(String::from).unwrap_or_else(|| i18n::t(
                        language,
                        "app_api.errors.invalid_activity_history_range",
                        &[]
                    ))
                );
            }
        }
    }
    #[test]
    fn every_frozen_non_github_endpoint_is_registered() {
        let frozen: Vec<(String, String)> =
            serde_json::from_str(include_str!("../../../records/golden/api-routes.json")).unwrap();
        let variable = regex::Regex::new(r"\{[^}]+\}").unwrap();
        for (method, path) in frozen {
            assert!(
                OWNER_ROUTES
                    .iter()
                    .chain(PUBLIC_ROUTES)
                    .any(|(m, p)| *m == method
                        && variable.replace_all(p, "{}") == variable.replace_all(&path, "{}")),
                "missing {method} {path}"
            );
        }
    }
    use super::*;

    #[test]
    fn every_mutating_route_is_classified_by_what_it_writes() {
        use std::collections::BTreeSet;
        // Every route writing the shared config goes through its compare
        // and answers a held lock with 503: config_api::tests checks both
        // for each of these.
        let config: &[(&str, &str)] = crate::config_api::tests::CONFIG_WRITERS;
        let elsewhere = [
            (
                ("post", "/system-alerts/dismiss"),
                "writes device-local system-alerts.json only",
            ),
            (
                ("post", "/intelligences/decisions/credential"),
                "writes the workspace secret store and shared key index, without screen revisions",
            ),
            (
                ("post", "/diagnostics/scenario"),
                "runs read-only diagnostics and records their result",
            ),
            (
                ("post", "/diagnostics/troubleshoot"),
                "runs a read-only diagnostics assistant",
            ),
            (
                ("put", "/runtime/debug"),
                "writes device-local debug settings",
            ),
            (
                ("post", "/config/providers/branvil/connect"),
                "holds an in-memory PKCE consent",
            ),
            (
                ("post", "/config/providers/branvil/connection"),
                "reads the identity connection and consumes a completed consent",
            ),
            (
                ("post", "/config/providers/branvil/workspaces"),
                "reads Branvil workspaces",
            ),
            (
                ("post", "/config/providers/branvil/workspaces/create"),
                "creates a Branvil workspace, without GuildBotics config writes",
            ),
            (
                ("post", "/config/providers/branvil/agents"),
                "reads Branvil agents",
            ),
            (
                ("post", "/config/providers/branvil/entry"),
                "issues a Branvil browser entry code",
            ),
            (
                ("post", "/config/providers/slack/app-registrations"),
                "builds a Slack manifest URL",
            ),
            (
                ("post", "/config/providers/slack/verify-tokens"),
                "read-only Slack token, scope and channel probes",
            ),
            (
                ("post", "/workspace"),
                "selects a workspace: the device-local registry and selection",
            ),
            (
                ("delete", "/device/workspaces"),
                "the device-local workspace registry",
            ),
            (("post", "/shutdown"), "server lifetime"),
            (("post", "/scheduler/start"), "service lifetime"),
            (("post", "/scheduler/stop"), "service lifetime"),
            (
                ("post", "/chat/receive-state/reset"),
                "shared chat receive state, without config writes",
            ),
            (
                ("patch", "/service/settings"),
                "device-local service settings",
            ),
            (("post", "/member/run"), "member capability execution"),
            (
                ("post", "/member/plan"),
                "member command plan without caller file access",
            ),
            (
                ("post", "/member/git"),
                "private host repository bundle exchange",
            ),
            (("post", "/session/links"), "an entry code held in memory"),
            (("post", "/session"), "machine-local authentication records"),
            (
                ("post", "/apps/authorizations"),
                "one-time consent code held in memory",
            ),
            (("post", "/apps/token"), "machine-local app token digest"),
            (
                ("post", "/local-api/proof"),
                "computes a proof without writing",
            ),
            (
                ("post", "/commands/run"),
                "starts a run, no edited config snapshot",
            ),
            (
                ("post", "/commands/author"),
                "runs the read-only authoring assistant; its changes are applied apart",
            ),
            (
                ("post", "/verify"),
                "reads the workspace and records its check as a trace",
            ),
            (
                ("post", "/commands/input-files"),
                "writes session-owned read-only command inputs",
            ),
            (
                ("post", "/intelligences/agent-environment/build"),
                "builds machine-local snapshots without editing shared config",
            ),
        ];
        let actual: BTreeSet<_> = OWNER_ROUTES
            .iter()
            .chain(PUBLIC_ROUTES)
            .copied()
            .filter(|(method, _)| matches!(*method, "post" | "put" | "patch" | "delete"))
            .chain(
                crate::provider_api::CONFIG_WRITERS
                    .iter()
                    .copied()
                    .filter(|(method, _)| *method == "get"),
            )
            .collect();
        let declared: BTreeSet<_> = config
            .iter()
            .chain(crate::provider_api::CONFIG_WRITERS)
            .copied()
            .chain(elsewhere.iter().map(|(route, reason)| {
                assert!(!reason.is_empty());
                *route
            }))
            .collect();
        assert_eq!(
            actual, declared,
            "classify every mutating endpoint, regardless of URL prefix"
        );
    }

    #[test]
    fn next_is_a_path_of_this_server() {
        for ok in ["/", "/commands?x=1", "/a/b"] {
            assert!(same_origin_path(ok), "{ok}");
        }
        for bad in [
            "",
            "commands",
            "//evil.example",
            "/\\evil.example",
            "/a b",
            "/a#b",
            &format!("/{}", "a".repeat(2048)),
        ] {
            assert!(!same_origin_path(bad), "{bad}");
        }
    }
}
