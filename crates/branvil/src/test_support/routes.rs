//! Routes no client of the server uses, kept for the tests that drive the
//! service over HTTP: agents reach the same operations in process
//! (`api::MemberApi`). Only the test server mounts them.

use axum::Router;
use axum::extract::State;
use axum::routing::{get, post};

use crate::api::{attachments, members, pulls, tickets};
use crate::app::Shared;
use crate::auth::Caller;
use crate::error::AppError;
use crate::http::{J, P, Q, R, RepoNum, created, number, ok};
use crate::util;

/// The routes, relative to `/api/v1`.
pub(crate) fn routes() -> Router<Shared> {
    Router::new()
        .route("/health", get(health))
        .route("/members/{name}", get(member_get))
        .route("/workspaces/{workspace}/tickets", get(mentioned_tickets))
        .route("/workspaces/{workspace}/board/closed", get(board_closed))
        .route("/repos/{repo}/pulls", post(pulls_create))
        .route("/repos/{repo}/pulls/{number}/comments", get(pull_comments))
        .route("/repos/{repo}/pulls/{number}/reviews", get(pull_reviews))
        .route(
            "/repos/{repo}/pulls/{number}/inline-comments",
            post(pull_inline),
        )
        .route(
            "/repos/{repo}/pulls/{number}/checks/yield",
            post(pull_yield_checks),
        )
        .route("/pulls", get(patrol))
        .route("/repos/{repo}/tickets", get(tickets_list))
        .route(
            "/repos/{repo}/tickets/{number}/attachments",
            get(ticket_attachments),
        )
        .route("/repos/{repo}/upstream/dependency-alerts", get(alerts))
        .route("/repos/{repo}/upstream/dependency-alerts/{id}", get(alert))
}

async fn health() -> axum::response::Response {
    ok(serde_json::json!({"service": "branvil", "version": env!("CARGO_PKG_VERSION"), "api": 1}))
}

async fn member_get(State(app): State<Shared>, c: Caller, P(name): P<String>) -> R {
    Ok(ok(members::get(&app, &c, name).await?))
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

async fn patrol(State(app): State<Shared>, c: Caller, q: Q) -> R {
    let page = q.page(&app, &c, "patrol")?;
    let query = pulls::PatrolQuery {
        involves: q.get("involves"),
        state: q.get("state"),
        draft: q.get("draft"),
    };
    Ok(ok(pulls::patrol(&app, &c, query, page).await?))
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

async fn pull_reviews(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum, q: Q) -> R {
    let page = q.page(&app, &c, &format!("reviews\n{}\n{n}", util::fold(&repo)))?;
    Ok(ok(
        pulls::list_reviews(&app, &c, &repo, number(&n)?, page).await?
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

async fn pull_yield_checks(State(app): State<Shared>, c: Caller, P((repo, n)): RepoNum) -> R {
    Ok(ok(crate::ci::api::yield_pull_checks(
        &app,
        &c,
        &repo,
        number(&n)?,
    )
    .await?))
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

async fn alerts(State(app): State<Shared>, c: Caller, P(repo): P<String>, q: Q) -> R {
    let state = match q.0.get("state").map(String::as_str) {
        None | Some("open") => crate::api::AlertState::Open,
        Some("resolved") => crate::api::AlertState::Resolved,
        Some("dismissed") => crate::api::AlertState::Dismissed,
        Some(_) => return Err(AppError::invalid("state is open, resolved, or dismissed.")),
    };
    let collection = format!("upstream/dependency-alerts\n{}", util::fold(&repo));
    let page = q.page(&app, &c, &collection)?;
    Ok(ok(crate::upstream::ops::dependency_alerts(
        &app, &c, &repo, state, page,
    )
    .await?))
}

async fn alert(State(app): State<Shared>, c: Caller, P((repo, id)): P<(String, String)>) -> R {
    Ok(ok(crate::upstream::ops::dependency_alert(
        &app, &c, &repo, &id,
    )
    .await?))
}
