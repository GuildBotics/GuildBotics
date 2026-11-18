//! Routes of the upstream, merged into the API router by `http::router`.

use axum::Router;
use axum::extract::State;
use axum::response::{IntoResponse, Response};

use super::ops;
use crate::app::Shared;
use crate::auth::Caller;
use crate::domain::pulls::MergeInput;
use crate::error::{AppError, AppResult};
use crate::http::{J, P};

type R = AppResult<Response>;

fn ok<T: serde::Serialize>(value: T) -> Response {
    axum::Json(value).into_response()
}

fn number(text: &str) -> AppResult<u64> {
    text.parse()
        .ok()
        .filter(|n| *n >= 1)
        .ok_or_else(|| AppError::not_found(format!("No pull request numbered {text}.")))
}

async fn upstream_put(
    State(app): State<Shared>,
    c: Caller,
    P(repo): P<String>,
    J(input): J<ops::UpstreamInput>,
) -> R {
    Ok(ok(ops::set(&app, &c, &repo, input).await?))
}

async fn upstream_delete(State(app): State<Shared>, c: Caller, P(repo): P<String>) -> R {
    Ok(ok(ops::delete(&app, &c, &repo).await?))
}

async fn refresh(State(app): State<Shared>, c: Caller, P(repo): P<String>) -> R {
    Ok(ok(ops::refresh(&app, &c, &repo).await?))
}

async fn account_put(
    State(app): State<Shared>,
    c: Caller,
    P((name, provider)): P<(String, String)>,
    J(body): J<serde_json::Value>,
) -> R {
    // Serde's own message may quote a value, which here may be a secret.
    let credential = serde_json::from_value(body).map_err(|_| {
        AppError::invalid(
            r#"The body is {"token": "..."} or {"app": {"app_id", "installation_id", "private_key"}}."#,
        )
    })?;
    Ok(ok(
        ops::set_account(&app, &c, &name, &provider, credential).await?
    ))
}

async fn account_delete(
    State(app): State<Shared>,
    c: Caller,
    P((name, provider)): P<(String, String)>,
) -> R {
    Ok(ok(ops::delete_account(&app, &c, &name, &provider).await?))
}

async fn preview(State(app): State<Shared>, c: Caller, P((repo, n)): P<(String, String)>) -> R {
    Ok(ok(ops::preview(&app, &c, &repo, number(&n)?).await?))
}

async fn publish(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n)): P<(String, String)>,
    J(input): J<MergeInput>,
) -> R {
    Ok(ok(ops::publish(&app, &c, &repo, number(&n)?, input).await?))
}

// Paths relative to `/api/v1`.
loopback_auth::classified_routes!(pub fn routes() -> Router<Shared>, ROUTES,
    ("/repos/{repo}/upstream", (put, upstream_put, OwnerSession), (delete, upstream_delete, OwnerSession)),
    ("/repos/{repo}/upstream/refresh", (post, refresh, MemberSession)),
    ("/repos/{repo}/pulls/{number}/publish", (get, preview, MemberSession), (post, publish, OwnerSession)),
    ("/members/{name}/accounts/{provider}", (put, account_put, OwnerSession), (delete, account_delete, OwnerSession)),
);
