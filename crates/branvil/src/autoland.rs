//! Advances each repository's landing queues and, with `auto_land`, merges
//! or publishes their passed leaders. Each automatic gate pass is told to
//! the owner's webhook, since people merge upstream pull requests there.

use std::collections::BTreeSet;
use std::sync::Mutex;
use std::time::Duration;

use axum::Router;
use axum::extract::State;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use futures_util::{FutureExt, StreamExt};
use serde::{Deserialize, Serialize};

use crate::app::{App, Shared};
use crate::auth::Caller;
use crate::domain::pulls::{self, MergeInput, load_pull};
use crate::error::{AppError, AppResult};
use crate::events::Notice;
use crate::http::J;
use crate::model::{Landing, Repo};
use crate::store::{self, Rd};
use crate::util;

/// Every repository is looked at again this often, whatever changed.
const TICK: Duration = Duration::from_secs(60);

/// The webhook's client and how its last send went, and why pull requests
/// failed to land (each said once).
pub struct Autoland {
    client: reqwest::Client,
    /// Why the last send to the webhook failed (the next sweep tries again).
    problem: Mutex<Option<String>>,
}

impl Default for Autoland {
    fn default() -> Self {
        Self {
            client: crate::webhook::client(),
            problem: Mutex::new(None),
        }
    }
}

impl Autoland {
    /// Shares delivery and its diagnostic with CI and landing notices.
    pub(crate) async fn send(&self, layout: &crate::config::Layout, text: &str) -> bool {
        match crate::webhook::send(&self.client, layout, text).await {
            Ok(sent) => {
                *self.problem.lock().unwrap() = None;
                sent
            }
            Err(why) => {
                let mut problem = self.problem.lock().unwrap();
                if problem.as_ref() != Some(&why) {
                    tracing::warn!("the webhook failed: {why}");
                }
                *problem = Some(why);
                false
            }
        }
    }
}

/// The repositories a sweep looks at.
#[derive(Default)]
struct Dirty {
    all: bool,
    paths: BTreeSet<String>,
}

impl Dirty {
    fn add(&mut self, notice: Notice) {
        match notice {
            Notice::Reset => self.all = true,
            Notice::Change(c) if ["pull", "check", "repo", "upstream"].contains(&c.kind) => {
                self.paths.insert(c.path);
            }
            Notice::Change(_) => {}
        }
    }
}

/// Starts the one task that lands pull requests: after the changes that can
/// make one landable (it drains what arrived, then sweeps their repositories
/// once), at start, and every `TICK` (every repository). Sweeps never overlap.
/// Ends when the service shuts down. Call once, after startup recovery.
pub fn start(app: Shared) {
    let mut events = Box::pin(app.events.subscribe());
    tokio::spawn(async move {
        let mut tick = tokio::time::interval(TICK);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            let mut dirty = Dirty::default();
            tokio::select! {
                _ = tick.tick() => dirty.all = true,
                notice = events.next() => match notice {
                    Some(n) => dirty.add(n),
                    None => return,
                },
            }
            loop {
                match events.next().now_or_never() {
                    Some(Some(n)) => dirty.add(n),
                    Some(None) => return,
                    None => break,
                }
            }
            sweep(&app, &dirty).await;
        }
    });
}

async fn sweep(app: &App, dirty: &Dirty) {
    let repos = match app
        .store
        .read(|r| r.prefix_all::<Repo>(store::REPOS, &[]))
        .await
    {
        Ok(r) => r,
        Err(e) => return tracing::error!("autoland: {e}"),
    };
    if let Err(e) = crate::ci::notify::sweep(app).await {
        tracing::warn!("CI notifications: {e}");
    }
    // There is one webhook: once a send to it fails, this sweep sends no more.
    let mut webhook_ok = true;
    for repo in repos
        .iter()
        .filter(|r| dirty.all || dirty.paths.contains(&r.path))
    {
        if let Err(e) = sweep_repo(app, repo, &mut webhook_ok).await {
            tracing::warn!("autoland: {}: {e}", repo.path);
        }
    }
}

/// An automatic gate pass the webhook has not been told about.
fn awaits_notice(x: &Landing) -> bool {
    x.state == "passed" && x.notify && x.notified_at.is_none()
}

/// Lands what the repository can when it lands automatically, then tells the
/// webhook of its automatic publishes it was not told of (while `webhook_ok`).
/// `repo` is as the sweep read it: landing reads it again under its lock.
async fn sweep_repo(app: &App, repo: &Repo, webhook_ok: &mut bool) -> AppResult<()> {
    crate::landing::sweep_repo(app, repo).await?;
    let r2 = repo.clone();
    let candidates = app
        .store
        .read(move |r| {
            let mut candidates = Vec::new();
            for p in pulls::open_pulls(r, &r2)? {
                let passed = crate::landing::current(r, &r2.id, p.number)?
                    .is_some_and(|l| l.state == "passed");
                if r2.auto_land && passed && (r2.upstream.is_none() || p.upstream.is_none()) {
                    candidates.push(p);
                }
            }
            Ok(candidates)
        })
        .await?;
    let tips = if candidates.is_empty() {
        Default::default()
    } else {
        pulls::tips(app, repo).await?
    };
    for p in candidates {
        let input = MergeInput {
            expected_head: tips.get(&p.head).cloned().unwrap_or_default(),
            expected_base: tips.get(&p.base).cloned().unwrap_or_default(),
        };
        let attempted_head = input.expected_head.clone();
        let attempted_base = input.expected_base.clone();
        let (done, landed) = match repo.upstream {
            None => (
                "merged",
                pulls::merge_pull(app, repo, p.number, input, true).await,
            ),
            Some(_) => (
                "published",
                crate::upstream::ops::publish_pull(app, repo, p.number, input, true).await,
            ),
        };
        let what = format!("{} !{}", repo.path, p.number);
        let failure = match landed {
            Ok(_) => {
                tracing::info!("autoland: {what} {done}");
                None
            }
            Err(e) if ["head_moved", "base_moved"].contains(&e.code) => continue,
            Err(e) => Some(format!("{}: {}", e.code, e.message)),
        };
        crate::landing::record_auto_result(
            app,
            repo,
            p.number,
            &attempted_head,
            &attempted_base,
            failure,
        )
        .await?;
    }
    // Publishing may have stopped. Reconcile it before offering a merge.
    crate::landing::sweep_repo(app, repo).await?;
    let r2 = repo.clone();
    let notices = app
        .store
        .read(move |r| {
            Ok(crate::landing::active(r, &r2.id)?
                .into_iter()
                .filter(awaits_notice)
                .map(|l| l.number)
                .collect::<Vec<_>>())
        })
        .await?;
    for number in notices {
        if !*webhook_ok {
            break;
        }
        *webhook_ok = notify(app, repo, number).await?;
    }
    Ok(())
}

// ---------------------------------------------------------------- the webhook

/// Tells the webhook, once per gate pass, that pull request `number` can merge
/// on the host: its queue's next action is the owner's merge there.
/// Without a webhook nothing is sent; a failure is kept and tried again.
/// Returns false when the send failed. A crash between the webhook's answer
/// and the write of `notified_at` sends it again: at least once.
async fn notify(app: &App, repo: &Repo, number: u64) -> AppResult<bool> {
    // The poller rewrites the record under the same lock.
    let _guard = app.upstream.pull_lock(&repo.id, number).await;
    let id = repo.id.clone();
    let (p, landing, repo) = app
        .store
        .read(move |r| {
            let repo = crate::domain::repo_by_id(r, &id)?;
            Ok((
                load_pull(r, &repo, number)?,
                crate::landing::current(r, &id, number)?,
                repo,
            ))
        })
        .await?;
    let Some(landing) = landing.filter(awaits_notice) else {
        return Ok(true);
    };
    // A statement written since the sweep's reconcile takes it out of the
    // queue at the next sweep: it is not ready to merge.
    if crate::landing::awaits_answer(app, &repo, &p).await? {
        return Ok(true);
    }
    // Only once it is the owner's turn: the head is on the host and the
    // host's checks of it have finished.
    let tips = pulls::tips(app, &repo).await?;
    let head = tips.get(&p.head).map(String::as_str).unwrap_or_default();
    let following = app.upstream.following(&repo.id);
    let first = crate::landing::first(&repo, &p, &landing, &following, head);
    let Some(up) = repo
        .upstream
        .as_ref()
        .filter(|_| first.next == "owner_merge_host")
    else {
        return Ok(true);
    };
    let text = format!(
        "{} ({} !{number}) passed its landing gate and is ready to merge on {}.\n{}\n{}",
        p.title,
        repo.path,
        app.upstream.host_name(&up.provider),
        app.pull_url(&repo.path, number),
        first.host_url.unwrap_or_default(),
    );
    if !app.autoland.send(&app.layout, &text).await {
        return Ok(false);
    }
    app.store
        .write(move |w| crate::landing::mark_notified(w, &landing, &util::now()))
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    Ok(true)
}

/// The webhook as the API shows it: never its URL, which is a credential of
/// another system.
#[derive(Serialize)]
struct WebhookView {
    set: bool,
    /// The host of its URL.
    host: Option<String>,
    /// Why the last send failed.
    problem: Option<String>,
}

fn webhook_view(app: &App) -> WebhookView {
    let host = std::fs::read_to_string(app.layout.webhook()).ok().map(|u| {
        reqwest::Url::parse(u.trim())
            .ok()
            .and_then(|u| u.host_str().map(str::to_string))
            .unwrap_or_default()
    });
    WebhookView {
        set: host.is_some(),
        host,
        problem: app.autoland.problem.lock().unwrap().clone(),
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WebhookInput {
    url: String,
}

async fn webhook_get(State(app): State<Shared>, c: Caller) -> AppResult<Response> {
    c.require_owner()?;
    Ok(axum::Json(webhook_view(&app)).into_response())
}

async fn webhook_put(
    State(app): State<Shared>,
    c: Caller,
    J(input): J<WebhookInput>,
) -> AppResult<Response> {
    c.require_owner()?;
    let url = reqwest::Url::parse(input.url.trim())
        .ok()
        .filter(|u| ["http", "https"].contains(&u.scheme()) && u.host_str().is_some())
        .ok_or_else(|| AppError::invalid("The webhook URL must be an http or https URL."))?;
    crate::config::write_secret(&app.layout.webhook(), url.as_str().as_bytes())
        .map_err(AppError::internal)?;
    *app.autoland.problem.lock().unwrap() = None;
    Ok(axum::Json(webhook_view(&app)).into_response())
}

async fn webhook_delete(State(app): State<Shared>, c: Caller) -> AppResult<Response> {
    c.require_owner()?;
    let _ = std::fs::remove_file(app.layout.webhook());
    *app.autoland.problem.lock().unwrap() = None;
    Ok(axum::Json(webhook_view(&app)).into_response())
}

/// The webhook's routes, merged into `/api/v1`.
pub fn routes() -> Router<Shared> {
    Router::new().route(
        "/admin/webhook",
        get(webhook_get).put(webhook_put).delete(webhook_delete),
    )
}
