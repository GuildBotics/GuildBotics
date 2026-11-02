//! HTTP boundary: routing, Host/Origin checks, authentication, idempotency,
//! security headers, request parsing, and the GUI's static files. Handlers
//! only parse the request and call `domain`.

use std::collections::BTreeMap;
use std::convert::Infallible;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{FromRequest, FromRequestParts, Path, Query, Request, State};
use axum::http::request::Parts;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get, post, put};
use futures_util::Stream;
use serde::de::DeserializeOwned;
use serde_json::json;

use crate::app::Shared;
use crate::auth::{self, Caller};
use crate::domain::{self, PageReq, apps, attachments, members, pulls, repos, tickets};
use crate::error::{AppError, AppResult};
use crate::model::IdempotencyRecord;
use crate::store::{self, Key, Rd};
use crate::util;

// ---------------------------------------------------------------- extractors

impl FromRequestParts<Shared> for Caller {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, app: &Shared) -> Result<Self, Self::Rejection> {
        if let Some(c) = parts.extensions.get::<Caller>() {
            return Ok(c.clone());
        }
        auth::authenticate(app, &parts.method, &parts.headers).await
    }
}

/// JSON body with errors in the API's error shape.
pub struct J<T>(pub T);

impl<T: DeserializeOwned, S: Send + Sync> FromRequest<S> for J<T> {
    type Rejection = AppError;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        let bytes = Bytes::from_request(req, state)
            .await
            .map_err(|_| AppError::too_large("The request body is too large."))?;
        let bytes = if bytes.is_empty() {
            Bytes::from_static(b"{}")
        } else {
            bytes
        };
        serde_json::from_slice(&bytes)
            .map(J)
            .map_err(|e| AppError::invalid(format!("Invalid JSON body: {e}")))
    }
}

/// Path parameters with errors in the API's error shape.
pub struct P<T>(pub T);

impl<T: DeserializeOwned + Send, S: Send + Sync> FromRequestParts<S> for P<T> {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        Path::<T>::from_request_parts(parts, state)
            .await
            .map(|Path(v)| P(v))
            .map_err(|e| AppError::not_found(format!("No such path ({e}).")))
    }
}

/// The query string as a map.
pub struct Q(pub BTreeMap<String, String>);

impl<S: Send + Sync> FromRequestParts<S> for Q {
    type Rejection = AppError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        Query::<BTreeMap<String, String>>::from_request_parts(parts, state)
            .await
            .map(|Query(v)| Q(v))
            .map_err(|_| AppError::invalid("Invalid query string."))
    }
}

impl Q {
    fn get(&self, k: &str) -> Option<String> {
        self.0.get(k).cloned()
    }

    /// The page request, its cursor bound to the collection, the caller, and
    /// every other query parameter.
    pub(crate) fn page(
        &self,
        app: &Shared,
        caller: &Caller,
        collection: &str,
    ) -> AppResult<PageReq> {
        let limit = match self.0.get("limit") {
            Some(l) => Some(
                l.parse::<usize>()
                    .map_err(|_| AppError::invalid("limit must be a number."))?,
            ),
            None => None,
        };
        let conditions: Vec<String> = self
            .0
            .iter()
            .filter(|(k, _)| *k != "limit" && *k != "cursor")
            .map(|(k, v)| format!("{k}={v}"))
            .collect();
        let binding = format!(
            "{collection}\n{}\n{}",
            util::fold(caller.name()),
            conditions.join("&")
        );
        PageReq::new(
            &app.cursor_key,
            binding,
            limit,
            self.0.get("cursor").map(String::as_str),
        )
    }
}

fn number(text: &str) -> AppResult<u64> {
    text.parse()
        .ok()
        .filter(|n| *n >= 1)
        .ok_or_else(|| AppError::not_found(format!("No pull request numbered {text}.")))
}

fn ok<T: serde::Serialize>(value: T) -> Response {
    axum::Json(value).into_response()
}

fn created<T: serde::Serialize>(value: T) -> Response {
    (StatusCode::CREATED, axum::Json(value)).into_response()
}

type R = AppResult<Response>;

// ---------------------------------------------------------------- service and members

async fn health() -> Response {
    ok(json!({"service": "branvil", "version": env!("CARGO_PKG_VERSION"), "api": 1}))
}

async fn me(c: Caller) -> R {
    Ok(ok(members::member_view(&c.member)))
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct Enter {
    code: String,
}

/// Exchanges an entry link's code for the owner's session cookie. The only
/// unauthenticated write; the Host and Origin checks apply as everywhere.
async fn session_create(State(app): State<Shared>, J(input): J<Enter>) -> R {
    let (view, id) = members::enter(&app, input.code).await?;
    let max_age = app.config.limits.session_hours * 3600;
    let cookie = format!(
        "{}={id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}",
        auth::SESSION_COOKIE
    );
    let mut resp = ok(view);
    resp.headers_mut().insert(
        header::SET_COOKIE,
        HeaderValue::from_str(&cookie).map_err(AppError::internal)?,
    );
    Ok(resp)
}

async fn session_get(State(app): State<Shared>, headers: HeaderMap) -> R {
    let id = auth::session_cookie(&headers).ok_or_else(AppError::unauthorized)?;
    Ok(ok(members::current_session(&app, id).await?))
}

async fn session_link(State(app): State<Shared>, c: Caller) -> R {
    Ok(created(members::entry_link(&app, &c)?))
}

async fn member_get(State(app): State<Shared>, c: Caller, P(name): P<String>) -> R {
    Ok(ok(members::get(&app, &c, name).await?))
}

async fn avatar_get(State(app): State<Shared>, _c: Caller, P(name): P<String>) -> R {
    let (kind, bytes) = members::avatar(&app, name).await?;
    // The address changes with the picture, so it never needs asking again.
    Ok((
        [
            (header::CONTENT_TYPE, kind),
            (
                header::CACHE_CONTROL,
                "private, max-age=31536000, immutable",
            ),
        ],
        bytes,
    )
        .into_response())
}

async fn avatar_put(State(app): State<Shared>, c: Caller, P(name): P<String>, body: Body) -> R {
    // One byte over the limit is enough for the domain to refuse it.
    let image = axum::body::to_bytes(body, members::MAX_AVATAR_BYTES + 1)
        .await
        .map_err(|_| AppError::too_large("The picture is too large."))?;
    Ok(ok(members::set_avatar(
        &app,
        &c,
        name,
        Some(image.to_vec()),
    )
    .await?))
}

async fn avatar_delete(State(app): State<Shared>, c: Caller, P(name): P<String>) -> R {
    Ok(ok(members::set_avatar(&app, &c, name, None).await?))
}

async fn members_list(State(app): State<Shared>, c: Caller, q: Q) -> R {
    let page = q.page(&app, &c, "members")?;
    Ok(ok(members::list(&app, &c, page).await?))
}

async fn members_create(
    State(app): State<Shared>,
    c: Caller,
    J(input): J<members::NewMember>,
) -> R {
    Ok(created(members::create(&app, &c, input).await?))
}

async fn member_patch(
    State(app): State<Shared>,
    c: Caller,
    P(name): P<String>,
    J(patch): J<members::MemberPatch>,
) -> R {
    Ok(ok(members::update(&app, &c, name, patch).await?))
}

async fn member_reviewer_put(
    State(app): State<Shared>,
    c: Caller,
    P((name, reviewer)): P<(String, String)>,
) -> R {
    Ok(ok(
        members::set_reviewer(&app, &c, name, reviewer, true).await?
    ))
}

async fn member_reviewer_delete(
    State(app): State<Shared>,
    c: Caller,
    P((name, reviewer)): P<(String, String)>,
) -> R {
    Ok(ok(
        members::set_reviewer(&app, &c, name, reviewer, false).await?
    ))
}

async fn tokens_create(
    State(app): State<Shared>,
    c: Caller,
    P(name): P<String>,
    J(input): J<members::NewToken>,
) -> R {
    Ok(created(members::issue_token(&app, &c, name, input).await?))
}

async fn tokens_list(State(app): State<Shared>, c: Caller, P(name): P<String>, q: Q) -> R {
    let page = q.page(&app, &c, &format!("tokens\n{name}"))?;
    Ok(ok(members::list_tokens(&app, &c, name, page).await?))
}

async fn token_delete(
    State(app): State<Shared>,
    c: Caller,
    P((name, id)): P<(String, String)>,
) -> R {
    members::revoke_token(&app, &c, name, id).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

async fn app_authorize(
    State(app): State<Shared>,
    c: Caller,
    J(input): J<apps::NewAuthorization>,
) -> R {
    Ok(created(apps::authorize(&app, &c, input)?))
}

/// Exchanges a local app's code for its owner token. Unauthenticated (the
/// code and the PKCE verifier are the proof); the Host and Origin checks
/// apply as everywhere.
async fn app_token(State(app): State<Shared>, J(input): J<apps::TokenRequest>) -> R {
    Ok(created(apps::exchange(&app, input).await?))
}

// ---------------------------------------------------------------- workspaces, repositories

async fn workspaces_list(State(app): State<Shared>, c: Caller, q: Q) -> R {
    let page = q.page(&app, &c, "workspaces")?;
    Ok(ok(repos::list_workspaces(&app, &c, page).await?))
}

async fn workspaces_create(
    State(app): State<Shared>,
    c: Caller,
    J(input): J<repos::NewWorkspace>,
) -> R {
    Ok(created(repos::create_workspace(&app, &c, input).await?))
}

async fn workspace_get(State(app): State<Shared>, c: Caller, P(ws): P<String>) -> R {
    Ok(ok(repos::get_workspace(&app, &c, &ws).await?))
}

async fn repos_list(State(app): State<Shared>, c: Caller, q: Q) -> R {
    let page = q.page(&app, &c, "repos")?;
    Ok(ok(repos::list(&app, &c, page).await?))
}

async fn repos_create(State(app): State<Shared>, c: Caller, J(input): J<repos::NewRepo>) -> R {
    Ok(created(repos::create(&app, &c, input).await?))
}

async fn repo_get(State(app): State<Shared>, c: Caller, P(repo): P<String>) -> R {
    Ok(ok(repos::get(&app, &c, &repo).await?))
}

async fn markdown_render(
    State(app): State<Shared>,
    c: Caller,
    P(repo): P<String>,
    J(input): J<domain::markdown::Input>,
) -> R {
    Ok(ok(
        domain::markdown::render(&app, &c, &repo, input.source).await?
    ))
}

async fn repo_patch(
    State(app): State<Shared>,
    c: Caller,
    P(repo): P<String>,
    J(patch): J<repos::RepoPatch>,
) -> R {
    Ok(ok(repos::update(&app, &c, &repo, patch).await?))
}

async fn branch_get(
    State(app): State<Shared>,
    c: Caller,
    P((repo, branch)): P<(String, String)>,
) -> R {
    Ok(ok(repos::branch(&app, &c, &repo, &branch).await?))
}

async fn commit_get(
    State(app): State<Shared>,
    c: Caller,
    P((repo, sha)): P<(String, String)>,
) -> R {
    Ok(ok(repos::commit(&app, &c, &repo, &sha).await?))
}

async fn commit_checks(
    State(app): State<Shared>,
    c: Caller,
    P((repo, sha)): P<(String, String)>,
) -> R {
    Ok(ok(repos::commit_checks(&app, &c, &repo, &sha).await?))
}

async fn compare(State(app): State<Shared>, c: Caller, P(repo): P<String>, q: Q) -> R {
    let base = q
        .get("base")
        .ok_or_else(|| AppError::invalid("base is required."))?;
    let head = q
        .get("head")
        .ok_or_else(|| AppError::invalid("head is required."))?;
    Ok(ok(repos::compare(&app, &c, &repo, &base, &head).await?))
}

// ---------------------------------------------------------------- tickets, the board

async fn tickets_create(
    State(app): State<Shared>,
    c: Caller,
    P(repo): P<String>,
    J(input): J<tickets::NewTicket>,
) -> R {
    Ok(created(
        tickets::create_ticket(&app, &c, &repo, input).await?,
    ))
}

async fn tickets_list(State(app): State<Shared>, c: Caller, P(repo): P<String>, q: Q) -> R {
    let page = q.page(&app, &c, &format!("tickets\n{}", util::fold(&repo)))?;
    let query = tickets::TicketQuery {
        q: q.get("q"),
        state: q.get("state"),
        lane: q.get("lane"),
        assignee: q.get("assignee"),
        label: q.get("label"),
    };
    Ok(ok(
        tickets::list_tickets(&app, &c, &repo, query, page).await?
    ))
}

async fn attachments_create(
    State(app): State<Shared>,
    c: Caller,
    P(repo): P<String>,
    q: Q,
    body: Body,
) -> R {
    // One byte over the limit is enough for the domain to refuse it.
    let image = axum::body::to_bytes(body, attachments::MAX_BYTES + 1)
        .await
        .map_err(|_| AppError::too_large("The image is too large."))?;
    Ok(created(
        attachments::upload(&app, &c, &repo, q.get("name"), image.to_vec()).await?,
    ))
}

async fn attachment_get(
    State(app): State<Shared>,
    c: Caller,
    P((repo, id)): P<(String, String)>,
) -> R {
    let (kind, bytes) = attachments::get(&app, &c, &repo, &id).await?;
    // An attachment never changes, so it never needs asking again.
    Ok((
        [
            (header::CONTENT_TYPE, kind),
            (
                header::CACHE_CONTROL,
                "private, max-age=31536000, immutable".to_string(),
            ),
        ],
        bytes,
    )
        .into_response())
}

async fn ticket_attachments(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    q: Q,
) -> R {
    let page = q.page(
        &app,
        &c,
        &format!("ticket-attachments\n{}\n{key}", util::fold(&repo)),
    )?;
    Ok(ok(
        attachments::of_ticket(&app, &c, &repo, &key, page).await?
    ))
}

async fn ticket_get(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
) -> R {
    Ok(ok(tickets::get_ticket(&app, &c, &repo, &key).await?))
}

async fn ticket_patch(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    J(patch): J<tickets::TicketPatch>,
) -> R {
    Ok(ok(
        tickets::update_ticket(&app, &c, &repo, &key, patch).await?
    ))
}

async fn ticket_delete(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
) -> R {
    tickets::delete_ticket(&app, &c, &repo, &key).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

async fn ticket_pulls(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    q: Q,
) -> R {
    let page = q.page(
        &app,
        &c,
        &format!("ticket-pulls\n{}\n{key}", util::fold(&repo)),
    )?;
    Ok(ok(tickets::ticket_pulls(&app, &c, &repo, &key, page).await?))
}

async fn ticket_comments(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    q: Q,
) -> R {
    let page = q.page(
        &app,
        &c,
        &format!("ticket-comments\n{}\n{key}", util::fold(&repo)),
    )?;
    Ok(ok(
        tickets::list_comments(&app, &c, &repo, &key, page).await?
    ))
}

async fn ticket_comment(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    J(input): J<tickets::NewComment>,
) -> R {
    Ok(created(
        tickets::add_comment(&app, &c, &repo, &key, input).await?,
    ))
}

async fn ticket_comment_patch(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key, id)): P<(String, String, String)>,
    J(input): J<tickets::CommentPatch>,
) -> R {
    Ok(ok(tickets::change_comment(
        &app,
        &c,
        &repo,
        &key,
        &id,
        Some(input.body),
    )
    .await?))
}

async fn ticket_comment_delete(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key, id)): P<(String, String, String)>,
) -> R {
    tickets::change_comment(&app, &c, &repo, &key, &id, None).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

async fn ticket_react(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    J(input): J<tickets::NewReaction>,
) -> R {
    Ok(ok(tickets::react(&app, &c, &repo, &key, input, true).await?))
}

async fn ticket_unreact(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    J(input): J<tickets::NewReaction>,
) -> R {
    tickets::react(&app, &c, &repo, &key, input, false).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

async fn assignee_put(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    J(input): J<tickets::NewAssignee>,
) -> R {
    let name = Some(input.name);
    Ok(ok(tickets::set_assignee(&app, &c, &repo, &key, name).await?))
}

async fn assignee_delete(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
) -> R {
    Ok(ok(tickets::set_assignee(&app, &c, &repo, &key, None).await?))
}

async fn ticket_board(
    State(app): State<Shared>,
    c: Caller,
    P((repo, key)): P<(String, String)>,
    J(input): J<tickets::Placement>,
) -> R {
    Ok(ok(
        tickets::put_on_board(&app, &c, &repo, &key, input).await?
    ))
}

async fn board_cards(State(app): State<Shared>, c: Caller, P(ws): P<String>, q: Q) -> R {
    let page = q.page(&app, &c, &format!("cards\n{}", util::fold(&ws)))?;
    let query = tickets::CardQuery {
        lane: q.get("lane"),
        assignee: q.get("assignee"),
        state: q.get("state"),
        comments: q.get("comments"),
    };
    Ok(ok(tickets::cards(&app, &c, &ws, query, page).await?))
}

async fn mentioned_tickets(State(app): State<Shared>, c: Caller, P(ws): P<String>, q: Q) -> R {
    let page = q.page(&app, &c, &format!("mentioned-tickets\n{}", util::fold(&ws)))?;
    let query = tickets::MentionQuery {
        mentions: q.get("mentions"),
        state: q.get("state"),
        comments: q.get("comments"),
    };
    Ok(ok(tickets::mentioned(&app, &c, &ws, query, page).await?))
}

async fn board_closed(State(app): State<Shared>, c: Caller, P(ws): P<String>, q: Q) -> R {
    let page = q.page(&app, &c, &format!("closed\n{}", util::fold(&ws)))?;
    let query = tickets::ClosedQuery {
        since: q.get("since"),
        until: q.get("until"),
    };
    Ok(ok(tickets::closed(&app, &c, &ws, query, page).await?))
}

// ---------------------------------------------------------------- pull requests

async fn pulls_create(
    State(app): State<Shared>,
    c: Caller,
    P(repo): P<String>,
    J(input): J<pulls::NewPull>,
) -> R {
    let result = pulls::create(&app, &c, &repo, input).await?;
    Ok(if result.created {
        created(result)
    } else {
        ok(result)
    })
}

async fn pulls_list(State(app): State<Shared>, c: Caller, P(repo): P<String>, q: Q) -> R {
    let page = q.page(
        &app,
        &c,
        &format!("pulls-updated-desc\n{}", util::fold(&repo)),
    )?;
    let query = pulls::PullQuery {
        state: q.get("state"),
        head: q.get("head"),
        base: q.get("base"),
    };
    Ok(ok(pulls::list(&app, &c, &repo, query, page).await?))
}

async fn patrol(State(app): State<Shared>, c: Caller, q: Q) -> R {
    let page = q.page(&app, &c, "patrol")?;
    let query = pulls::PatrolQuery {
        involves: q.get("involves"),
        state: q.get("state"),
        draft: q.get("draft"),
    };
    Ok(ok(pulls::patrol(&app, &c, query, page).await?))
}

type RepoNum = P<(String, String)>;

async fn pull_get(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum) -> R {
    Ok(ok(pulls::get(&app, &c, &repo, number(&n)?).await?))
}

async fn pull_patch(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n)): RepoNum,
    J(patch): J<pulls::PullPatch>,
) -> R {
    Ok(ok(pulls::update(&app, &c, &repo, number(&n)?, patch).await?))
}

async fn pull_activity(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum) -> R {
    Ok(ok(pulls::activity(&app, &c, &repo, number(&n)?).await?))
}

async fn pull_comments(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum, q: Q) -> R {
    let page = q.page(
        &app,
        &c,
        &format!("pull-comments\n{}\n{n}", util::fold(&repo)),
    )?;
    Ok(ok(
        pulls::list_comments(&app, &c, &repo, number(&n)?, page).await?
    ))
}

async fn pull_comment(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n)): RepoNum,
    J(input): J<tickets::NewComment>,
) -> R {
    Ok(created(
        pulls::add_comment(&app, &c, &repo, number(&n)?, input).await?,
    ))
}

async fn pull_reviews(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum, q: Q) -> R {
    let page = q.page(&app, &c, &format!("reviews\n{}\n{n}", util::fold(&repo)))?;
    Ok(ok(
        pulls::list_reviews(&app, &c, &repo, number(&n)?, page).await?
    ))
}

async fn pull_review(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n)): RepoNum,
    J(input): J<pulls::NewReview>,
) -> R {
    Ok(created(
        pulls::review(&app, &c, &repo, number(&n)?, input).await?,
    ))
}

async fn pull_inline(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n)): RepoNum,
    J(input): J<pulls::NewInline>,
) -> R {
    Ok(created(
        pulls::inline_comment(&app, &c, &repo, number(&n)?, input).await?,
    ))
}

async fn pull_threads(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum, q: Q) -> R {
    let page = q.page(&app, &c, &format!("threads\n{}\n{n}", util::fold(&repo)))?;
    Ok(ok(
        pulls::list_threads(&app, &c, &repo, number(&n)?, page).await?
    ))
}

type RepoNumId = P<(String, String, String)>;

async fn thread_comments(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n, id)): RepoNumId,
    q: Q,
) -> R {
    let page = q.page(
        &app,
        &c,
        &format!("thread-comments\n{}\n{n}\n{id}", util::fold(&repo)),
    )?;
    Ok(ok(pulls::thread_comments(
        &app,
        &c,
        &repo,
        number(&n)?,
        &id,
        page,
    )
    .await?))
}

async fn thread_reply(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n, id)): RepoNumId,
    J(input): J<pulls::NewReply>,
) -> R {
    Ok(created(
        pulls::reply(&app, &c, &repo, number(&n)?, &id, input).await?,
    ))
}

async fn thread_resolved(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n, id)): RepoNumId,
    J(input): J<pulls::Resolution>,
) -> R {
    Ok(ok(
        pulls::resolve(&app, &c, &repo, number(&n)?, &id, input).await?
    ))
}

async fn pull_react(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n)): RepoNum,
    J(input): J<tickets::NewReaction>,
) -> R {
    Ok(ok(
        pulls::react(&app, &c, &repo, number(&n)?, input, true).await?
    ))
}

async fn pull_unreact(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n)): RepoNum,
    J(input): J<tickets::NewReaction>,
) -> R {
    pulls::react(&app, &c, &repo, number(&n)?, input, false).await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

async fn reviewer_put(State(app): State<Shared>, c: Caller, P((repo, n, name)): RepoNumId) -> R {
    Ok(ok(pulls::set_reviewer(
        &app,
        &c,
        &repo,
        number(&n)?,
        &name,
        true,
    )
    .await?))
}

async fn reviewer_delete(State(app): State<Shared>, c: Caller, P((repo, n, name)): RepoNumId) -> R {
    Ok(ok(pulls::set_reviewer(
        &app,
        &c,
        &repo,
        number(&n)?,
        &name,
        false,
    )
    .await?))
}

async fn pull_files(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum, q: Q) -> R {
    let page = q.page(&app, &c, &format!("files\n{}\n{n}", util::fold(&repo)))?;
    Ok(ok(pulls::files(&app, &c, &repo, number(&n)?, page).await?))
}

async fn pull_commits(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum, q: Q) -> R {
    let page = q.page(&app, &c, &format!("commits\n{}\n{n}", util::fold(&repo)))?;
    Ok(ok(pulls::commits(&app, &c, &repo, number(&n)?, page).await?))
}

async fn pull_checks(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum) -> R {
    Ok(ok(pulls::checks(&app, &c, &repo, number(&n)?).await?))
}

async fn pull_merge(
    State(app): State<Shared>,
    c: Caller,
    P((repo, n)): RepoNum,
    J(input): J<pulls::MergeInput>,
) -> R {
    Ok(ok(pulls::merge(&app, &c, &repo, number(&n)?, input).await?))
}

async fn pull_close(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum) -> R {
    Ok(ok(
        pulls::set_open(&app, &c, &repo, number(&n)?, false).await?
    ))
}

async fn pull_reopen(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum) -> R {
    Ok(ok(
        pulls::set_open(&app, &c, &repo, number(&n)?, true).await?
    ))
}

// ---------------------------------------------------------------- events, diagnostics

async fn events(
    State(app): State<Shared>,
    c: Caller,
) -> AppResult<Sse<impl Stream<Item = Result<Event, Infallible>>>> {
    use crate::events::Notice;
    c.require_agent_or_owner()?;
    use futures_util::StreamExt;
    let stream = app.events.subscribe().map(|notice| {
        Ok(match notice {
            Notice::Change(change) => Event::default()
                .event("change")
                .json_data(&change)
                .unwrap_or_else(|_| Event::default().event("reset").data("{}")),
            Notice::Reset => Event::default().event("reset").data("{}"),
        })
    });
    Ok(Sse::new(stream).keep_alive(KeepAlive::default()))
}

async fn diagnostics(State(app): State<Shared>, c: Caller) -> R {
    c.require_owner()?;
    let (repos, journal) = app
        .store
        .read(|r| {
            Ok((
                r.prefix_keys(store::REPOS, &[])?.len(),
                r.prefix_keys(store::JOURNAL, &[])?.len(),
            ))
        })
        .await?;
    let db_bytes = std::fs::metadata(app.layout.db())
        .map(|m| m.len())
        .unwrap_or(0);
    let cache = app.store.cache_stats();
    let git_version = app
        .git
        .version()
        .await
        .unwrap_or_else(|e| format!("unavailable: {}", e.message));
    Ok(ok(json!({
        "service": {"name": "branvil", "version": env!("CARGO_PKG_VERSION"), "base_url": app.base_url},
        "storage": {
            "engine": "redb",
            "path": app.layout.db(),
            "file_bytes": db_bytes,
            "schema_version": store::SCHEMA_VERSION,
            "cache_size_bytes": app.config.storage.cache_size_mib << 20,
            "cache_used_bytes": cache.used_bytes(),
            "cache_read_hits": cache.read_hits(),
            "cache_read_misses": cache.read_misses(),
            "pending_ref_operations": journal,
            "queues": app.store.stats().snapshot(),
        },
        "git": {"version": git_version, "repositories": repos},
        "runner": app.ci.diagnostics().await,
        "upstream": app.upstream.diagnostics(&app).await,
    })))
}

// ---------------------------------------------------------------- hooks (loopback, hook secret)

fn hook_secret_ok(app: &Shared, headers: &HeaderMap) -> bool {
    let sent = headers
        .get(crate::hooks::SECRET_HEADER)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    bool::from(subtle::ConstantTimeEq::ct_eq(
        sent.as_bytes(),
        app.hook_secret.as_bytes(),
    ))
}

fn hook_answer(ok: bool, message: impl Into<String>) -> Response {
    axum::Json(crate::hooks::HookAnswer {
        ok,
        message: message.into(),
    })
    .into_response()
}

async fn hook_pre_receive(
    State(app): State<Shared>,
    headers: HeaderMap,
    J(input): J<crate::hooks::PreReceive>,
) -> Response {
    if !hook_secret_ok(&app, &headers) {
        return AppError::forbidden("Bad hook secret.").into_response();
    }
    let (repo_id, member) = (input.repo.clone(), input.member.clone());
    let loaded = app
        .store
        .read(move |r| {
            Ok((
                domain::repo_by_id(r, &repo_id)?,
                domain::member_by_name(r, &member)?,
            ))
        })
        .await;
    let (repo, member) = match loaded {
        Ok(x) => x,
        Err(_) => return hook_answer(false, "Unknown repository or member."),
    };
    let existing = match app.git.all_refs(&app.git.repo_dir(&repo.path)).await {
        Ok(r) => r,
        Err(_) => return hook_answer(false, "Cannot read the repository's refs."),
    };
    match crate::refs::check_push(&repo, &member, &input.updates, &existing) {
        Ok(()) => hook_answer(true, ""),
        Err(message) => hook_answer(false, message),
    }
}

async fn hook_post_receive(
    State(app): State<Shared>,
    headers: HeaderMap,
    J(input): J<crate::hooks::PostReceive>,
) -> Response {
    if !hook_secret_ok(&app, &headers) {
        return AppError::forbidden("Bad hook secret.").into_response();
    }
    app.failpoint("post_receive_before_record");
    let repo_id = input.repo.clone();
    let repo = match app
        .store
        .read(move |r| domain::repo_by_id(r, &repo_id))
        .await
    {
        Ok(r) => r,
        Err(_) => return hook_answer(false, "Unknown repository."),
    };
    match crate::refs::record_refs(&app, &repo).await {
        Ok(_) => hook_answer(true, ""),
        Err(e) => hook_answer(false, e.message),
    }
}

// ---------------------------------------------------------------- GUI

#[derive(rust_embed::RustEmbed)]
#[folder = "web/dist"]
#[allow_missing = true]
struct Gui;

async fn gui(method: Method, uri: axum::http::Uri) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return AppError::not_found("No such endpoint.").into_response();
    }
    let path = uri.path().trim_start_matches('/');
    if path.starts_with("api/") || path.starts_with("git/") || path.starts_with("-/hooks/") {
        return AppError::not_found("No such endpoint.").into_response();
    }
    let (file, name) = match Gui::get(path).filter(|_| !path.is_empty()) {
        Some(f) => (f, path.to_string()),
        // Every other path is a page of the single-page GUI (ticket, pull
        // request, commit, and job URLs included).
        None => match Gui::get("index.html") {
            Some(f) => (f, "index.html".to_string()),
            None => {
                return (
                    StatusCode::NOT_FOUND,
                    [(header::CONTENT_TYPE, "text/plain; charset=utf-8")],
                    "The Branvil GUI is not built into this binary (web/dist was empty).\n",
                )
                    .into_response();
            }
        },
    };
    let mime = mime_guess::from_path(&name).first_or_octet_stream();
    let cache = if name == "index.html" {
        "no-cache"
    } else {
        "public, max-age=3600"
    };
    (
        [
            (header::CONTENT_TYPE, mime.essence_str().to_string()),
            (header::CACHE_CONTROL, cache.to_string()),
        ],
        Body::from(file.data.into_owned()),
    )
        .into_response()
}

// ---------------------------------------------------------------- middleware

// Inline styles are allowed for the GUI's component library (Mantine), which
// sets each component's look through `style` attributes, as GuildBotics
// Desktop does; scripts stay limited to the GUI's own files.
const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

/// Host and Origin checks (DNS-rebinding protection) and security headers.
async fn guard(State(app): State<Shared>, req: Request, next: Next) -> Response {
    let path = req.uri().path().to_string();
    if !path.starts_with("/-/hooks/") {
        let host = req
            .headers()
            .get(header::HOST)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_ascii_lowercase();
        if !allowed_host(&app.base_url, &host) {
            return AppError::new(
                StatusCode::MISDIRECTED_REQUEST,
                "misdirected",
                "This Host is not Branvil's.",
            )
            .into_response();
        }
        if let Some(origin) = req.headers().get(header::ORIGIN) {
            let scheme = app.base_url.split("://").next().unwrap_or("http");
            let expected = format!("{scheme}://{host}");
            if origin.to_str().map(|o| o.to_ascii_lowercase()).ok() != Some(expected) {
                return AppError::forbidden("Cross-origin requests are refused.").into_response();
            }
        }
    }
    let mut resp = next.run(req).await;
    let h = resp.headers_mut();
    h.insert(
        "x-content-type-options",
        HeaderValue::from_static("nosniff"),
    );
    h.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    h.insert("x-frame-options", HeaderValue::from_static("DENY"));
    h.insert("content-security-policy", HeaderValue::from_static(CSP));
    resp
}

/// The base URL's host and port, or a loopback name with that port.
pub fn allowed_host(base_url: &str, host: &str) -> bool {
    let (scheme, authority) = base_url.split_once("://").unwrap_or(("http", base_url));
    let default_port = if scheme == "https" { "443" } else { "80" };
    let (base_host, port) = split_host_port(&authority.to_ascii_lowercase(), default_port);
    let (req_host, req_port) = split_host_port(host, default_port);
    req_port == port
        && [base_host.as_str(), "localhost", "127.0.0.1", "[::1]"].contains(&req_host.as_str())
}

/// `host:port` → (host, port); an IPv6 host keeps its brackets.
fn split_host_port(value: &str, default_port: &str) -> (String, String) {
    match value.rsplit_once(':') {
        Some((h, p)) if !p.contains(']') && (!h.contains(':') || h.ends_with(']')) => {
            (h.to_string(), p.to_string())
        }
        _ => (value.to_string(), default_port.to_string()),
    }
}

/// Replays the first response of a repeated `Idempotency-Key` (per member,
/// same body) and refuses the key with another body.
async fn idempotency(State(app): State<Shared>, req: Request, next: Next) -> Response {
    // Session, entry-link, token, and local-app responses are never
    // replayed: they hold a credential or a code, which would otherwise be
    // kept in the database for 24 hours. An attachment's upload is not either:
    // its body is larger than a JSON body may be, and a repeated upload only
    // leaves an attachment no ticket refers to. (Paths here are relative to
    // `/api/v1`.)
    let path = req.uri().path();
    if req.method() != Method::POST
        || path.starts_with("/session")
        || path.starts_with("/apps/")
        || path.ends_with("/tokens")
        || path.ends_with("/markdown")
        || path.ends_with("/attachments")
    {
        return next.run(req).await;
    }
    let Some(key) = req.headers().get("idempotency-key").cloned() else {
        return next.run(req).await;
    };
    let key = match key.to_str() {
        Ok(k) if (1..=128).contains(&k.len()) && k.bytes().all(|b| (0x21..=0x7e).contains(&b)) => {
            k.to_string()
        }
        _ => {
            return AppError::invalid("Idempotency-Key must be 1-128 printable ASCII characters.")
                .into_response();
        }
    };
    let caller = match auth::authenticate(&app, req.method(), req.headers()).await {
        Ok(c) => c,
        Err(e) => return e.into_response(),
    };
    let (mut parts, body) = req.into_parts();
    let limit = app.config.limits.api_body_max_kib << 10;
    let bytes = match axum::body::to_bytes(body, limit).await {
        Ok(b) => b,
        Err(_) => return AppError::too_large("The request body is too large.").into_response(),
    };
    let mut hashed = parts.uri.path().as_bytes().to_vec();
    hashed.push(0);
    hashed.extend_from_slice(&bytes);
    let body_hash = util::hex(&util::sha256(&hashed));
    let member = util::fold(caller.name());
    let record_key = Key::new().s(&member).s(&key).bytes();
    let _serial = app.idempotency.lock(&record_key).await;
    let rk = record_key.clone();
    let previous: Option<IdempotencyRecord> = match app
        .store
        .read(move |r| r.get(store::IDEMPOTENCY, &rk))
        .await
    {
        Ok(p) => p,
        Err(e) => return e.into_response(),
    };
    let cutoff = util::format_time(chrono::Utc::now() - chrono::Duration::hours(24));
    if let Some(prev) = previous.filter(|p| p.created_at >= cutoff) {
        if prev.body_hash != body_hash {
            return AppError::conflict_code(
                "idempotency_conflict",
                "This Idempotency-Key was used with another body.",
            )
            .into_response();
        }
        let status = StatusCode::from_u16(prev.status).unwrap_or(StatusCode::OK);
        return (
            status,
            [(header::CONTENT_TYPE, "application/json")],
            prev.body,
        )
            .into_response();
    }
    parts.extensions.insert(caller);
    let resp = next
        .run(Request::from_parts(parts, Body::from(bytes)))
        .await;
    if resp.status().is_server_error() {
        return resp;
    }
    let (rparts, rbody) = resp.into_parts();
    let rbytes = match axum::body::to_bytes(rbody, 16 << 20).await {
        Ok(b) => b,
        Err(_) => {
            return AppError::internal("response too large to keep for idempotency")
                .into_response();
        }
    };
    let record = IdempotencyRecord {
        body_hash,
        status: rparts.status.as_u16(),
        body: String::from_utf8_lossy(&rbytes).into_owned(),
        created_at: util::now(),
    };
    let stored = app
        .store
        .write(move |w| {
            // Forget keys older than 24 hours.
            let end = Key::new().s(&cutoff).bytes();
            for (k, _) in w.range_raw(
                store::IX_IDEMPOTENCY_TIME,
                std::ops::Bound::Unbounded,
                std::ops::Bound::Excluded(end),
                256,
            )? {
                if let Some(parts) = store::decode_key(&k, "sss") {
                    w.del(
                        store::IDEMPOTENCY,
                        &Key::new().s(parts[1].s()).s(parts[2].s()).bytes(),
                    )?;
                }
                w.del(store::IX_IDEMPOTENCY_TIME, &k)?;
            }
            if let Some(old) = w.get::<IdempotencyRecord>(store::IDEMPOTENCY, &record_key)? {
                crate::model::reindex(
                    w,
                    crate::model::idempotency_index(&member, &key, &old),
                    Vec::new(),
                )?;
            }
            crate::model::put_indexed(
                w,
                store::IDEMPOTENCY,
                &record_key,
                None,
                &record,
                crate::model::idempotency_index(&member, &key, &record),
            )
        })
        .await;
    if let Err(e) = stored {
        tracing::warn!("idempotency record not kept: {e}");
    }
    Response::from_parts(rparts, Body::from(rbytes))
}

pub fn router(app: Shared) -> Router {
    let body_limit = app.config.limits.api_body_max_kib << 10;
    let api = Router::new()
        .route("/health", get(health))
        .route("/me", get(me))
        .route("/session", post(session_create).get(session_get))
        .route("/session/links", post(session_link))
        .route("/members", get(members_list).post(members_create))
        .route("/members/{name}", get(member_get).patch(member_patch))
        .route(
            "/members/{name}/avatar",
            get(avatar_get).put(avatar_put).delete(avatar_delete),
        )
        .route(
            "/members/{name}/tokens",
            get(tokens_list).post(tokens_create),
        )
        .route(
            "/members/{name}/reviewers/{reviewer}",
            put(member_reviewer_put).delete(member_reviewer_delete),
        )
        .route(
            "/members/{name}/tokens/{id}",
            axum::routing::delete(token_delete),
        )
        .route("/apps/authorizations", post(app_authorize))
        .route("/apps/token", post(app_token))
        .route("/workspaces", get(workspaces_list).post(workspaces_create))
        .route("/workspaces/{workspace}", get(workspace_get))
        .route("/workspaces/{workspace}/board/cards", get(board_cards))
        .route("/workspaces/{workspace}/tickets", get(mentioned_tickets))
        .route("/workspaces/{workspace}/board/closed", get(board_closed))
        .route("/repos", get(repos_list).post(repos_create))
        .route("/repos/{repo}", get(repo_get).patch(repo_patch))
        .route("/repos/{repo}/markdown", post(markdown_render))
        .route("/repos/{repo}/branches/{branch}", get(branch_get))
        .route("/repos/{repo}/compare", get(compare))
        .route("/repos/{repo}/commits/{sha}", get(commit_get))
        .route("/repos/{repo}/commits/{sha}/checks", get(commit_checks))
        .route("/repos/{repo}/pulls", get(pulls_list).post(pulls_create))
        .route(
            "/repos/{repo}/pulls/{number}",
            get(pull_get).patch(pull_patch),
        )
        .route("/repos/{repo}/pulls/{number}/activity", get(pull_activity))
        .route(
            "/repos/{repo}/pulls/{number}/comments",
            get(pull_comments).post(pull_comment),
        )
        .route(
            "/repos/{repo}/pulls/{number}/reviews",
            get(pull_reviews).post(pull_review),
        )
        .route(
            "/repos/{repo}/pulls/{number}/inline-comments",
            post(pull_inline),
        )
        .route("/repos/{repo}/pulls/{number}/threads", get(pull_threads))
        .route(
            "/repos/{repo}/pulls/{number}/threads/{id}/comments",
            get(thread_comments),
        )
        .route(
            "/repos/{repo}/pulls/{number}/threads/{id}/replies",
            post(thread_reply),
        )
        .route(
            "/repos/{repo}/pulls/{number}/threads/{id}/resolved",
            put(thread_resolved),
        )
        .route(
            "/repos/{repo}/pulls/{number}/reactions",
            post(pull_react).delete(pull_unreact),
        )
        .route(
            "/repos/{repo}/pulls/{number}/reviewers/{name}",
            put(reviewer_put).delete(reviewer_delete),
        )
        .route("/repos/{repo}/pulls/{number}/files", get(pull_files))
        .route("/repos/{repo}/pulls/{number}/commits", get(pull_commits))
        .route("/repos/{repo}/pulls/{number}/checks", get(pull_checks))
        .route("/repos/{repo}/pulls/{number}/merge", post(pull_merge))
        .route("/repos/{repo}/pulls/{number}/close", post(pull_close))
        .route("/repos/{repo}/pulls/{number}/reopen", post(pull_reopen))
        .route("/pulls", get(patrol))
        .route(
            "/repos/{repo}/tickets",
            get(tickets_list).post(tickets_create),
        )
        .route(
            "/repos/{repo}/tickets/{number}",
            get(ticket_get).patch(ticket_patch).delete(ticket_delete),
        )
        .route("/repos/{repo}/attachments", post(attachments_create))
        .route("/repos/{repo}/attachments/{id}", get(attachment_get))
        .route("/repos/{repo}/tickets/{number}/pulls", get(ticket_pulls))
        .route(
            "/repos/{repo}/tickets/{number}/attachments",
            get(ticket_attachments),
        )
        .route(
            "/repos/{repo}/tickets/{number}/comments",
            get(ticket_comments).post(ticket_comment),
        )
        .route(
            "/repos/{repo}/tickets/{number}/comments/{id}",
            axum::routing::patch(ticket_comment_patch).delete(ticket_comment_delete),
        )
        .route(
            "/repos/{repo}/tickets/{number}/reactions",
            post(ticket_react).delete(ticket_unreact),
        )
        .route(
            "/repos/{repo}/tickets/{number}/assignee",
            put(assignee_put).delete(assignee_delete),
        )
        .route("/repos/{repo}/tickets/{number}/board", put(ticket_board))
        .route("/events", get(events))
        .route("/admin/diagnostics", get(diagnostics))
        .merge(crate::ci::api::routes())
        .merge(crate::ci::admin::routes())
        .route("/admin/backup", post(crate::backup::http_backup))
        .merge(crate::upstream::http::routes())
        .merge(crate::autoland::routes())
        .merge(crate::landing::routes())
        .fallback(|| async { AppError::not_found("No such endpoint.") })
        .layer(axum::extract::DefaultBodyLimit::max(body_limit))
        .layer(middleware::from_fn_with_state(app.clone(), idempotency));
    let hooks = Router::new()
        .route("/pre-receive", post(hook_pre_receive))
        .route("/post-receive", post(hook_post_receive));
    Router::new()
        .nest("/api/v1", api)
        .nest("/-/hooks", hooks)
        .route(
            "/git/{*rest}",
            any(crate::smart_http::handle).layer(axum::extract::DefaultBodyLimit::disable()),
        )
        .fallback(gui)
        .layer(middleware::from_fn_with_state(app.clone(), guard))
        .with_state(app)
}

#[cfg(test)]
mod tests {
    use super::allowed_host;

    #[test]
    fn hosts_are_the_base_url_or_loopback_with_its_port() {
        let base = "http://127.0.0.1:7878";
        assert!(allowed_host(base, "127.0.0.1:7878"));
        assert!(allowed_host(base, "localhost:7878"));
        assert!(allowed_host(base, "[::1]:7878"));
        assert!(!allowed_host(base, "127.0.0.1:7879"));
        assert!(!allowed_host(base, "evil.example:7878"));
        assert!(!allowed_host(base, "127.0.0.1"));
        assert!(!allowed_host(base, ""));
        assert!(allowed_host("http://branvil.test:80", "branvil.test"));
        assert!(allowed_host("http://branvil.test", "branvil.test:80"));
        assert!(allowed_host("http://[::1]:7878", "localhost:7878"));
        assert!(!allowed_host("http://[::1]", "[::1]:7878"));
    }
}
