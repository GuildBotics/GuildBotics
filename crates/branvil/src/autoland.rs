//! Lands each eligible pull independently and notifies the owner once its host checks pass.

use std::collections::BTreeSet;
use std::sync::Mutex;
use std::time::Duration;

use axum::Router;
use axum::extract::State;
use axum::response::{IntoResponse, Response};
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

impl Autoland {
    pub(crate) fn new(webhook_timeout: Duration) -> Self {
        Self {
            client: crate::webhook::client(webhook_timeout),
            problem: Mutex::new(None),
        }
    }

    /// Shares delivery and its diagnostic with CI and landing notices. True
    /// only after delivery; an unset webhook is a successful no-op.
    pub(crate) async fn send(&self, secrets: &crate::vault::Secrets, text: &str) -> bool {
        let sent = match webhook_url(secrets).await {
            Ok(Some(url)) => crate::webhook::send(&self.client, &url, text)
                .await
                .map(|()| true),
            Ok(None) => Ok(false),
            Err(why) => Err(why),
        };
        match sent {
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
            Notice::Change(c)
                if ["pull", "check", "repo", "upstream", "verification"].contains(&c.kind) =>
            {
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
    let affected = || {
        repos
            .iter()
            .filter(|r| dirty.all || dirty.paths.contains(&r.path))
    };
    for repo in affected() {
        if let Err(e) = crate::ci::collection::advance(app, repo).await {
            tracing::warn!("collection verification of {}: {e}", repo.path);
        }
    }
    if let Err(e) = crate::repair::sweep(app).await {
        tracing::warn!("repair tickets: {e}");
    }
    if let Err(e) = crate::ci::notify::sweep(app).await {
        tracing::warn!("repair notifications: {e}");
    }
    // There is one webhook: once a send to it fails, this sweep sends no more.
    let mut webhook_ok = true;
    for repo in affected() {
        if let Err(e) = sweep_repo(app, repo, &mut webhook_ok).await {
            tracing::warn!("autoland: {}: {e}", repo.path);
        }
    }
}

/// An automatic publication the webhook has not been told about.
fn awaits_notice(x: &Landing) -> bool {
    x.notified_at.is_none()
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
                if r2.auto_land
                    && (r2.upstream.is_none()
                        || p.base != r2.default_branch
                        || p.upstream.is_none())
                {
                    candidates.push(p);
                }
            }
            Ok(candidates)
        })
        .await?;
    for p in candidates {
        crate::landing::sweep_repo(app, repo).await?;
        let tips = pulls::tips(app, repo).await?;
        let Some(view) = crate::landing::view(app, repo, p.number, &tips).await? else {
            continue;
        };
        if !view.blockers.is_empty() || view.entry.error.is_some() {
            continue;
        }
        let input = MergeInput {
            expected_head: tips.get(&p.head).cloned().unwrap_or_default(),
            expected_base: tips.get(&p.base).cloned().unwrap_or_default(),
        };
        let attempted_head = input.expected_head.clone();
        let attempted_base = input.expected_base.clone();
        let (done, landed) = match repo
            .upstream
            .as_ref()
            .filter(|_| p.base == repo.default_branch)
        {
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
            let mut notices = vec![];
            if r2.auto_land && r2.upstream.is_some() {
                for p in pulls::open_pulls(r, &r2)? {
                    if p.upstream.as_ref().is_some_and(|x| x.auto)
                        && crate::landing::current(r, &r2.id, p.number)?
                            .as_ref()
                            .is_some_and(awaits_notice)
                    {
                        notices.push(p.number);
                    }
                }
            }
            Ok(notices)
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

/// Tells the webhook, once per head, that pull request `number` can merge
/// on the host: its next action is the owner's merge there.
/// Without a webhook nothing is sent; a failure is kept and tried again.
/// Returns false when the send failed. A crash between the webhook's answer
/// and the write of `notified_at` sends it again: at least once.
async fn notify(app: &App, repo: &Repo, number: u64) -> AppResult<bool> {
    // The setting it decides under stays until the notice is sent, and the
    // poller rewrites the record under the pull request's lock.
    let _setting = app.upstream.setting_lock(&repo.id).await;
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
    // Read again under the locks: the owner may have published it by hand,
    // or turned automatic landing off, since the sweep listed it.
    let Some(landing) = landing
        .filter(awaits_notice)
        .filter(|_| repo.auto_land && p.upstream.as_ref().is_some_and(|x| x.auto))
    else {
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
    let blockers = crate::landing::blockers(
        app,
        &repo,
        &p,
        head,
        tips.get(&p.base).map(String::as_str).unwrap_or_default(),
        &landing,
    )
    .await?;
    let (next, _) = crate::landing::next(&repo, &p, &landing, &following, head, &blockers);
    let Some(up) = repo
        .upstream
        .as_ref()
        .filter(|_| next == "owner_merge_host")
    else {
        return Ok(true);
    };
    let text = format!(
        "{} ({} !{number}) is ready to merge on {}.\n{}\n{}",
        p.title,
        repo.path,
        app.upstream.host_name(&up.provider),
        app.pull_url(&repo.path, number),
        p.upstream
            .as_ref()
            .and_then(|x| x.url.clone())
            .unwrap_or_default(),
    );
    if !app.autoland.send(&app.secrets, &text).await {
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

/// The webhook's URL, or why the vault cannot tell it.
async fn webhook_url(secrets: &crate::vault::Secrets) -> Result<Option<String>, String> {
    secrets
        .get(secrets.webhook())
        .await
        .map_err(|why| format!("cannot read the webhook credential: {why}"))
}

async fn webhook_view(app: &App) -> WebhookView {
    let (url, problem) = match webhook_url(&app.secrets).await {
        Ok(url) => (url, app.autoland.problem.lock().unwrap().clone()),
        Err(why) => (None, Some(why)),
    };
    let host = url.map(|u| {
        reqwest::Url::parse(u.trim())
            .ok()
            .and_then(|u| u.host_str().map(str::to_string))
            .unwrap_or_default()
    });
    WebhookView {
        set: host.is_some(),
        host,
        problem,
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WebhookInput {
    url: String,
}

async fn webhook_get(State(app): State<Shared>, c: Caller) -> AppResult<Response> {
    c.require_owner()?;
    Ok(axum::Json(webhook_view(&app).await).into_response())
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
    app.secrets
        .set(app.secrets.webhook(), url.to_string())
        .await
        .map_err(|why| AppError::unavailable(format!("The webhook cannot be kept: {why}.")))?;
    *app.autoland.problem.lock().unwrap() = None;
    Ok(axum::Json(webhook_view(&app).await).into_response())
}

async fn webhook_delete(State(app): State<Shared>, c: Caller) -> AppResult<Response> {
    c.require_owner()?;
    app.secrets
        .delete(app.secrets.webhook())
        .await
        .map_err(|why| AppError::unavailable(format!("The webhook cannot be removed: {why}.")))?;
    *app.autoland.problem.lock().unwrap() = None;
    Ok(axum::Json(webhook_view(&app).await).into_response())
}

// The webhook's routes, merged into `/api/v1`.
loopback_auth::classified_routes!(pub fn routes() -> Router<Shared>, ROUTES,
    ("/admin/webhook", (get, webhook_get, OwnerSession), (put, webhook_put, OwnerSession), (delete, webhook_delete, OwnerSession)),
);

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn verification_marks_its_repository_dirty() {
        let mut dirty = Dirty::default();
        dirty.add(Notice::Change(crate::events::Change {
            actor: None,
            kind: "verification",
            path: "acme/demo".into(),
            id: "".into(),
        }));
        assert!(dirty.paths.contains("acme/demo"));
    }
}
