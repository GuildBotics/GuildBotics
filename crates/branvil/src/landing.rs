//! Persistent, serial landing queues, one for each repository and base branch.

use axum::{
    Router,
    extract::{Path, State},
    routing::get,
};
use serde::Serialize;

use crate::app::{App, Shared};
use crate::auth::Caller;
use crate::ci::Check;
use crate::domain::{self, pulls};
use crate::error::AppResult;
use crate::git::GateCommit;
use crate::model::{Landing, LandingReason, Published, Pull, Repo};
use crate::store::{self, Key, Rd, Wtx};
use crate::util;

pub fn current(r: &impl Rd, repo: &str, number: u64) -> AppResult<Option<Landing>> {
    r.get(store::LANDING, &Pull::key(repo, number))
}

fn all(r: &impl Rd, repo: &str) -> AppResult<Vec<Landing>> {
    let mut entries: Vec<Landing> = r.prefix_all(store::LANDING, &Key::new().s(repo).bytes())?;
    entries.sort_by_key(|e| (e.state == "left", e.sequence));
    Ok(entries)
}

pub fn active(r: &impl Rd, repo: &str) -> AppResult<Vec<Landing>> {
    let mut entries = Vec::new();
    for (_, n) in r.prefix_raw(
        store::IX_OPEN_PULLS,
        &Key::new().s(repo).bytes(),
        None,
        usize::MAX,
    )? {
        let n = String::from_utf8_lossy(&n)
            .parse()
            .map_err(crate::error::AppError::internal)?;
        if let Some(e) = current(r, repo, n)?.filter(|e| e.state != "left") {
            entries.push(e);
        }
    }
    entries.sort_by_key(|e| e.sequence);
    Ok(entries)
}

#[derive(Serialize, Clone, Debug)]
pub struct LandingFirst {
    pub number: u64,
    pub state: String,
    pub state_at: String,
    pub error: Option<String>,
    pub next: String,
    pub host_url: Option<String>,
    pub next_detail: Option<String>,
}

#[derive(Serialize, Clone, Debug)]
pub struct LandingView {
    #[serde(flatten)]
    pub entry: Landing,
    pub title: String,
    pub pull_state: String,
    pub draft: bool,
    pub position: Option<u64>,
    pub first: Option<LandingFirst>,
}

pub(crate) fn first(
    repo: &Repo,
    p: &Pull,
    e: &Landing,
    following: &crate::upstream::Following,
    head: &str,
) -> LandingFirst {
    let x = published(repo, p);
    let detail = x.and_then(|x| x.error.clone());
    let (next, next_detail) = if e.state != "passed" {
        ("gate", None)
    } else if repo.upstream.is_none() {
        if repo.auto_land && e.error.is_some() {
            ("owner_fix_auto", e.error.clone())
        } else {
            (
                if repo.auto_land {
                    "auto_merge"
                } else {
                    "owner_merge"
                },
                None,
            )
        }
    } else if x.is_some_and(|x| x.state == "merged") {
        match following
            .stopped
            .clone()
            .or_else(|| following.error.clone())
        {
            Some(why) => ("owner_fix_following", Some(why)),
            None => ("following", None),
        }
    } else if e.resume_publish
        || p.upstream.is_some() && x.is_none()
        || x.is_some_and(|x| x.stopped.is_some())
    {
        (
            "owner_publish",
            x.and_then(|x| x.stopped.clone()).or(detail),
        )
    } else if x.is_some_and(|x| x.state == "closed") {
        ("host_closed", detail)
    } else if let Some(x) = x.filter(|x| x.number.is_some() && x.pushed.as_deref() == Some(head)) {
        (
            if x.checks_passed() {
                "owner_merge_host"
            } else {
                "host_checks"
            },
            x.error.clone(),
        )
    } else if x.is_some() {
        ("publishing", detail)
    } else if repo.auto_land && e.error.is_some() {
        ("owner_fix_auto", e.error.clone())
    } else if repo.auto_land && p.upstream.is_none() {
        ("publishing", detail)
    } else {
        ("owner_publish", detail)
    };
    LandingFirst {
        number: e.number,
        state: e.state.clone(),
        state_at: e.state_at.clone(),
        error: e.error.clone(),
        next: next.into(),
        host_url: x.and_then(|x| x.url.clone()),
        next_detail,
    }
}

fn views(
    r: &impl Rd,
    repo: &Repo,
    following: &crate::upstream::Following,
    tips: &pulls::Tips,
) -> AppResult<Vec<LandingView>> {
    let entries = all(r, &repo.id)?;
    let mut queues = std::collections::BTreeMap::new();
    for entry in entries.iter().filter(|e| e.state != "left") {
        if !queues.contains_key(&entry.base) {
            let p = pulls::load_pull(r, repo, entry.number)?;
            queues.insert(
                entry.base.clone(),
                first(
                    repo,
                    &p,
                    entry,
                    following,
                    tips.get(&p.head).map(String::as_str).unwrap_or_default(),
                ),
            );
        }
    }
    let mut positions = std::collections::BTreeMap::new();
    entries
        .into_iter()
        .map(|entry| {
            let p = pulls::load_pull(r, repo, entry.number)?;
            let position = (entry.state != "left").then(|| {
                let next = positions.entry(entry.base.clone()).or_insert(0);
                *next += 1;
                *next
            });
            Ok(LandingView {
                title: p.title,
                pull_state: p.state,
                draft: p.draft,
                first: position.and_then(|_| queues.get(&entry.base).cloned()),
                entry,
                position,
            })
        })
        .collect()
}

pub async fn view(
    app: &App,
    repo: &Repo,
    number: u64,
    tips: &pulls::Tips,
) -> AppResult<Option<LandingView>> {
    let following = app.upstream.following(&repo.id);
    let repo = repo.clone();
    let tips = tips.clone();
    app.store
        .read(move |r| {
            let Some(entry) = current(r, &repo.id, number)? else {
                return Ok(None);
            };
            let queue: Vec<_> = active(r, &repo.id)?
                .into_iter()
                .filter(|e| e.base == entry.base)
                .collect();
            let position = queue
                .iter()
                .position(|e| e.number == number)
                .map(|i| i as u64 + 1);
            let first = match (position, queue.first()) {
                (Some(_), Some(e)) => {
                    let p = pulls::load_pull(r, &repo, e.number)?;
                    Some(first(
                        &repo,
                        &p,
                        e,
                        &following,
                        tips.get(&p.head).map(String::as_str).unwrap_or_default(),
                    ))
                }
                _ => None,
            };
            let p = pulls::load_pull(r, &repo, number)?;
            Ok(Some(LandingView {
                entry,
                position,
                first,
                title: p.title,
                pull_state: p.state,
                draft: p.draft,
            }))
        })
        .await
}

async fn queue_get(
    State(app): State<Shared>,
    c: Caller,
    Path(path): Path<String>,
) -> AppResult<axum::Json<serde_json::Value>> {
    let repo = domain::repos::visible(&app, &c, &path).await?;
    let following = app.upstream.following(&repo.id);
    let tips = pulls::tips(&app, &repo).await?;
    let items = app
        .store
        .read(move |r| views(r, &repo, &following, &tips))
        .await?;
    Ok(axum::Json(serde_json::json!({"items":items})))
}

pub fn routes() -> Router<Shared> {
    Router::new().route("/repos/{repo}/landing", get(queue_get))
}

pub fn mark_notified(w: &Wtx, expected: &Landing, at: &str) -> AppResult<()> {
    if let Some(mut e) = current(w, &expected.repo, expected.number)?
        && e.state == "passed"
        && e.gate == expected.gate
        && e.state_at == expected.state_at
    {
        e.notified_at = Some(at.to_string());
        w.put(store::LANDING, &Pull::key(&e.repo, e.number), &e)?;
    }
    Ok(())
}

fn reason(code: &str, message: impl Into<String>) -> LandingReason {
    LandingReason {
        code: code.into(),
        message: message.into(),
        jobs: Vec::new(),
        files: Vec::new(),
    }
}

async fn eligibility(
    app: &App,
    repo: &Repo,
    p: &Pull,
    head: &str,
    base: &str,
) -> AppResult<Option<LandingReason>> {
    if p.state != "open" {
        return Ok(Some(reason("closed", "The pull request is closed.")));
    }
    if p.draft {
        return Ok(Some(reason("draft", "The pull request is a draft.")));
    }
    if head.is_empty()
        || base.is_empty()
        || app
            .git
            .is_ancestor(&app.git.repo_dir(&repo.path), head, base)
            .await?
    {
        return Ok(Some(reason(
            "empty_head",
            "The head adds nothing to the base.",
        )));
    }
    if let Some(up) = &repo.upstream {
        let author = p.author.clone();
        let provider = up.provider.clone();
        let account = app
            .store
            .read(move |r| {
                Ok(domain::member_by_name(r, &author)?
                    .accounts
                    .iter()
                    .any(|a| a.provider == provider))
            })
            .await?;
        if p.base != repo.default_branch || !account {
            return Ok(Some(reason(
                "publish_requirements",
                "Publishing requires the default base and an author account on the host.",
            )));
        }
    }
    if head_checks(repo, &app.ci.head_checks(repo, head).await?.checks) != HeadChecks::Passed {
        return Ok(Some(reason(
            "head_checks",
            "Required head checks have not succeeded.",
        )));
    }
    if pulls::approvals(&pulls::read_review_states(app, p, head).await?) < repo.required_approvals {
        return Ok(Some(reason("approvals", "The head needs more approvals.")));
    }
    if awaits_answer(app, repo, p).await? {
        return Ok(Some(reason(
            "unanswered",
            "A statement awaits the author's answer.",
        )));
    }
    Ok(None)
}

/// Whether a statement on `p` awaits its author's answer (`pulls::unanswered`).
pub(crate) async fn awaits_answer(app: &App, repo: &Repo, p: &Pull) -> AppResult<bool> {
    let (p, url) = (p.clone(), app.pull_url(&repo.path, p.number));
    Ok(!app
        .store
        .read(move |r| pulls::unanswered(r, &p, &url))
        .await?
        .is_empty())
}

pub(crate) fn published<'a>(repo: &Repo, p: &'a Pull) -> Option<&'a Published> {
    p.upstream.as_ref().filter(|x| {
        repo.upstream
            .as_ref()
            .is_some_and(|up| up.same_place(&x.provider, &x.repository))
    })
}

fn published_problem(repo: &Repo, p: &Pull, head: &str, resume: bool) -> Option<LandingReason> {
    let x = published(repo, p)?;
    if !resume && let Some(stopped) = &x.stopped {
        return Some(reason("publish_stopped", stopped.clone()));
    }
    if x.pushed.as_deref() == Some(head) {
        let failed: Vec<_> = x
            .checks
            .iter()
            .filter(|c| c.status == "completed" && !c.succeeded())
            .map(|c| c.name.clone())
            .collect();
        if !failed.is_empty() {
            let mut why = reason("host_failed", "The published head failed host checks.");
            why.jobs = failed;
            return Some(why);
        }
    }
    None
}

/// Where the repository's required checks on a head stand.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum HeadChecks {
    /// Every one succeeded, or was skipped by its paths.
    Passed,
    /// None failed, and one is not registered yet or still runs.
    Running,
    /// One completed without succeeding.
    Failed,
}

pub(crate) fn head_checks(repo: &Repo, checks: &[Check]) -> HeadChecks {
    let required = || {
        repo.required_checks
            .iter()
            .map(|name| checks.iter().find(|c| c.name == *name))
    };
    if required().any(|c| c.is_some_and(|c| c.status == "completed" && !c.head_satisfied())) {
        HeadChecks::Failed
    } else if required().any(|c| c.is_none_or(|c| c.status != "completed")) {
        HeadChecks::Running
    } else {
        HeadChecks::Passed
    }
}

/// Whether `e` left the queue for a failure that still stands at `p`'s head
/// and base: a conflict, required gate jobs not retried to success, or host
/// checks not re-run to success. Until it is cleared the pull request does not
/// rejoin.
pub(crate) async fn failure_stands(
    app: &App,
    repo: &Repo,
    p: &Pull,
    e: &Landing,
    head: &str,
    base: &str,
) -> AppResult<bool> {
    if e.state != "left" || e.head_sha != head || e.base != p.base || e.base_sha != base {
        return Ok(false);
    }
    let Some(why) = &e.reason else {
        return Ok(false);
    };
    Ok(match why.code.as_str() {
        "conflict" => true,
        "gate_failed" => match &e.gate {
            None => true,
            Some(gate) => {
                let checks = app.ci.head_checks(repo, gate).await?;
                !repo
                    .required_checks
                    .iter()
                    .all(|n| checks.checks.iter().any(|c| c.name == *n && c.succeeded()))
            }
        },
        "host_failed" => published(repo, p).is_some_and(|x| {
            x.pushed.as_deref() != Some(head)
                || !why
                    .jobs
                    .iter()
                    .all(|name| x.checks.iter().any(|c| &c.name == name && c.succeeded()))
        }),
        _ => false,
    })
}

async fn save(app: &App, repo: &Repo, e: Landing, comment: Option<String>) -> AppResult<()> {
    let id = repo.id.clone();
    let r2 = repo.clone();
    let number = e.number;
    app.store
        .write(move |w| {
            w.put(store::LANDING, &Pull::key(&id, number), &e)?;
            if let Some(body) = comment {
                pulls::system_comment(w, &r2, number, body)?;
            }
            Ok(())
        })
        .await?;
    app.events.publish("pull", &repo.path, number.to_string());
    Ok(())
}

/// Persist automatic failures only for the gate that was actually attempted.
/// Identical failures do not emit another event (nor reset the wait clock).
pub async fn record_auto_result(
    app: &App,
    repo: &Repo,
    number: u64,
    head: &str,
    base: &str,
    error: Option<String>,
) -> AppResult<()> {
    let (r, head, base) = (repo.clone(), head.to_string(), base.to_string());
    app.locked(&repo.id, move |app| async move {
        let id = r.id.clone();
        if let Some(mut e) = app.store.read(move |r| current(r, &id, number)).await?
            && e.state == "passed"
            && e.head_sha == head
            && e.base_sha == base
            && e.error != error
        {
            e.error = error;
            e.state_at = util::now();
            save(&app, &r, e, None).await?;
        }
        Ok(())
    })
    .await
}

pub async fn remove_merged_ref(app: &App, repo: &Repo, number: u64) -> AppResult<()> {
    for kind in ["gate", "failed-gate"] {
        app.git
            .run_ok(
                Some(&app.git.repo_dir(&repo.path)),
                &["update-ref", "-d", &format!("refs/branvil/{kind}/{number}")],
            )
            .await?;
    }
    Ok(())
}

pub fn mark_closed(w: &Wtx, repo: &str, number: u64) -> AppResult<()> {
    if let Some(mut e) = current(w, repo, number)? {
        e.state = "left".into();
        e.state_at = util::now();
        e.reason = Some(reason("closed", "The pull request is closed."));
        e.resume_publish = false;
        w.put(store::LANDING, &Pull::key(repo, number), &e)?;
    }
    Ok(())
}

pub fn mark_merged(w: &Wtx, repo: &str, number: u64) -> AppResult<()> {
    if let Some(mut e) = current(w, repo, number)? {
        e.state = "left".into();
        e.state_at = util::now();
        e.reason = Some(reason("merged", "The pull request was merged."));
        w.put(store::LANDING, &Pull::key(repo, number), &e)?;
    }
    Ok(())
}

async fn leave(
    app: &App,
    repo: &Repo,
    mut e: Landing,
    why: LandingReason,
    checks: &[Check],
) -> AppResult<()> {
    let failed = || {
        checks
            .iter()
            .filter(|c| why.jobs.contains(&c.name))
            .map(|c| {
                format!(
                    "- {}: {}",
                    c.name,
                    c.details_url.as_deref().unwrap_or("no log available")
                )
            })
            .collect::<Vec<_>>()
            .join("\n")
    };
    let comment = match why.code.as_str() {
        "gate_failed" | "conflict" => Some(format!(
            "Landing gate failed.\n\nBase: `{}`\nHead: `{}`\nGate: `{}`\n\n{}\n{}{}",
            e.base_sha,
            e.head_sha,
            e.gate.as_deref().unwrap_or("not created"),
            why.message,
            if why.code == "conflict" {
                why.files.join("\n")
            } else {
                failed()
            },
            if why.code == "gate_failed" {
                "\n\nRetry the failed and cancelled required gate jobs. Once all required jobs succeed and no statement awaits the author's answer, this pull request rejoins the end of the landing queue."
            } else {
                ""
            }
        )),
        "host_failed" => Some(format!(
            "Checks on {host} failed.\n\nHead: `{}`\n\n{}\n{}\n\nPush a head that fixes them. If a failure was intermittent, the owner can re-run it on {host}: once the failed checks succeed and no statement awaits the author's answer, this pull request rejoins the end of the landing queue.",
            e.head_sha,
            why.message,
            failed(),
            host = repo
                .upstream
                .as_ref()
                .map(|up| app.upstream.host_name(&up.provider))
                .unwrap_or_default()
        )),
        _ => None,
    };
    e.state = "left".into();
    e.state_at = util::now();
    e.reason = Some(why);
    save(app, repo, e, comment).await?;
    repair_refs_locked(app, repo).await?;
    Ok(())
}

/// Owner intent to replace a stopped publication is distinct from successfully
/// publishing it: the old upstream state remains until its gate passes.
pub async fn resume_publish(
    app: &App,
    repo: &Repo,
    number: u64,
    input: pulls::MergeInput,
) -> AppResult<()> {
    let r = repo.clone();
    app.locked(&repo.id, move |app| async move {
        resume_publish_locked(&app, &r, number, &input).await
    })
    .await
}

async fn resume_publish_locked(
    app: &App,
    repo: &Repo,
    number: u64,
    input: &pulls::MergeInput,
) -> AppResult<()> {
    let id = repo.id.clone();
    let r2 = repo.clone();
    let (p, mut e) = app
        .store
        .read(move |r| Ok((pulls::load_pull(r, &r2, number)?, current(r, &id, number)?)))
        .await?;
    let tips = pulls::tips(app, repo).await?;
    let (head, base) = (
        tips.get(&p.head).cloned().unwrap_or_default(),
        tips.get(&p.base).cloned().unwrap_or_default(),
    );
    if head != input.expected_head {
        return Err(crate::error::AppError::conflict_code(
            "head_moved",
            "The pull request head moved.",
        ));
    }
    if base != input.expected_base {
        return Err(crate::error::AppError::conflict_code(
            "base_moved",
            "The pull request base moved.",
        ));
    }
    if p.upstream.as_ref().is_none_or(|x| x.stopped.is_none()) {
        return Err(crate::error::AppError::conflict_code(
            "not_mergeable",
            "The publication is not stopped.",
        ));
    }
    if let Some(why) = eligibility(app, repo, &p, &head, &base).await? {
        return Err(crate::error::AppError::conflict_code(
            "not_mergeable",
            why.message,
        ));
    }
    let mut entry = match e.take() {
        Some(entry) => entry,
        None => {
            let id = repo.id.clone();
            let sequence = app
                .store
                .write(move |w| w.next(&format!("landing:{id}")))
                .await?;
            let now = util::now();
            Landing {
                repo: repo.id.clone(),
                number,
                base: p.base.clone(),
                queued_at: now.clone(),
                sequence,
                state: "waiting".into(),
                state_at: now,
                gate: None,
                base_sha: base,
                head_sha: head,
                reason: None,
                notified_at: None,
                notify: false,
                resume_publish: false,
                error: None,
            }
        }
    };
    entry.resume_publish = true;
    save(app, repo, entry, None).await?;
    reconcile_locked(app, repo).await
}

pub fn clear_resume_publish(w: &Wtx, repo: &str, number: u64) -> AppResult<()> {
    if let Some(mut e) = current(w, repo, number)?
        && e.resume_publish
    {
        e.resume_publish = false;
        w.put(store::LANDING, &Pull::key(repo, number), &e)?;
    }
    Ok(())
}

pub fn publication_changed(w: &Wtx, repo: &str, number: u64) -> AppResult<()> {
    if let Some(mut e) = current(w, repo, number)?
        && e.state == "passed"
    {
        e.state_at = util::now();
        w.put(store::LANDING, &Pull::key(repo, number), &e)?;
    }
    Ok(())
}

/// The upstream has just confirmed this pull request is still open. A base
/// containing its entire tree therefore came from another integration; it must
/// no longer block other pulls while waiting for a merge that did not happen.
/// For an operation (`App::finish`): the upstream's poll.
pub async fn host_observed_open(app: &App, repo: &Repo, number: u64) -> AppResult<()> {
    let _guard = app.lock_repo(&repo.id).await;
    let id = repo.id.clone();
    let Some(mut e) = app.store.read(move |r| current(r, &id, number)).await? else {
        return Ok(());
    };
    if e.state != "passed" {
        return Ok(());
    }
    let tips = pulls::tips(app, repo).await?;
    let Some(base) = tips.get(&e.base) else {
        return Ok(());
    };
    if base == &e.base_sha {
        return Ok(());
    }
    let dir = app.git.repo_dir(&repo.path);
    if let Ok(tree) = app.git.merge_tree(&dir, base, &e.head_sha).await?
        && app.git.tree_oid(&dir, base).await?.as_ref() == Some(&tree)
    {
        e.base_sha = base.clone();
        leave(
            app,
            repo,
            e,
            reason(
                "already_in_base",
                "The base already contains these changes and the host pull request is still open.",
            ),
            &[],
        )
        .await?;
    }
    Ok(())
}

/// Reconciles every queue, including repositories whose automatic landing is off.
pub async fn sweep_repo(app: &App, repo: &Repo) -> AppResult<()> {
    let r = repo.clone();
    app.locked(&repo.id, move |app| async move {
        reconcile_locked(&app, &r).await
    })
    .await
}

/// Caller holds the repository lock. Persist gate ownership before updating its
/// ref, so CI recovery can safely recover either side of an interrupted write.
pub async fn reconcile_locked(app: &App, repo: &Repo) -> AppResult<()> {
    let id = repo.id.clone();
    let current_repo = app.store.read(move |r| domain::repo_by_id(r, &id)).await?;
    let repo = &current_repo;
    let tips = pulls::tips(app, repo).await?;
    let r2 = repo.clone();
    let pulls = app.store.read(move |r| pulls::open_pulls(r, &r2)).await?;
    for p in &pulls {
        let (head, base) = (
            tips.get(&p.head).cloned().unwrap_or_default(),
            tips.get(&p.base).cloned().unwrap_or_default(),
        );
        let id = repo.id.clone();
        let number = p.number;
        let old = app.store.read(move |r| current(r, &id, number)).await?;
        // A host merge may already have advanced the base before the poller
        // records mark_merged. Keep the passed gate until that record arrives.
        if let Some(e) = &old
            && p.state == "open"
            && !p.draft
            && !p.merged
            && published(repo, p).is_some_and(|x| {
                x.number.is_some()
                    && x.pushed.as_deref() == Some(head.as_str())
                    && x.stopped.is_none()
            })
            && e.state == "passed"
            && e.head_sha == head
            && e.base_sha != base
            && !base.is_empty()
            && let Ok(tree) = app
                .git
                .merge_tree(&app.git.repo_dir(&repo.path), &base, &head)
                .await?
            && tree
                == app
                    .git
                    .tree_oid(&app.git.repo_dir(&repo.path), &base)
                    .await?
                    .unwrap_or_default()
        {
            continue;
        }
        let ineligible = eligibility(app, repo, p, &head, &base).await?;
        let identity_changed = old
            .as_ref()
            .is_some_and(|e| e.head_sha != head || e.base != p.base);
        let base_changed = old.as_ref().is_some_and(|e| e.base_sha != base);
        let changed = identity_changed || base_changed;
        let problem = published_problem(
            repo,
            p,
            &head,
            old.as_ref().is_some_and(|e| e.resume_publish),
        )
        .or(ineligible);
        if let Some(e) = old.as_ref().filter(|e| e.state != "left") {
            if identity_changed || problem.is_some() {
                leave(
                    app,
                    repo,
                    e.clone(),
                    problem.clone().unwrap_or_else(|| {
                        reason(
                            if e.head_sha != head {
                                "head_moved"
                            } else {
                                "base_moved"
                            },
                            "The head or base moved.",
                        )
                    }),
                    &published(repo, p).map_or_else(Vec::new, |x| {
                        x.checks
                            .iter()
                            .map(|c| Check {
                                name: c.name.clone(),
                                details_url: c.url.clone(),
                                ..Check::default()
                            })
                            .collect()
                    }),
                )
                .await?;
            } else {
                if base_changed {
                    // Admission belongs to the reviewed head, not to a gate's
                    // base snapshot. Rebuild in place, including a retry gate
                    // which was waiting behind another pull request.
                    let mut e = e.clone();
                    e.base_sha = base.clone();
                    e.gate = None;
                    if e.state != "waiting" {
                        e.state_at = util::now();
                    }
                    e.state = "waiting".into();
                    e.notified_at = None;
                    e.notify = false;
                    e.error = None;
                    save(app, repo, e, None).await?;
                }
                continue;
            }
        }
        if problem.is_some() {
            continue;
        }
        if old.as_ref().is_some_and(|e| {
            e.state == "left"
                && !changed
                && e.reason
                    .as_ref()
                    .is_some_and(|r| r.code == "already_in_base")
        }) {
            continue;
        }
        if let Some(e) = &old
            && failure_stands(app, repo, p, e, &head, &base).await?
        {
            continue;
        }
        let now = util::now();
        let id = repo.id.clone();
        let sequence = app
            .store
            .write(move |w| w.next(&format!("landing:{id}")))
            .await?;
        save(
            app,
            repo,
            Landing {
                repo: repo.id.clone(),
                number: p.number,
                base: p.base.clone(),
                queued_at: now.clone(),
                sequence,
                state: "waiting".into(),
                state_at: now,
                gate: old
                    .as_ref()
                    .filter(|e| {
                        e.state == "left"
                            && !changed
                            && e.reason.as_ref().is_some_and(|r| r.code == "gate_failed")
                    })
                    .and_then(|e| e.gate.clone()),
                base_sha: base,
                head_sha: head,
                reason: None,
                notified_at: None,
                notify: false,
                resume_publish: old.as_ref().is_some_and(|e| e.resume_publish),
                error: None,
            },
            None,
        )
        .await?;
    }
    repair_refs_locked(app, repo).await?;
    let id = repo.id.clone();
    let entries = app.store.read(move |r| active(r, &id)).await?;
    let mut bases = std::collections::BTreeSet::new();
    for mut e in entries {
        if !bases.insert(e.base.clone()) {
            continue;
        }
        if e.state == "waiting" {
            let gate = match &e.gate {
                Some(sha) => GateCommit::Ready {
                    sha: sha.clone(),
                    tree: String::new(),
                },
                None => {
                    app.git
                        .gate_commit(
                            &app.git.repo_dir(&repo.path),
                            &e.base_sha,
                            &e.head_sha,
                            e.number,
                            !app.ci.needs_gate_commit(repo, &e.head_sha).await?,
                        )
                        .await?
                }
            };
            match gate {
                GateCommit::Conflicts { files } => {
                    let mut why = reason("conflict", "The head conflicts with the base.");
                    why.files = files;
                    leave(app, repo, e, why, &[]).await?;
                    continue;
                }
                GateCommit::Ready { sha, tree } => {
                    if sha != e.head_sha
                        && !tree.is_empty()
                        && app
                            .git
                            .tree_oid(&app.git.repo_dir(&repo.path), &e.base_sha)
                            .await?
                            .as_ref()
                            == Some(&tree)
                    {
                        leave(
                            app,
                            repo,
                            e,
                            reason(
                                "already_in_base",
                                "The base already contains these changes.",
                            ),
                            &[],
                        )
                        .await?;
                        continue;
                    }
                    e.gate = Some(sha);
                    e.state = "gating".into();
                    e.state_at = util::now();
                    save(app, repo, e.clone(), None).await?;
                    app.failpoint("gate_before_update_ref");
                    repair_refs_locked(app, repo).await?;
                    app.failpoint("gate_after_update_ref");
                }
            }
        }
        if !["gating", "passed"].contains(&e.state.as_str()) {
            continue;
        }
        let checks = app.ci.head_checks(repo, e.gate.as_deref().unwrap()).await?;
        let failed: Vec<_> = checks
            .checks
            .iter()
            .filter(|c| {
                repo.required_checks.contains(&c.name) && c.status == "completed" && !c.succeeded()
            })
            .map(|c| c.name.clone())
            .collect();
        if !failed.is_empty() {
            let mut why = reason("gate_failed", "Required landing gate jobs failed.");
            why.jobs = failed;
            leave(app, repo, e, why, &checks.checks).await?;
        } else if repo
            .required_checks
            .iter()
            .all(|n| checks.checks.iter().any(|c| &c.name == n && c.succeeded()))
        {
            if e.state != "passed" {
                e.state = "passed".into();
                e.state_at = util::now();
                e.notify = repo.auto_land && repo.upstream.is_some();
                save(app, repo, e, None).await?;
            }
        } else if e.state == "passed" {
            e.state = "gating".into();
            e.state_at = util::now();
            e.notify = false;
            e.notified_at = None;
            e.error = None;
            save(app, repo, e, None).await?;
        }
    }
    Ok(())
}

/// Restores only refs implied by committed queue entries. Startup calls this
/// before accepting requests, so even an immediate backup includes their objects.
/// The caller holds the repository lock; no eligibility or new gates are made.
pub async fn repair_refs_locked(app: &App, repo: &Repo) -> AppResult<Vec<crate::refs::RefChange>> {
    let r2 = repo.clone();
    let entries = app
        .store
        .read(move |r| {
            let mut entries = Vec::new();
            for p in pulls::open_pulls(r, &r2)? {
                if let Some(e) = current(r, &r2.id, p.number)? {
                    entries.push((p, e));
                }
            }
            Ok(entries)
        })
        .await?;
    let dir = app.git.repo_dir(&repo.path);
    let actual = app.git.all_refs(&dir).await?;
    let tips: std::collections::BTreeMap<_, _> = actual
        .iter()
        .filter_map(|(name, sha)| name.strip_prefix("refs/heads/").map(|b| (b, sha)))
        .collect();
    let mut expected = std::collections::BTreeMap::new();
    for (p, e) in &entries {
        let Some(gate) = &e.gate else { continue };
        let kind = if ["gating", "passed"].contains(&e.state.as_str()) {
            if gate == &e.head_sha {
                continue;
            }
            "gate"
        } else if (e.state == "waiting"
            || e.reason
                .as_ref()
                .is_some_and(|r| e.state == "left" && r.code == "gate_failed"))
            && !p.draft
            && e.base == p.base
            && tips.get(p.head.as_str()).copied() == Some(&e.head_sha)
            && tips.get(p.base.as_str()).copied() == Some(&e.base_sha)
        {
            "failed-gate"
        } else {
            continue;
        };
        expected.insert(format!("refs/branvil/{kind}/{}", e.number), gate);
    }
    // Make the next owner reachable before deleting the previous ref. This
    // also repairs a crash between saving a failed/retried gate and its refs.
    for (name, sha) in &expected {
        if actual.get(name) != Some(*sha) {
            app.git
                .run_ok(Some(&dir), &["update-ref", name, sha])
                .await?;
        }
    }
    for name in actual.keys().filter(|name| {
        name.starts_with("refs/branvil/gate/") || name.starts_with("refs/branvil/failed-gate/")
    }) {
        if !expected.contains_key(name) {
            app.git
                .run_ok(Some(&dir), &["update-ref", "-d", name])
                .await?;
        }
    }
    crate::refs::record_refs_locked(app, repo).await
}

/// Gate checks, including retries after departure, belong only to their head.
/// The caller supplies the same head it uses for the checks response.
pub async fn checks(app: &App, repo: &Repo, number: u64, head: &str) -> AppResult<Vec<Check>> {
    let id = repo.id.clone();
    let Some(e) = app.store.read(move |r| current(r, &id, number)).await? else {
        return Ok(Vec::new());
    };
    if e.head_sha != head {
        return Ok(Vec::new());
    }
    if let Some(why) = e.reason.as_ref().filter(|r| r.code == "conflict") {
        return Ok(vec![Check {
            name: "gate".into(),
            status: "completed".into(),
            conclusion: Some("failure".into()),
            details: Some(format!("{}\n{}", why.message, why.files.join("\n"))),
            ..Check::default()
        }]);
    }
    let Some(gate) = e.gate else {
        return Ok(Vec::new());
    };
    let mut checks = app.ci.head_checks(repo, &gate).await?.checks;
    for c in &mut checks {
        c.name = format!("gate/{}", c.name);
    }
    Ok(checks)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{HostCheck, Reader, Upstream};
    use crate::upstream::Following;
    use serde_json::json;

    fn fixture() -> (Repo, Pull, Landing) {
        let repo = serde_json::from_value(json!({
            "id":"r", "path":"acme/demo", "workspace":"w", "default_branch":"main", "protected_branches":[],
            "required_checks":[], "required_approvals":0, "auto_land":true, "ci_network":false, "labels":[],
            "upstream":null, "revision":1, "created_at":"now"
        })).unwrap();
        let pull = serde_json::from_value(json!({
            "repo":"r", "number":1, "title":"Feature", "body":"", "state":"open", "merged":false, "draft":false,
            "author":"test-agent", "head":"feature", "base":"main", "reviewers":[], "tickets":[], "labels":[],
            "reviewed_by":[], "ready_at":null, "created_at":"now", "updated_at":"now", "closed_at":null,
            "merged_at":null, "merge_sha":null, "merged_head":null, "merged_base":null, "upstream":null, "revision":1
        })).unwrap();
        let entry = serde_json::from_value(json!({
            "repo":"r", "number":1, "base":"main", "queued_at":"now", "sequence":1, "state":"passed", "state_at":"now",
            "gate":"head", "base_sha":"base", "head_sha":"head", "reason":null, "notified_at":null,
            "notify":false, "resume_publish":false, "error":null
        })).unwrap();
        (repo, pull, entry)
    }

    fn publication() -> Published {
        serde_json::from_value(json!({
            "provider":"test-host", "repository":"remote/demo", "account":"test-agent", "number":2,
            "url":"https://host.test/pulls/2", "state":"open", "pushed":"head", "sent_title":"Feature", "sent_body":"",
            "sent_state":"open", "checks":[], "checks_registered_by":null, "checks_settled":true, "published_at":"now", "error":null, "stopped":null, "auto":true
        })).unwrap()
    }

    #[tokio::test]
    async fn gate_checks_follow_the_requested_head_in_every_state_and_retry() {
        use crate::ci::{pipeline::JobDef, queue};
        let temp = tempfile::tempdir().unwrap();
        let config = crate::config::Config::load(None, Some(temp.path()), None, None).unwrap();
        config.layout().create().unwrap();
        let store = store::Store::open(&config.layout().db(), 1 << 20, 1).unwrap();
        let app = App::new(
            config,
            "http://127.0.0.1:7878".into(),
            store,
            vec![],
            "http://127.0.0.1:7878".into(),
        );
        let (repo, _, mut entry) = fixture();
        entry.head_sha = "old-head".into();
        entry.gate = Some("old-gate".into());
        let register = |sha: &str| {
            app.store
                .write_sync(|w| {
                    queue::register(
                        w,
                        queue::Registration {
                            selection: None,
                            repo: repo.id.clone(),
                            repo_path: repo.path.clone(),
                            sha: sha.into(),
                            tree: None,
                            branch: format!("gate/{}", entry.number),
                            protected: false,
                            pipeline: queue::PipelineState::Jobs {
                                jobs: vec![JobDef {
                                    paths: Vec::new(),
                                    name: "test".into(),
                                    image: "alpine".into(),
                                    commands: vec!["true".into()],
                                    timeout_secs: None,
                                    artifacts: vec![],
                                    cache: None,
                                }],
                            },
                            network: false,
                        },
                        10,
                        &util::now(),
                    )?;
                    Ok(())
                })
                .unwrap();
        };
        register("old-gate");
        let failed_id = app
            .store
            .write_sync(|w| {
                let now = util::now();
                let c = queue::claim(w, "runner1", 60, &now)?.unwrap();
                queue::finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "failure",
                    None,
                    queue::ResultInfo::default(),
                    10,
                    &now,
                )?;
                Ok(c.attempt.id)
            })
            .unwrap();
        for state in ["waiting", "gating", "passed", "left"] {
            entry.state = state.into();
            entry.reason = (state == "left").then(|| reason("gate_failed", "Gate failed."));
            app.store
                .write_sync(|w| w.put(store::LANDING, &Pull::key(&repo.id, entry.number), &entry))
                .unwrap();
            let current = checks(&app, &repo, entry.number, "old-head").await.unwrap();
            assert_eq!(current.len(), 1, "{state}");
            assert_eq!(current[0].name, "gate/test");
            assert_eq!(current[0].conclusion.as_deref(), Some("failure"));
            for head in ["new-head", ""] {
                assert!(
                    checks(&app, &repo, entry.number, head)
                        .await
                        .unwrap()
                        .is_empty(),
                    "{state}: {head}"
                );
            }
        }
        // A departed entry keeps the newest retry of its own head visible.
        let retry = app
            .store
            .write_sync(|w| queue::retry(w, &failed_id, 10, &util::now()))
            .unwrap();
        let current = checks(&app, &repo, entry.number, "old-head").await.unwrap();
        assert_eq!(current[0].attempt_id.as_deref(), Some(retry.id.as_str()));
        assert_eq!(current[0].status, "queued");
        assert!(
            checks(&app, &repo, entry.number, "new-head")
                .await
                .unwrap()
                .is_empty()
        );
        // A new admission exposes only its new gate's jobs.
        register("new-gate");
        entry.head_sha = "new-head".into();
        entry.gate = Some("new-gate".into());
        entry.state = "gating".into();
        entry.reason = None;
        app.store
            .write_sync(|w| w.put(store::LANDING, &Pull::key(&repo.id, entry.number), &entry))
            .unwrap();
        let current = checks(&app, &repo, entry.number, "new-head").await.unwrap();
        assert_eq!(current.len(), 1);
        assert_eq!(current[0].name, "gate/test");
        assert_ne!(current[0].attempt_id.as_deref(), Some(retry.id.as_str()));
        assert!(
            checks(&app, &repo, entry.number, "old-head")
                .await
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn next_follows_the_actual_publish_and_merge_decisions() {
        let (mut repo, mut p, mut e) = fixture();
        let mut following = Following::default();
        let next = |r: &Repo, p: &Pull, e: &Landing, f: &Following| first(r, p, e, f, "head").next;
        assert_eq!(next(&repo, &p, &e, &following), "auto_merge");
        repo.auto_land = false;
        assert_eq!(next(&repo, &p, &e, &following), "owner_merge");
        repo.upstream = Some(Upstream {
            provider: "test-host".into(),
            repository: "remote/demo".into(),
            reader: Reader {
                member: "reader".into(),
                chosen: false,
            },
        });
        assert_eq!(next(&repo, &p, &e, &following), "owner_publish");
        repo.auto_land = true;
        assert_eq!(next(&repo, &p, &e, &following), "publishing");
        for state in ["waiting", "gating"] {
            e.state = state.into();
            assert_eq!(next(&repo, &p, &e, &following), "gate");
        }
        e.state = "passed".into();
        e.error = Some("no_account: restore the credential".into());
        assert_eq!(next(&repo, &p, &e, &following), "owner_fix_auto");
        assert!(
            first(&repo, &p, &e, &following, "head")
                .next_detail
                .unwrap()
                .contains("no_account")
        );
        e.error = None;
        p.upstream = Some(publication());
        assert_eq!(next(&repo, &p, &e, &following), "owner_merge_host"); // the host has none
        let done = HostCheck {
            id: "1".into(),
            name: "host/push".into(),
            status: "completed".into(),
            conclusion: Some("success".into()),
            url: None,
            summary: None,
        };
        let x = p.upstream.as_mut().unwrap();
        x.await_checks(60);
        x.record_checks((vec![done.clone()], true));
        // Some checks are not all there are while more may be registered.
        assert_eq!(next(&repo, &p, &e, &following), "host_checks");
        let x = p.upstream.as_mut().unwrap();
        x.await_checks(0);
        x.record_checks((vec![done.clone()], false)); // a job still to start
        assert_eq!(next(&repo, &p, &e, &following), "host_checks");
        let x = p.upstream.as_mut().unwrap();
        x.record_checks((vec![done], true));
        assert_eq!(next(&repo, &p, &e, &following), "owner_merge_host");
        let x = p.upstream.as_mut().unwrap();
        x.await_checks(0);
        x.record_checks((Vec::new(), true)); // none, after the time given
        assert_eq!(next(&repo, &p, &e, &following), "owner_merge_host");
        assert_eq!(
            first(&repo, &p, &e, &following, "head").host_url.as_deref(),
            Some("https://host.test/pulls/2")
        );
        assert_eq!(
            first(&repo, &p, &e, &following, "new-head").next,
            "publishing"
        );
        for conclusion in ["success", "neutral", "skipped"] {
            p.upstream.as_mut().unwrap().record_checks((
                vec![HostCheck {
                    id: "1".into(),
                    name: "host/test".into(),
                    status: "completed".into(),
                    conclusion: Some(conclusion.into()),
                    url: None,
                    summary: None,
                }],
                true,
            ));
            assert_eq!(next(&repo, &p, &e, &following), "owner_merge_host");
        }
        p.upstream.as_mut().unwrap().checks[0].status = "queued".into();
        assert_eq!(next(&repo, &p, &e, &following), "host_checks");
        // A failure is not the owner's merge, even before it makes the entry leave.
        let x = p.upstream.as_mut().unwrap();
        (x.checks[0].status, x.checks[0].conclusion) = ("completed".into(), Some("failure".into()));
        assert_eq!(next(&repo, &p, &e, &following), "host_checks");
        p.upstream.as_mut().unwrap().state = "closed".into();
        assert_eq!(
            first(&repo, &p, &e, &following, "new-head").next,
            "host_closed"
        );
        p.upstream.as_mut().unwrap().state = "merged".into();
        assert_eq!(next(&repo, &p, &e, &following), "following");
        for stopped in [false, true] {
            following = Following::default();
            if stopped {
                following.stopped = Some("diverged".into());
            } else {
                following.error = Some("cannot fetch".into());
            }
            let first = first(&repo, &p, &e, &following, "head");
            assert_eq!(first.next, "owner_fix_following");
            assert_eq!(
                first.next_detail,
                following.stopped.clone().or(following.error.clone())
            );
        }
        following = Following::default();
        p.upstream = Some(publication());
        p.upstream.as_mut().unwrap().number = None;
        p.upstream.as_mut().unwrap().error = Some("opening PR failed; poll retries".into());
        e.error = Some("earlier automatic create failed".into());
        assert_eq!(next(&repo, &p, &e, &following), "publishing");
        p.upstream.as_mut().unwrap().stopped = Some("host refused".into());
        assert_eq!(next(&repo, &p, &e, &following), "owner_publish");
        p.upstream.as_mut().unwrap().stopped = None;
        e.resume_publish = true;
        assert_eq!(next(&repo, &p, &e, &following), "owner_publish");
        e.resume_publish = false;
        p.upstream.as_mut().unwrap().repository = "previous/upstream".into();
        assert_eq!(next(&repo, &p, &e, &following), "owner_publish");
    }

    #[test]
    fn next_details_belong_to_the_actor_that_can_still_make_progress() {
        let (mut repo, mut p, mut e) = fixture();
        let mut following = Following::default();
        e.error = Some("no_account: an earlier automatic attempt failed".into());
        let auto_failure = e.error.clone();
        for upstream in [false, true] {
            repo.upstream = upstream.then(|| Upstream {
                provider: "test-host".into(),
                repository: "remote/demo".into(),
                reader: Reader {
                    member: "reader".into(),
                    chosen: false,
                },
            });
            repo.auto_land = true;
            let f = first(&repo, &p, &e, &following, "head");
            assert_eq!(f.next, "owner_fix_auto");
            assert_eq!(f.next_detail, auto_failure);
            repo.auto_land = false;
            let f = first(&repo, &p, &e, &following, "head");
            assert_eq!(
                f.next,
                if upstream {
                    "owner_publish"
                } else {
                    "owner_merge"
                }
            );
            assert_eq!(f.next_detail, None);
        }

        for auto in [false, true] {
            repo.auto_land = auto;
            // Every publication path has taken ownership away from autoland,
            // including records for a previous upstream and manual resends.
            for (state, numbered, current_head, current_upstream, stopped, resume, expected) in [
                ("open", false, true, true, false, false, "publishing"),
                ("open", true, false, true, false, false, "publishing"),
                ("open", true, true, true, false, false, "owner_merge_host"),
                ("closed", true, true, true, false, false, "host_closed"),
                ("merged", true, true, true, false, false, "following"),
                ("open", true, true, false, false, false, "owner_publish"),
                ("open", true, true, true, true, false, "owner_publish"),
                ("open", true, true, true, false, true, "owner_publish"),
            ] {
                let mut x = publication();
                x.state = state.into();
                x.number = numbered.then_some(2);
                x.pushed = Some(
                    if current_head {
                        "head"
                    } else {
                        "previous-head"
                    }
                    .into(),
                );
                if !current_upstream {
                    x.repository = "previous/upstream".into();
                }
                x.stopped = stopped.then(|| "host sending stopped".into());
                p.upstream = Some(x);
                e.resume_publish = resume;
                for host_error in [None, Some("current host error".to_string())] {
                    p.upstream.as_mut().unwrap().error = host_error.clone();
                    let f = first(&repo, &p, &e, &following, "head");
                    assert_eq!(f.next, expected);
                    let detail = if !current_upstream || state == "merged" {
                        None
                    } else if stopped {
                        Some("host sending stopped".into())
                    } else {
                        host_error
                    };
                    assert_eq!(f.next_detail, detail, "{auto} {expected}");
                }
            }
        }
        e.resume_publish = false;
        p.upstream = Some(publication());
        p.upstream.as_mut().unwrap().checks = vec![HostCheck {
            id: "host-1".into(),
            name: "host/test".into(),
            status: "queued".into(),
            conclusion: None,
            url: None,
            summary: None,
        }];
        let f = first(&repo, &p, &e, &following, "head");
        assert_eq!(f.next, "host_checks");
        assert_eq!(f.next_detail, None);
        p.upstream.as_mut().unwrap().state = "merged".into();
        following.stopped = Some("following stopped".into());
        let f = first(&repo, &p, &e, &following, "head");
        assert_eq!(f.next, "owner_fix_following");
        assert_eq!(f.next_detail, following.stopped);
    }
}
