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
use axum::routing::{any, delete, get, patch, post, put};
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
    pub(crate) command_results: crate::commands::Results,
    pub(crate) manual: Arc<std::sync::Mutex<Option<String>>>,
    pub inputs: crate::input_files::InputFileStore,
    pub auth: Arc<ServerAuth>,
    /// This run of the server, so a screen sees a restart.
    pub instance_id: String,
    /// The machine's own records: `<machine root>/data`.
    pub data: PathBuf,
    /// The work the server started, which outlives the request that started
    /// it and which a stop waits for.
    pub work: tokio_util::task::TaskTracker,
    /// The interactive member operations, which a stopping server keeps
    /// answering until it closes its listener, after its work has drained.
    pub(crate) member_work: tokio_util::task::TaskTracker,
    /// Cancelled when the server stops without waiting for its work.
    pub cancelled: tokio_util::sync::CancellationToken,
    /// The owner's home.
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
            command_results: Default::default(),
            inputs,
            auth: Arc::new(auth),
            instance_id,
            work: tokio_util::task::TaskTracker::new(),
            member_work: tokio_util::task::TaskTracker::new(),
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
    ($builder:ident, $table:ident, $classified:ident, $(($auth:ident, $method:ident, $path:expr, $handler:path $(, $limit:expr)?)),* $(,)?) => {
        fn $builder() -> Router<Shared> {
            Router::new()$(.route($path, $method($handler) $(.layer(axum::extract::DefaultBodyLimit::max($limit)))?))*
        }
        const $classified: &[loopback_auth::Route] = &[$(loopback_auth::Route {
            method: stringify!($method), path: $path, auth: loopback_auth::RouteAuth::$auth,
        }),*];
        #[cfg(test)]
        const $table: &[(&str, &str)] = &[$((stringify!($method), $path)),*];
    };
}
routes!(
    owner_routes,
    OWNER_ROUTES,
    OWNER_CLASSIFIED,
    (
        OwnerOrConsent,
        post,
        "/git-credentials",
        crate::git_credentials::issue
    ),
    (
        OwnerSession,
        post,
        "/config/providers/slack/app-registrations",
        crate::provider_api::slack::registration
    ),
    (
        OwnerSession,
        post,
        "/config/providers/slack/verify-tokens",
        crate::provider_api::slack::verify
    ),
    (
        OwnerSession,
        post,
        "/config/members/{name}/avatar/{provider}",
        crate::provider_api::slack::avatar
    ),
    (
        OwnerSession,
        get,
        "/config/providers",
        crate::provider_api::providers
    ),
    (
        OwnerSession,
        post,
        "/config/providers/branvil/connect",
        crate::provider_api::connect
    ),
    (
        OwnerSession,
        post,
        "/config/providers/branvil/connection",
        crate::provider_api::connection
    ),
    (
        OwnerSession,
        post,
        "/config/providers/branvil/workspaces",
        crate::provider_api::workspaces
    ),
    (
        OwnerSession,
        post,
        "/config/providers/branvil/workspaces/create",
        crate::provider_api::create_workspace
    ),
    (
        OwnerSession,
        post,
        "/config/providers/branvil/agents",
        crate::provider_api::agents
    ),
    (
        OwnerSession,
        post,
        "/config/providers/branvil/entry",
        crate::provider_api::entry
    ),
    (OwnerOrConsent, get, "/health", health),
    (OwnerOrConsent, get, "/clients/{target}", client_manifest),
    (
        OwnerOrConsent,
        get,
        "/clients/{target}/binary",
        client_binary
    ),
    (
        OwnerOrConsent,
        get,
        "/member/redaction-keys",
        redaction_keys
    ),
    (
        OwnerSession,
        get,
        "/activity/history",
        crate::activity::history
    ),
    (OwnerSession, post, "/shutdown", crate::server::shutdown),
    (
        OwnerSession,
        get,
        "/scheduler/status",
        crate::service_runtime::status
    ),
    (
        OwnerSession,
        post,
        "/scheduler/start",
        crate::service_runtime::start
    ),
    (
        OwnerSession,
        post,
        "/scheduler/stop",
        crate::service_runtime::stop
    ),
    (
        OwnerSession,
        post,
        "/chat/receive-state/reset",
        crate::activity::reset
    ),
    (
        OwnerSession,
        get,
        "/service/settings",
        crate::service_settings::get
    ),
    (
        OwnerSession,
        patch,
        "/service/settings",
        crate::service_settings::patch
    ),
    (OwnerSession, post, "/apps/authorizations", authorize_app),
    (Session, get, "/session", session),
    (
        OwnerOrConsent,
        post,
        "/commands/run",
        crate::commands::run,
        32 * 1024 * 1024
    ),
    (
        OwnerOrConsent,
        get,
        "/commands/runs/{id}",
        crate::commands::result
    ),
    (
        OwnerOrConsent,
        post,
        "/member/run",
        crate::member::execute,
        4 << 20
    ),
    (
        OwnerOrConsent,
        post,
        "/member/plan",
        crate::member::describe,
        4 << 20
    ),
    (
        OwnerOrConsent,
        post,
        "/member/git",
        crate::member::exchange,
        (1 << 30) + (4 << 20) + 1
    ),
    (
        OwnerSession,
        post,
        "/commands/input-files",
        crate::input_files::upload,
        crate::input_files::REQUEST_LIMIT
    ),
    (
        OwnerSession,
        post,
        "/intelligences/agent-environment/build",
        crate::environment::build_route
    ),
    (
        OwnerSession,
        get,
        "/intelligences/agent-environment",
        crate::environment::get
    ),
    (
        OwnerSession,
        get,
        "/intelligences/agent-environment/images",
        crate::environment::images
    ),
    (
        OwnerSession,
        get,
        "/intelligences/cli-agents/{agent}/usage",
        crate::environment::usage
    ),
    (
        OwnerSession,
        get,
        "/config/status",
        crate::screens::config_status
    ),
    (OwnerSession, post, "/config/init", crate::config_api::init),
    (
        OwnerSession,
        post,
        "/workspace",
        crate::config_api::select_workspace
    ),
    (
        OwnerSession,
        get,
        "/device/workspaces",
        crate::config_api::workspaces
    ),
    (
        OwnerSession,
        delete,
        "/device/workspaces",
        crate::config_api::forget_workspace
    ),
    (
        OwnerSession,
        get,
        "/config/project",
        crate::config_api::read_project
    ),
    (
        OwnerSession,
        get,
        "/config/intelligences",
        crate::intelligences::get
    ),
    (
        OwnerSession,
        put,
        "/config/intelligences",
        crate::intelligences::put
    ),
    (
        OwnerSession,
        get,
        "/intelligences/model-providers",
        crate::intelligences::providers
    ),
    (
        OwnerSession,
        get,
        "/intelligences/grant-evaluation",
        crate::intelligences::preview
    ),
    (
        OwnerSession,
        get,
        "/intelligences/decisions/options",
        crate::intelligences::options
    ),
    (
        OwnerSession,
        post,
        "/intelligences/decisions/credential",
        crate::intelligences::credential
    ),
    (
        OwnerSession,
        put,
        "/config/project",
        crate::config_api::save_project
    ),
    (
        OwnerSession,
        put,
        "/config/project/default-person",
        crate::config_api::default_person
    ),
    (OwnerSession, get, "/config/roles", crate::config_api::roles),
    (
        OwnerSession,
        post,
        "/config/members",
        crate::config_api::create_member
    ),
    (
        OwnerSession,
        get,
        "/config/members/{name}",
        crate::config_api::read_member
    ),
    (
        OwnerSession,
        put,
        "/config/members/{name}",
        crate::config_api::save_member
    ),
    (
        OwnerSession,
        delete,
        "/config/members/{name}",
        crate::config_api::remove_member
    ),
    (
        OwnerSession,
        get,
        "/config/members/{name}/avatar",
        crate::config_api::avatar
    ),
    (
        OwnerSession,
        post,
        "/config/members/{name}/avatar",
        crate::config_api::upload_avatar,
        gb_setup::AVATAR_LIMIT + (64 << 10)
    ),
    (
        OwnerSession,
        get,
        "/identities",
        crate::config_api::identities
    ),
    (
        OwnerSession,
        delete,
        "/identities/{name}",
        crate::config_api::delete_identity
    ),
    (
        OwnerSession,
        get,
        "/transcripts/settings",
        crate::config_api::transcript_settings
    ),
    (
        OwnerSession,
        put,
        "/transcripts/settings",
        crate::config_api::save_transcript_settings
    ),
    (OwnerSession, get, "/team", crate::screens::team),
    (
        OwnerSession,
        get,
        "/commands/options",
        crate::command_catalog::options
    ),
    (OwnerSession, post, "/verify", crate::verify::verify),
    (
        OwnerSession,
        post,
        "/commands/author",
        crate::command_authoring::author
    ),
    (
        OwnerSession,
        get,
        "/commands/routine-options",
        crate::command_catalog::routine_options
    ),
    (
        OwnerSession,
        get,
        "/commands/files/{id}/execution-status",
        crate::command_catalog::execution_status
    ),
    (OwnerSession, get, "/commands/files", crate::screens::files),
    (
        OwnerSession,
        post,
        "/commands/files",
        crate::command_files_api::create,
        crate::command_files_api::FILE_REQUEST_LIMIT
    ),
    (
        OwnerSession,
        put,
        "/commands/files/{id}",
        crate::command_files_api::update,
        crate::command_files_api::FILE_REQUEST_LIMIT
    ),
    (
        OwnerSession,
        delete,
        "/commands/files/{id}",
        crate::command_files_api::delete
    ),
    (
        OwnerSession,
        post,
        "/commands/author/apply",
        crate::command_files_api::apply,
        10 * crate::command_files_api::FILE_REQUEST_LIMIT
    ),
    (
        OwnerSession,
        get,
        "/commands/files/{id}",
        crate::screens::file
    ),
    (
        OwnerSession,
        get,
        "/diagnostics/traces",
        crate::screens::traces
    ),
    (
        OwnerSession,
        get,
        "/intelligences/cli-agents/last-turns",
        crate::health::turns
    ),
    (
        OwnerSession,
        get,
        "/intelligences/inference-failures",
        crate::health::failures
    ),
    (OwnerSession, get, "/system-alerts", crate::health::alerts),
    (
        OwnerSession,
        post,
        "/system-alerts/dismiss",
        crate::health::dismiss
    ),
    (
        OwnerSession,
        post,
        "/diagnostics/scenario",
        crate::diagnostics::scenario
    ),
    (
        OwnerSession,
        get,
        "/diagnostics/global",
        crate::diagnostics::global
    ),
    (
        OwnerSession,
        post,
        "/diagnostics/troubleshoot",
        crate::diagnostics::troubleshoot
    ),
    (
        OwnerSession,
        get,
        "/diagnostics/memory-events",
        crate::diagnostics::memory_events
    ),
    (
        OwnerSession,
        get,
        "/runtime/debug",
        crate::diagnostics::debug
    ),
    (
        OwnerSession,
        put,
        "/runtime/debug",
        crate::diagnostics::update_debug
    ),
    (
        OwnerSession,
        get,
        "/diagnostics/traces/{id}",
        crate::screens::trace
    ),
    (
        OwnerSession,
        get,
        "/diagnostics/traces/{id}/files",
        crate::run_files::index
    ),
    (
        OwnerSession,
        get,
        "/diagnostics/traces/{id}/file",
        crate::run_files::download
    ),
);
routes!(
    public_routes,
    PUBLIC_ROUTES,
    PUBLIC_CLASSIFIED,
    // A client asks before trusting the port enough to send its token.
    (Public, post, "/local-api/proof", proof),
    // The entry code is the credential; a browser holds nothing else yet.
    (Public, post, "/session", start_session),
    (Public, post, "/apps/token", app_token),
    (Session, get, "/events", crate::events::stream),
    // This route admits an application token as well as the owner.
    (OwnerOrConsent, post, "/session/links", entry_link),
);

routes!(
    outer_routes,
    OUTER_ROUTES,
    OUTER_CLASSIFIED,
    (Public, get, "/-/guildbotics", redirect_to_ui),
    (
        Public,
        get,
        crate::provider_api::CALLBACK_PATH,
        crate::provider_api::connected
    ),
    (Public, any, "/-/guildbotics/{*path}", reserved_not_found),
);
async fn redirect_to_ui() -> axum::response::Redirect {
    axum::response::Redirect::temporary("/-/guildbotics/")
}
async fn reserved_not_found() -> StatusCode {
    StatusCode::NOT_FOUND
}

const ROOT_ROUTE: loopback_auth::Route = loopback_auth::Route {
    method: "get",
    path: "/",
    auth: loopback_auth::RouteAuth::Public,
};

#[cfg(test)]
pub fn router(api: Shared) -> Router {
    router_with_host(api, None)
}

#[derive(Clone, Copy)]
pub(crate) struct EmbeddedHost(pub bool);

/// The routes that start work the server waits for when it stops: a run, the
/// service, an environment build, a usage probe, or another selection. While
/// the server drains, they are refused and every other route still answers.
pub(crate) const REFUSED_WHILE_STOPPING: &[(&str, &str)] = &[
    ("post", "/scheduler/start"),
    ("post", "/commands/run"),
    ("post", "/commands/author"),
    ("post", "/verify"),
    ("post", "/diagnostics/scenario"),
    ("post", "/diagnostics/troubleshoot"),
    ("post", "/intelligences/agent-environment/build"),
    ("get", "/intelligences/cli-agents/{agent}/usage"),
    ("post", "/workspace"),
];

pub(crate) fn router_with_host(api: Shared, host: Option<Router>) -> Router {
    let owners = owner_routes()
        .route_layer(middleware::from_fn_with_state(api.clone(), refuse_new_work))
        .route_layer(middleware::from_fn_with_state(api.clone(), authenticate));
    let anyone = public_routes();
    let integrated = host.is_some();
    let host = host.unwrap_or_else(|| {
        Router::new().route(
            ROOT_ROUTE.path,
            get(|| async { axum::response::Redirect::temporary("/-/guildbotics/") }),
        )
    });
    outer_routes()
        .nest("/-/guildbotics/api", owners.merge(anyone))
        .merge(crate::ui::router(crate::ui::directory()))
        .with_state(api.clone())
        .merge(host)
        .layer(axum::Extension(EmbeddedHost(integrated)))
        .layer(middleware::from_fn_with_state((api, integrated), guard))
}

/// Refuses a request for another host or from another origin before
/// anything runs for it, and gives every response the security headers.
async fn guard(
    State((api, integrated)): State<(Shared, bool)>,
    req: Request,
    next: Next,
) -> Response {
    let websocket = req.uri().path() == "/-/guildbotics/api/events"
        && req.method() == axum::http::Method::GET
        && req
            .headers()
            .get(header::UPGRADE)
            .is_some_and(|v| v.as_bytes().eq_ignore_ascii_case(b"websocket"));
    let hook = integrated && branvil::hook_paths().any(|path| path == req.uri().path());
    let checked = if websocket || hook {
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

async fn refuse_new_work(State(api): State<Shared>, req: Request, next: Next) -> Response {
    if api.shutdown.stopping() {
        // HEAD runs the GET handler.
        let method = if req.method() == axum::http::Method::HEAD {
            "get".to_owned()
        } else {
            req.method().as_str().to_ascii_lowercase()
        };
        let route = req
            .extensions()
            .get::<axum::extract::MatchedPath>()
            .map(|path| path.as_str().trim_start_matches("/-/guildbotics/api"));
        if route.is_some_and(|route| REFUSED_WHILE_STOPPING.contains(&(method.as_str(), route))) {
            return ApiError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "server_stopping",
                req.headers(),
            )
            .into_response();
        }
    }
    next.run(req).await
}

async fn authenticate(State(api): State<Shared>, req: Request, next: Next) -> Response {
    let path = req
        .extensions()
        .get::<axum::extract::MatchedPath>()
        .map(|path| path.as_str().trim_start_matches("/-/guildbotics/api"));
    let method = if req.method() == axum::http::Method::HEAD {
        "get"
    } else {
        req.method().as_str()
    };
    let consent = OWNER_CLASSIFIED
        .iter()
        .chain(PUBLIC_CLASSIFIED)
        .chain(OUTER_CLASSIFIED)
        .any(|route| {
            Some(route.path) == path
                && route.method.eq_ignore_ascii_case(method)
                && route.auth == loopback_auth::RouteAuth::OwnerOrConsent
        });
    match api
        .auth
        .authenticate_for(req.method(), req.headers(), consent)
    {
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

/// Return only the configured names; secret values remain on the Hub.
async fn redaction_keys(State(api): State<Shared>, headers: HeaderMap) -> Response {
    let workspace = api.selected().workspace.clone();
    let identity_root = api.identity_root.clone();
    let names = tokio::task::spawn_blocking(move || -> anyhow::Result<Vec<String>> {
        let mut names = match workspace {
            Some(workspace) => {
                gb_config::secrets::read_secret_index(&workspace.join(".guildbotics/config"))?
            }
            None => Vec::new(),
        };
        names.extend(gb_config::secrets::read_secret_index(
            &identity_root.join(".guildbotics/config"),
        )?);
        names.sort();
        names.dedup();
        Ok(names)
    })
    .await;
    match names {
        Ok(Ok(names)) => Json(names).into_response(),
        _ => ApiError::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            &headers,
        )
        .into_response(),
    }
}

fn release_bytes(api: &Api, target: &str) -> anyhow::Result<Vec<u8>> {
    crate::client_release::load(&api.data, gb_base::BUILD_VERSION, target)
}

async fn client_manifest(
    State(api): State<Shared>,
    headers: HeaderMap,
    axum::extract::Path(target): axum::extract::Path<String>,
) -> Response {
    match release_bytes(&api, &target) {
        Ok(bytes) => Json(json!({"version": gb_base::BUILD_VERSION, "target": target, "sha256": crate::client_release::digest(&bytes)})).into_response(),
        Err(_) => (StatusCode::SERVICE_UNAVAILABLE, Json(json!({"message": crate::i18n::t(i18n::language(headers.get(LANGUAGE_HEADER).and_then(|v| v.to_str().ok()).unwrap_or("en")), "cli.server.update_unavailable", &[])}))).into_response(),
    }
}

async fn client_binary(
    State(api): State<Shared>,
    axum::extract::Path(target): axum::extract::Path<String>,
) -> Response {
    match release_bytes(&api, &target) {
        Ok(bytes) => ([("content-type", "application/octet-stream")], bytes).into_response(),
        Err(_) => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
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
        "version": selected.member.as_ref().map(|v|v.version.as_str()).unwrap_or(gb_base::BUILD_VERSION),
        "workspace_id": selected.member.as_ref().map(|v|v.workspace_id.as_str()).or(workspace_id.as_deref()),
        "service_instance_id": api.instance_id,
        "command_time": crate::commands::Results::now(),
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
    async fn consent_downloads_only_the_running_builds_verified_client() {
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let api = Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), 8765).unwrap(),
            "fixture".into(),
            root.path().into(),
        );
        let token = api.auth.test_consent();
        let app = router(Arc::new(api));
        let request = |path: &str| {
            axum::http::Request::builder()
                .uri(format!("/-/guildbotics/api/clients/{path}"))
                .header("host", "127.0.0.1:8765")
                .header("authorization", format!("Bearer {token}"))
                .header(LANGUAGE_HEADER, "ja")
                .body(axum::body::Body::empty())
                .unwrap()
        };
        let target = crate::client_release::TARGET;
        let unavailable = app.clone().oneshot(request(target)).await.unwrap();
        assert_eq!(unavailable.status(), StatusCode::SERVICE_UNAVAILABLE);
        let message = axum::body::to_bytes(unavailable.into_body(), 4096)
            .await
            .unwrap();
        assert!(String::from_utf8_lossy(&message).contains("まだありません"));
        crate::client_release::store(root.path(), gb_base::BUILD_VERSION, b"verified client")
            .unwrap();
        let manifest = app.clone().oneshot(request(target)).await.unwrap();
        assert_eq!(manifest.status(), StatusCode::OK);
        let body = axum::body::to_bytes(manifest.into_body(), 4096)
            .await
            .unwrap();
        let manifest: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(manifest["version"], gb_base::BUILD_VERSION);
        let binary = app
            .oneshot(request(&format!("{target}/binary")))
            .await
            .unwrap();
        assert_eq!(binary.status(), StatusCode::OK);
        let bytes = axum::body::to_bytes(binary.into_body(), 4096)
            .await
            .unwrap();
        crate::client_release::verify(&bytes, manifest["sha256"].as_str().unwrap()).unwrap();
        assert_eq!(bytes.as_ref(), b"verified client");
    }

    #[tokio::test]
    async fn config_reports_embedded_topology_before_branvil_is_ready() {
        use tower::ServiceExt;
        struct EmptyKeychain;
        impl gb_config::secrets::Keychain for EmptyKeychain {
            fn read(&self) -> anyhow::Result<Option<String>> {
                Ok(None)
            }
            fn create(&self, _: &str) -> anyhow::Result<()> {
                panic!("the topology test never creates a key")
            }
        }
        for embedded in [false, true] {
            let root = tempfile::tempdir().unwrap();
            let mut api = Api::new(
                Selected::default(),
                crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
                ServerAuth::open(root.path(), 8765).unwrap(),
                "fixture".into(),
                root.path().into(),
            );
            api.keychain = Arc::new(EmptyKeychain);
            let api = Arc::new(api);
            assert!(api.branvil.get().is_none());
            let host = embedded.then(Router::new);
            let link = api.auth.entry_link("/");
            let (cookie, csrf) = api
                .auth
                .start_session(link.url.split_once('#').unwrap().1)
                .unwrap()
                .unwrap();
            let cookie = format!("{}={cookie}", api.auth.cookie);
            let app = router_with_host(api, host);
            let workspace = root.path().join("workspace");
            std::fs::create_dir(&workspace).unwrap();
            for (method, path, input, nested) in [
                ("GET", "/config/status", json!({}), false),
                (
                    "POST",
                    "/workspace",
                    json!({"workspace_dir": workspace}),
                    false,
                ),
                ("POST", "/verify", json!({}), true),
            ] {
                let response = app
                    .clone()
                    .oneshot(
                        axum::http::Request::builder()
                            .method(method)
                            .uri(format!("/-/guildbotics/api{path}"))
                            .header("host", "127.0.0.1:8765")
                            .header("cookie", &cookie)
                            .header(crate::auth::CSRF_HEADER, &csrf)
                            .header("content-type", "application/json")
                            .body(axum::body::Body::from(input.to_string()))
                            .unwrap(),
                    )
                    .await
                    .unwrap();
                assert_eq!(response.status(), StatusCode::OK, "{path}");
                let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
                    .await
                    .unwrap();
                let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
                let config = if nested { &body["config"] } else { &body };
                assert_eq!(config["branvil_available"], embedded, "{path}");
            }
        }
    }

    /// The catalogs are the only way in: a router built with `.route()`
    /// would escape the classification tested below.
    #[test]
    fn routers_register_routes_only_through_the_classified_catalogs() {
        for (file, source, direct_routes) in [
            ("api.rs", include_str!("api.rs"), 2), // The catalog macro and standalone root.
            ("ui.rs", include_str!("ui.rs"), 2),   // MAIN_PATHS and ASSET_PATH.
        ] {
            let production = source.split("#[cfg(test)]\nmod tests").next().unwrap();
            assert_eq!(
                production.matches(".route(").count(),
                direct_routes,
                "{file}: register the route in the classified catalog"
            );
        }
    }

    #[tokio::test]
    async fn consent_is_confined_to_cli_routes_and_entry_links() {
        use loopback_auth::RouteAuth;
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let mut api = Api::new(
            Selected {
                workspace: Some(root.path().join("workspace")),
                ..Default::default()
            },
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), 8765).unwrap(),
            "fixture".into(),
            root.path().into(),
        );
        api.identity_root = root.path().join("identity");
        let config = api.identity_root.join(".guildbotics/config");
        std::fs::create_dir_all(&config).unwrap();
        std::fs::write(config.join("secrets.yml"), "keys: [CUSTOM_CREDENTIAL]\n").unwrap();
        let workspace_config = root.path().join("workspace/.guildbotics/config");
        std::fs::create_dir_all(&workspace_config).unwrap();
        std::fs::write(
            workspace_config.join("secrets.yml"),
            "keys: [WORKSPACE_CREDENTIAL, CUSTOM_CREDENTIAL]\n",
        )
        .unwrap();
        let token = api.auth.test_consent();
        let app = router(Arc::new(api));
        let permitted = [
            ("get", "/health"),
            ("get", "/clients/{target}"),
            ("get", "/clients/{target}/binary"),
            ("post", "/commands/run"),
            ("get", "/commands/runs/{id}"),
            ("get", "/member/redaction-keys"),
            ("post", "/member/run"),
            ("post", "/member/plan"),
            ("post", "/member/git"),
            ("post", "/git-credentials"),
            ("post", "/session/links"),
        ];
        let variable = regex::Regex::new(r"\{[^}]+\}").unwrap();
        for route in OWNER_CLASSIFIED.iter().chain(PUBLIC_CLASSIFIED) {
            let allowed = permitted.contains(&(route.method, route.path));
            assert_eq!(
                route.auth == RouteAuth::OwnerOrConsent,
                allowed,
                "{route:?}"
            );
            if !matches!(
                route.auth,
                RouteAuth::OwnerSession | RouteAuth::OwnerOrConsent
            ) {
                continue;
            }
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::builder()
                        .method(route.method.to_uppercase().as_str())
                        .uri(format!(
                            "/-/guildbotics/api{}",
                            variable.replace_all(route.path, "missing")
                        ))
                        .header("host", "127.0.0.1:8765")
                        .header("authorization", format!("Bearer {token}"))
                        .body(axum::body::Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            if allowed {
                assert_ne!(response.status(), StatusCode::UNAUTHORIZED, "{route:?}");
                if route.path == "/member/redaction-keys" {
                    let body = axum::body::to_bytes(response.into_body(), 4096)
                        .await
                        .unwrap();
                    assert_eq!(
                        serde_json::from_slice::<Value>(&body).unwrap(),
                        json!(["CUSTOM_CREDENTIAL", "WORKSPACE_CREDENTIAL"])
                    );
                }
            } else {
                assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{route:?}");
            }
        }
    }

    #[tokio::test]
    async fn classified_routes_cover_the_combined_guard_and_reserved_namespace() {
        assert_eq!(OUTER_ROUTES.len(), OUTER_CLASSIFIED.len());
        use loopback_auth::{Route, RouteAuth};
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let api = Arc::new(Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), 8765).unwrap(),
            "fixture".into(),
            root.path().into(),
        ));
        let host = Router::new().fallback(|| async { StatusCode::IM_A_TEAPOT });
        let link = api.auth.entry_link("/");
        let (cookie, _) = api
            .auth
            .start_session(link.url.split_once('#').unwrap().1)
            .unwrap()
            .unwrap();
        let cookie = format!("{}={cookie}", api.auth.cookie);
        let app = router_with_host(api, Some(host));
        let mut routes: Vec<(String, Route)> = OWNER_CLASSIFIED
            .iter()
            .chain(PUBLIC_CLASSIFIED)
            .map(|r| (format!("/-/guildbotics/api{}", r.path), *r))
            .collect();
        routes.extend(
            OUTER_CLASSIFIED
                .iter()
                .chain(crate::ui::route_inventory().iter())
                .map(|r| (r.path.to_owned(), *r)),
        );
        routes.push((ROOT_ROUTE.path.to_owned(), ROOT_ROUTE));
        let branvil = branvil::embedded_route_inventory();
        for (path, _) in &branvil {
            assert!(!path.starts_with("/-/guildbotics"), "Branvil owns {path}");
        }
        routes.extend(branvil);
        let variable = regex::Regex::new(r"\{[^}]+\}").unwrap();
        let mut seen = std::collections::BTreeSet::new();
        for (path, route) in routes {
            assert!(
                seen.insert((route.method, path.clone())),
                "duplicate {path}"
            );
            let path = variable.replace_all(&path, "missing");
            let methods: &[&str] = match route.method {
                "any" => &[
                    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT",
                ],
                "get" => &["GET", "HEAD"],
                method => &[method],
            };
            for method in methods {
                for (host, origin, expected) in [
                    (
                        "evil.invalid",
                        "http://127.0.0.1:8765",
                        StatusCode::MISDIRECTED_REQUEST,
                    ),
                    (
                        "127.0.0.1:8765",
                        "http://evil.invalid",
                        StatusCode::FORBIDDEN,
                    ),
                ] {
                    let response = app
                        .clone()
                        .oneshot(
                            axum::http::Request::builder()
                                .method(method.to_uppercase().as_str())
                                .uri(path.as_ref())
                                .header("host", host)
                                .header("origin", origin)
                                .body(axum::body::Body::empty())
                                .unwrap(),
                        )
                        .await
                        .unwrap();
                    let expected = if route.auth == RouteAuth::HookSecret {
                        StatusCode::IM_A_TEAPOT
                    } else {
                        expected
                    };
                    assert_eq!(response.status(), expected, "{method} {path}: {route:?}");
                }
            }
        }
        for route in OWNER_CLASSIFIED
            .iter()
            .chain(PUBLIC_CLASSIFIED)
            .filter(|r| {
                matches!(r.auth, RouteAuth::OwnerSession | RouteAuth::OwnerOrConsent)
                    && r.method != "get"
            })
        {
            let path = format!(
                "/-/guildbotics/api{}",
                variable.replace_all(route.path, "missing")
            );
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::builder()
                        .method(route.method.to_uppercase().as_str())
                        .uri(&path)
                        .header("host", "127.0.0.1:8765")
                        .header("cookie", &cookie)
                        .body(axum::body::Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::FORBIDDEN, "{route:?}");
            let body = axum::body::to_bytes(response.into_body(), usize::MAX)
                .await
                .unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&body).unwrap()["code"],
                "csrf_mismatch"
            );
        }
        // Even an otherwise public GUI fallback must not enter this namespace.
        for (path, route) in branvil::embedded_route_inventory() {
            let path = format!("/-/guildbotics{}", variable.replace_all(&path, "missing"));
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::builder()
                        .method(
                            if route.method == "any" {
                                "GET".to_owned()
                            } else {
                                route.method.to_uppercase()
                            }
                            .as_str(),
                        )
                        .uri(&path)
                        .header("host", "127.0.0.1:8765")
                        .body(axum::body::Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::NOT_FOUND, "{path}");
        }
        assert_eq!(crate::provider_api::CALLBACK_PATH, OUTER_CLASSIFIED[1].path);
    }

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
    async fn guildbotics_entry_redirect_precedes_the_host_fallback() {
        use super::*;
        use tower::ServiceExt;
        let root = tempfile::tempdir().unwrap();
        let api = Arc::new(Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), 7878).unwrap(),
            "fixture".into(),
            root.path().into(),
        ));
        let app = router_with_host(api, Some(Router::new().fallback(|| async { "host GUI" })));
        for path in ["/-/guildbotics", "/other"] {
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::get(path)
                        .header("host", "127.0.0.1:7878")
                        .body(axum::body::Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            if path == "/other" {
                assert_eq!(response.status(), StatusCode::OK);
            } else {
                assert_eq!(response.status(), StatusCode::TEMPORARY_REDIRECT);
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

    /// Every route that changes something is either refused while the server
    /// stops, or says why it starts nothing the stop waits for.
    #[test]
    fn every_mutating_route_is_classified_for_a_stopping_server() {
        use std::collections::BTreeSet;
        let allowed: &[((&str, &str), &str)] = &[
            (("post", "/shutdown"), "asks for the stop itself"),
            (("post", "/scheduler/stop"), "stops the service"),
            (
                ("post", "/member/run"),
                "interactive member operations, drained last",
            ),
            (("post", "/member/plan"), "plans a member operation"),
            (
                ("post", "/member/git"),
                "interactive repository exchange, drained last",
            ),
            (
                ("post", "/commands/input-files"),
                "stores inputs; a run starts apart",
            ),
            (("post", "/git-credentials"), "issues a token"),
            (("post", "/chat/receive-state/reset"), "shared chat state"),
            (("patch", "/service/settings"), "service settings"),
            (("post", "/apps/authorizations"), "a consent code"),
            (("post", "/apps/token"), "an app token"),
            (("post", "/session"), "a browser session"),
            (("post", "/session/links"), "an entry code"),
            (("post", "/local-api/proof"), "a proof"),
            (("post", "/system-alerts/dismiss"), "device-local alerts"),
            (("put", "/runtime/debug"), "device-local debug settings"),
            (
                ("delete", "/device/workspaces"),
                "forgets a registered workspace",
            ),
            (("post", "/config/init"), "writes configuration"),
            (("put", "/config/intelligences"), "writes configuration"),
            (
                ("post", "/intelligences/decisions/credential"),
                "stores a key",
            ),
            (("put", "/config/project"), "writes configuration"),
            (
                ("put", "/config/project/default-person"),
                "writes configuration",
            ),
            (("post", "/config/members"), "writes configuration"),
            (("put", "/config/members/{name}"), "writes configuration"),
            (("delete", "/config/members/{name}"), "writes configuration"),
            (
                ("post", "/config/members/{name}/avatar"),
                "writes configuration",
            ),
            (("delete", "/identities/{name}"), "writes an identity"),
            (("put", "/transcripts/settings"), "writes configuration"),
            (("post", "/commands/files"), "writes a command file"),
            (("put", "/commands/files/{id}"), "writes a command file"),
            (("delete", "/commands/files/{id}"), "writes a command file"),
            (("post", "/commands/author/apply"), "writes command files"),
            (
                ("post", "/config/members/{name}/avatar/{provider}"),
                "fetches an avatar",
            ),
            (
                ("post", "/config/providers/slack/app-registrations"),
                "builds a manifest URL",
            ),
            (
                ("post", "/config/providers/slack/verify-tokens"),
                "probes Slack",
            ),
            (
                ("post", "/config/providers/branvil/connect"),
                "a consent in memory",
            ),
            (
                ("post", "/config/providers/branvil/connection"),
                "reads a connection",
            ),
            (
                ("post", "/config/providers/branvil/workspaces"),
                "reads Branvil",
            ),
            (
                ("post", "/config/providers/branvil/workspaces/create"),
                "creates a Branvil workspace",
            ),
            (
                ("post", "/config/providers/branvil/agents"),
                "reads Branvil",
            ),
            (("post", "/config/providers/branvil/entry"), "an entry code"),
        ];
        let refused: BTreeSet<_> = REFUSED_WHILE_STOPPING.iter().copied().collect();
        let routes: BTreeSet<_> = OWNER_ROUTES.iter().chain(PUBLIC_ROUTES).copied().collect();
        assert!(
            refused.is_subset(&routes),
            "{:?}",
            refused.difference(&routes)
        );
        let mutating: BTreeSet<_> = routes
            .iter()
            .copied()
            .filter(|(method, _)| *method != "get")
            .collect();
        let classified: BTreeSet<_> = allowed
            .iter()
            .map(|(route, _)| *route)
            .chain(
                refused
                    .iter()
                    .copied()
                    .filter(|(method, _)| *method != "get"),
            )
            .collect();
        assert_eq!(classified, mutating);
    }

    #[tokio::test]
    async fn a_stopping_server_refuses_only_what_starts_work() {
        use tower::ServiceExt;
        // No request here may reach the machine's keychain.
        struct Untouchable;
        impl gb_config::secrets::Keychain for Untouchable {
            fn read(&self) -> anyhow::Result<Option<String>> {
                panic!("the keychain was reached")
            }
            fn create(&self, _: &str) -> anyhow::Result<()> {
                panic!("the keychain was reached")
            }
        }
        let root = tempfile::tempdir().unwrap();
        let mut api = Api::new(
            Selected::default(),
            crate::input_files::InputFileStore::deferred(&root.path().join("inputs")),
            ServerAuth::open(root.path(), 8765).unwrap(),
            "instance".into(),
            root.path().into(),
        );
        api.keychain = Arc::new(Untouchable);
        let api = Arc::new(api);
        let token = std::fs::read_to_string(api.auth.owner_token_path()).unwrap();
        let app = router(api.clone());
        let variable = regex::Regex::new(r"\{[^}]+\}").unwrap();
        let refusals = async |path: &str, method: &str| {
            let response = app
                .clone()
                .oneshot(
                    axum::http::Request::builder()
                        .method(method.to_uppercase().as_str())
                        .uri(format!(
                            "/-/guildbotics/api{}",
                            variable.replace_all(path, "codex")
                        ))
                        .header("host", "127.0.0.1:8765")
                        .header("authorization", format!("Bearer {}", token.trim()))
                        // Not JSON: a handler that is reached refuses it first.
                        .body(axum::body::Body::from("not json"))
                        .unwrap(),
                )
                .await
                .unwrap();
            let status = response.status();
            let body = axum::body::to_bytes(response.into_body(), usize::MAX)
                .await
                .unwrap();
            let code = serde_json::from_slice::<Value>(&body).unwrap_or_default()["code"].clone();
            (status, code)
        };
        // Only requests that end before any handler does work: they reach
        // neither the work nor the keychain.
        assert_eq!(
            refusals("/commands/run", "post").await,
            (StatusCode::UNPROCESSABLE_ENTITY, json!("validation_error"))
        );
        api.shutdown.request(false);
        for (method, path) in REFUSED_WHILE_STOPPING {
            assert_eq!(
                refusals(path, method).await,
                (StatusCode::SERVICE_UNAVAILABLE, json!("server_stopping")),
                "{path}"
            );
        }
        // HEAD runs the GET handler, so it is refused alike.
        assert_eq!(
            refusals("/intelligences/cli-agents/{agent}/usage", "head")
                .await
                .0,
            StatusCode::SERVICE_UNAVAILABLE
        );
        // The running work still reaches its members.
        assert_eq!(
            refusals("/member/plan", "post").await,
            (StatusCode::UNPROCESSABLE_ENTITY, json!("validation_error"))
        );
    }

    #[test]
    fn every_mutating_route_is_classified_by_what_it_writes() {
        use std::collections::BTreeSet;
        // Every route writing the shared config goes through its compare
        // and answers a held lock with 503: config_api::tests checks both
        // for each of these.
        let config: &[(&str, &str)] = crate::config_api::tests::CONFIG_WRITERS;
        let elsewhere = [
            (
                ("post", "/git-credentials"),
                "rotates provider tokens without writing shared configuration",
            ),
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
