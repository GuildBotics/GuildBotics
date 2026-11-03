//! The server's HTTP API under `/api`, as far as the Python version's
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
use axum::routing::{get, post};
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
const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; \
img-src 'self' data:; connect-src 'self' ipc: http://ipc.localhost; \
form-action 'self'; frame-ancestors 'none'; base-uri 'none'";

/// What every request of the server shares.
pub struct Api {
    pub auth: ServerAuth,
    /// This run of the server, so a screen sees a restart.
    pub instance_id: String,
    /// The machine's own records: `<machine root>/data`.
    pub data: PathBuf,
    /// The workspace the server opened, if any.
    pub workspace: Option<PathBuf>,
    /// The work the server started, which outlives the request that started
    /// it and which a stop waits for.
    pub work: tokio_util::task::TaskTracker,
    /// Cancelled when the server stops without waiting for its work.
    pub cancelled: tokio_util::sync::CancellationToken,
}

pub(crate) type Shared = Arc<Api>;

/// An answer that is not a success: its code, its sentence in the language
/// the request asked for, and what the screen reads beside it.
pub(crate) struct ApiError {
    status: StatusCode,
    code: &'static str,
    pub(crate) language: &'static str,
    sentence: Sentence,
    context: Value,
}

/// A sentence of `app_api.errors` (the code's own by default), or one a
/// lower layer already wrote.
enum Sentence {
    Key(&'static str, Vec<(&'static str, String)>),
    Reason(String),
}

impl ApiError {
    pub(crate) fn new(status: StatusCode, code: &'static str, headers: &HeaderMap) -> ApiError {
        let asked = headers
            .get(LANGUAGE_HEADER)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        ApiError {
            status,
            code,
            language: i18n::language(asked),
            sentence: Sentence::Key(code, vec![]),
            context: json!({}),
        }
    }

    /// Says it with the sentence `key`, given `params`.
    pub(crate) fn key(
        mut self,
        key: &'static str,
        params: Vec<(&'static str, String)>,
    ) -> ApiError {
        self.sentence = Sentence::Key(key, params);
        self
    }

    /// Says it with a sentence a lower layer wrote.
    pub(crate) fn reason(mut self, reason: String) -> ApiError {
        self.sentence = Sentence::Reason(reason);
        self
    }

    pub(crate) fn context(mut self, context: Value) -> ApiError {
        self.context = context;
        self
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let message = match self.sentence {
            Sentence::Key(key, params) => {
                let params: Vec<(&str, &str)> =
                    params.iter().map(|(k, v)| (*k, v.as_str())).collect();
                i18n::t(self.language, &format!("app_api.errors.{key}"), &params)
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
        let invalid = || {
            ApiError::new(
                StatusCode::UNPROCESSABLE_ENTITY,
                "validation_error",
                &headers,
            )
        };
        let bytes = axum::body::Bytes::from_request(req, state)
            .await
            .map_err(|_| invalid())?;
        serde_json::from_slice(if bytes.is_empty() { b"{}" } else { &bytes })
            .map(Body)
            .map_err(|_| invalid())
    }
}

pub fn router(api: Shared) -> Router {
    let owners = Router::new()
        .route("/health", get(health))
        .route("/session/links", post(entry_link))
        .route("/session", get(session))
        .route("/commands/run", post(crate::commands::run))
        .route_layer(middleware::from_fn_with_state(api.clone(), authenticate));
    // Each says why it cannot be the owner's.
    let anyone = Router::new()
        // A client asks this before it trusts the port enough to send its
        // token, and the answer reveals nothing a caller could reuse.
        .route("/local-api/proof", post(proof))
        // The entry code is the credential: a browser holds nothing else yet.
        .route("/session", post(start_session));
    Router::new()
        .nest("/api", owners.merge(anyone))
        .merge(crate::ui::router(crate::ui::directory()))
        .layer(middleware::from_fn_with_state(api.clone(), guard))
        .with_state(api)
}

/// Refuses a request for another host or from another origin before
/// anything runs for it, and gives every response the security headers.
async fn guard(State(api): State<Shared>, req: Request, next: Next) -> Response {
    let mut response = match loopback_auth::check_request(api.auth.base_url(), req.headers()) {
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
        Err(Denied::Unauthorized) => {
            ApiError::new(StatusCode::UNAUTHORIZED, "unauthorized", req.headers()).into_response()
        }
        Err(Denied::CsrfMismatch) => {
            ApiError::new(StatusCode::FORBIDDEN, "csrf_mismatch", req.headers()).into_response()
        }
    }
}

/// The server, the workspace it opened by the identity a client compares
/// with the one it means, and this machine by its name. The identities are
/// read, or created on first use, on every answer.
async fn health(State(api): State<Shared>) -> Response {
    let identities = {
        let api = api.clone();
        tokio::task::spawn_blocking(move || -> anyhow::Result<_> {
            let device = identity::ensure_device_identity(&api.data)?;
            let workspace = match &api.workspace {
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
        "version": env!("CARGO_PKG_VERSION"),
        "workspace_id": workspace_id,
        "service_instance_id": api.instance_id,
        "workspace": api.workspace,
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
    "/".into()
}

/// A path of this server: not `//host` or `/\host`, which a browser reads as
/// another host.
fn same_origin_path(path: &str) -> bool {
    path.chars().count() <= 2048
        && path.starts_with('/')
        && !matches!(path.as_bytes().get(1), Some(b'/' | b'\\'))
        && !path.chars().any(|c| c.is_whitespace() || c == '#')
}

async fn entry_link(
    State(api): State<Shared>,
    headers: HeaderMap,
    Body(input): Body<EntryLinkRequest>,
) -> Response {
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
    use super::*;

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
