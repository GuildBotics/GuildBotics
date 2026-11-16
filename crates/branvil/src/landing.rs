//! Per-pull landing conditions and notifications, independent of other pulls.
use crate::app::App;
use crate::ci::Check;
use crate::domain::{self, pulls};
use crate::error::AppResult;
use crate::model::{Landing, LandingReason, Published, Pull, Repo};
use crate::store::{self, Rd, Wtx};
use serde::Serialize;

pub fn current(r: &impl Rd, repo: &str, number: u64) -> AppResult<Option<Landing>> {
    r.get(store::LANDING, &Pull::key(repo, number))
}

#[derive(Serialize, Clone, Debug)]
pub struct LandingView {
    #[serde(flatten)]
    pub entry: Landing,
    pub blockers: Vec<LandingReason>,
    pub next: String,
    pub host_url: Option<String>,
    pub next_detail: Option<String>,
}

fn reason(code: &str, message: impl Into<String>) -> LandingReason {
    LandingReason {
        count: None,
        required: None,
        held: None,
        code: code.into(),
        message: message.into(),
        jobs: vec![],
        files: vec![],
    }
}

/// Store-only predicate shared by landing and CI admission in their transaction.
pub fn held(r: &impl Rd, repo: &Repo, p: &Pull) -> AppResult<Option<crate::model::Held>> {
    if !repo.hold_while_failing
        || p.base != repo.default_branch
        || crate::ci::verify::get(r, &repo.id)?.is_none_or(|v| v.failing_since.is_none())
        || crate::repair::is_pull(r, p)?
    {
        return Ok(None);
    }
    Ok(
        crate::repair::open_ticket(r, &repo.id)?.map(|t| crate::model::Held {
            number: t.number,
            assignee: t.assignee.map(|a| a.name),
        }),
    )
}

pub(crate) fn next(
    repo: &Repo,
    p: &Pull,
    e: &Landing,
    following: &crate::upstream::Following,
    head: &str,
    blockers: &[LandingReason],
) -> (String, Option<String>) {
    let x = published(repo, p);
    let detail = x.and_then(|x| x.error.clone());
    let (next, detail) = if x.is_some_and(|x| x.state == "merged") {
        match following
            .stopped
            .clone()
            .or_else(|| following.error.clone())
        {
            Some(why) => ("owner_fix_following", Some(why)),
            None => ("following", None),
        }
    } else if x.is_some_and(|x| x.state == "closed") {
        ("host_closed", detail)
    } else if !blockers.is_empty() {
        ("conditions", None)
    } else if repo.upstream.is_some()
        && (!repo.auto_land && p.upstream.is_none()
            || p.upstream.is_some() && x.is_none()
            || x.is_some_and(|x| x.stopped.is_some()))
    {
        (
            "owner_publish",
            x.and_then(|x| x.stopped.clone()).or(detail),
        )
    } else if repo.auto_land
        && e.error.is_some()
        && (repo.upstream.is_none() || p.upstream.is_none())
    {
        ("owner_fix_auto", e.error.clone())
    } else if repo.upstream.is_none() {
        (
            if repo.auto_land {
                "auto_merge"
            } else {
                "owner_merge"
            },
            None,
        )
    } else if let Some(x) = x.filter(|x| x.number.is_some() && x.pushed.as_deref() == Some(head)) {
        (
            if x.checks_passed() {
                "owner_merge_host"
            } else {
                "host_checks"
            },
            detail,
        )
    } else {
        ("publishing", detail)
    };
    (next.into(), detail)
}

pub async fn view(
    app: &App,
    repo: &Repo,
    number: u64,
    tips: &pulls::Tips,
) -> AppResult<Option<LandingView>> {
    let r2 = repo.clone();
    let p = app
        .store
        .read(move |r| pulls::load_pull(r, &r2, number))
        .await?;
    if p.state != "open" {
        return Ok(None);
    }
    let head = tips.get(&p.head).map(String::as_str).unwrap_or_default();
    let base = tips.get(&p.base).map(String::as_str).unwrap_or_default();
    let id = repo.id.clone();
    let entry = app
        .store
        .read(move |r| current(r, &id, number))
        .await?
        .unwrap_or_else(|| blank(repo, number));
    let blockers = blockers(app, repo, &p, head, base, &entry).await?;
    let (next, next_detail) = next(
        repo,
        &p,
        &entry,
        &app.upstream.following(&repo.id),
        head,
        &blockers,
    );
    Ok(Some(LandingView {
        entry,
        blockers,
        next,
        next_detail,
        host_url: published(repo, &p).and_then(|x| x.url.clone()),
    }))
}

pub async fn blockers(
    app: &App,
    repo: &Repo,
    p: &Pull,
    head: &str,
    base: &str,
    e: &Landing,
) -> AppResult<Vec<LandingReason>> {
    let hold = if repo.hold_while_failing {
        let (r2, p2) = (repo.clone(), p.clone());
        app.store.read(move |r| held(r, &r2, &p2)).await?
    } else {
        None
    };
    let mut out = vec![];
    if let Some(hold) = hold {
        let mut why = reason(
            "held",
            format!(
                "Repair #{}; assignee: {}.",
                hold.number,
                hold.assignee.as_deref().unwrap_or("unassigned")
            ),
        );
        why.held = Some(hold);
        out.push(why);
    }
    if p.draft {
        out.push(reason("draft", "The pull request is a draft."));
    }
    if e.head_sha != head || e.base_sha != base {
        out.push(reason(
            "checking",
            "The current head and base are being checked.",
        ));
    } else if !e.conflicts.is_empty() {
        let mut why = reason("conflict", "The head conflicts with the base.");
        why.files = e.conflicts.clone();
        out.push(why);
    } else if head.is_empty()
        || base.is_empty()
        || app
            .git
            .merge_tree(&app.git.repo_dir(&repo.path), base, head)
            .await?
            .ok()
            .as_ref()
            == app
                .git
                .tree_oid(&app.git.repo_dir(&repo.path), base)
                .await?
                .as_ref()
    {
        out.push(reason(
            "already_in_base",
            "The head adds nothing to the base.",
        ));
    }
    if let Some(up) = &repo.upstream {
        let (author, provider) = (p.author.clone(), up.provider.clone());
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
            out.push(reason(
                "publish_requirements",
                "Publishing requires the default base and an author account on the host.",
            ));
        }
    }
    let (check_state, jobs) = if head.is_empty() {
        (HeadChecks::Running, repo.required_checks.clone())
    } else {
        pull_head_check_details(app, repo, p, head).await?
    };
    if head.is_empty() || check_state != HeadChecks::Passed {
        let mut why = reason(
            "head_checks",
            format!(
                "Required head checks have not succeeded: {}.",
                repo.required_checks.join(", ")
            ),
        );
        why.jobs = jobs;
        out.push(why);
    }
    let approved = pulls::approvals(&pulls::read_review_states(app, p, head).await?);
    if approved < repo.required_approvals {
        let mut why = reason(
            "approvals",
            format!(
                "Approvals of {head}: {approved} / {} required.",
                repo.required_approvals
            ),
        );
        why.count = Some(approved);
        why.required = Some(repo.required_approvals);
        out.push(why);
    }
    if awaits_answer(app, repo, p).await? {
        out.push(reason(
            "unanswered",
            "A statement awaits the author's answer.",
        ));
    }
    if let Some(x) = published(repo, p).filter(|x| x.pushed.as_deref() == Some(head)) {
        let jobs: Vec<_> = x
            .checks
            .iter()
            .filter(|c| c.status == "completed" && !c.succeeded())
            .map(|c| c.name.clone())
            .collect();
        if !jobs.is_empty() {
            let mut why = reason("host_failed", "The published head failed host checks.");
            why.jobs = jobs;
            out.push(why);
        }
    }
    Ok(out)
}

fn blank(repo: &Repo, number: u64) -> Landing {
    Landing {
        repo: repo.id.clone(),
        number,
        head_sha: String::new(),
        base_sha: String::new(),
        conflicts: vec![],
        told: vec![],
        notified_at: None,
        error: None,
    }
}

/// Refreshes the pair while holding the repository lock. A new head starts a new notice lifetime.
pub async fn refresh_locked(
    app: &App,
    repo: &Repo,
    p: &Pull,
    head: &str,
    base: &str,
    force: bool,
) -> AppResult<Landing> {
    let (id, number) = (repo.id.clone(), p.number);
    let old = app.store.read(move |r| current(r, &id, number)).await?;
    let mut e = old.clone().unwrap_or_else(|| blank(repo, number));
    if e.head_sha != head {
        e.told.clear();
        e.notified_at = None;
        e.error = None;
    }
    if force || e.head_sha != head || e.base_sha != base {
        e.conflicts = if head.is_empty() || base.is_empty() {
            vec![]
        } else {
            app.git
                .conflicts(&app.git.repo_dir(&repo.path), base, head)
                .await?
        };
        e.head_sha = head.into();
        e.base_sha = base.into();
    }
    if old.as_ref() != Some(&e) {
        save(app, repo, e.clone(), None).await?;
    }
    Ok(e)
}

pub async fn sweep_repo(app: &App, repo: &Repo) -> AppResult<()> {
    let r2 = repo.clone();
    app.locked(&repo.id, move |app| async move {
        let id = r2.id.clone();
        let repo = app.store.read(move |r| domain::repo_by_id(r, &id)).await?;
        let r2 = repo.clone();
        let pulls = app.store.read(move |r| pulls::open_pulls(r, &r2)).await?;
        let tips = pulls::tips(&app, &repo).await?;
        for p in pulls.into_iter().filter(|p| !p.draft) {
            let head = tips.get(&p.head).map(String::as_str).unwrap_or_default();
            let base = tips.get(&p.base).map(String::as_str).unwrap_or_default();
            let mut e = refresh_locked(&app, &repo, &p, head, base, false).await?;
            if !e.conflicts.is_empty() && !e.told.iter().any(|s| s == "conflict") {
                let body = format!("The head conflicts with the base.\n\nBase: `{}` (`{base}`)\nHead: `{head}`\n\n{}\n\nMerge `{}` into the head, resolve the conflicts, and push.", p.base, e.conflicts.join("\n"), p.base);
                e.told.push("conflict".into());
                save(&app, &repo, e.clone(), Some(body)).await?;
            }
            if let Some(x) = published(&repo, &p).filter(|x| x.pushed.as_deref() == Some(head)) {
                let failed: Vec<_> = x.checks.iter().filter(|c| c.status == "completed" && !c.succeeded() && !e.told.contains(&format!("host:{}", c.name))).collect();
                if !failed.is_empty() {
                    let body = format!("Checks on {} failed.\n\nHead: `{head}`\n\n{}\n\nPush a head that fixes them. If a failure was intermittent, the owner can re-run it on the host.", app.upstream.host_name(&x.provider), failed.iter().map(|c| format!("- {}: {}", c.name, c.url.as_deref().unwrap_or("no log available"))).collect::<Vec<_>>().join("\n"));
                    e.told.extend(failed.iter().map(|c| format!("host:{}", c.name)));
                    save(&app, &repo, e, Some(body)).await?;
                }
            }
        }
        Ok(())
    }).await
}

pub fn mark_notified(w: &Wtx, expected: &Landing, at: &str) -> AppResult<()> {
    if let Some(mut e) = current(w, &expected.repo, expected.number)?
        && e.head_sha == expected.head_sha
    {
        e.notified_at = Some(at.into());
        w.put(store::LANDING, &Pull::key(&e.repo, e.number), &e)?;
    }
    Ok(())
}
pub fn mark_closed(w: &Wtx, repo: &str, number: u64) -> AppResult<()> {
    w.del(store::LANDING, &Pull::key(repo, number))?;
    Ok(())
}
pub fn mark_merged(w: &Wtx, repo: &str, number: u64) -> AppResult<()> {
    mark_closed(w, repo, number)
}
pub async fn record_auto_result(
    app: &App,
    repo: &Repo,
    number: u64,
    head: &str,
    base: &str,
    error: Option<String>,
) -> AppResult<()> {
    let (r2, head, base) = (repo.clone(), head.to_string(), base.to_string());
    app.locked(&repo.id, move |app| async move {
        let id = r2.id.clone();
        if let Some(mut e) = app.store.read(move |r| current(r, &id, number)).await?
            && e.head_sha == head
            && e.base_sha == base
            && e.error != error
        {
            e.error = error;
            save(&app, &r2, e, None).await?;
        }
        Ok(())
    })
    .await
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

pub(crate) async fn pull_head_checks(
    app: &App,
    repo: &Repo,
    p: &Pull,
    head: &str,
) -> AppResult<HeadChecks> {
    Ok(pull_head_check_details(app, repo, p, head).await?.0)
}

async fn pull_head_check_details(
    app: &App,
    repo: &Repo,
    p: &Pull,
    head: &str,
) -> AppResult<(HeadChecks, Vec<String>)> {
    let p = p.clone();
    let repair = app
        .store
        .read(move |r| crate::repair::is_pull(r, &p))
        .await?;
    let checks = app.ci.head_checks(repo, head).await?;
    let mut required = repo.clone();
    let mut waiting = repair && !checks.run_registered;
    if repair {
        required
            .required_checks
            .extend(checks.checks.iter().map(|c| c.name.clone()));
        if checks.checks.iter().any(|c| {
            c.path_skip.is_some()
                || c.reused_from
                    .as_ref()
                    .is_some_and(|s| s.basis == crate::ci::queue::Basis::Paths)
        }) {
            waiting = true;
        }
    }
    let jobs = missing_head_checks(&required, &checks.checks, repair);
    Ok((
        if waiting {
            HeadChecks::Running
        } else {
            head_checks(&required, &checks.checks)
        },
        jobs,
    ))
}

fn missing_head_checks(repo: &Repo, checks: &[Check], repair: bool) -> Vec<String> {
    let mut jobs = vec![];
    for name in &repo.required_checks {
        let check = checks.iter().find(|c| c.name == *name);
        if check.is_none_or(|c| {
            c.status != "completed"
                || !c.head_satisfied()
                || repair
                    && (c.path_skip.is_some()
                        || c.reused_from
                            .as_ref()
                            .is_some_and(|s| s.basis == crate::ci::queue::Basis::Paths))
        }) && !jobs.contains(name)
        {
            jobs.push(name.clone());
        }
    }
    jobs
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Reader, Upstream};
    use crate::upstream::Following;
    use serde_json::json;
    fn fixture() -> (Repo, Pull, Landing) {
        let repo = serde_json::from_value(json!({
            "id":"r", "path":"acme/demo", "workspace":"w", "default_branch":"main", "protected_branches":[],
            "required_checks":[], "post_merge_jobs":[], "required_approvals":0, "auto_land":true,"hold_while_failing":false, "ci_network":false, "labels":[],
            "upstream":null, "revision":1, "created_at":"now"
        })).unwrap();
        let pull = serde_json::from_value(json!({
            "repo":"r", "number":1, "title":"Feature", "body":"", "state":"open", "merged":false, "draft":false,
            "author":"test-agent", "head":"feature", "base":"main", "reviewers":[], "tickets":[], "labels":[],
            "reviewed_by":[], "ready_at":null, "created_at":"now", "updated_at":"now", "closed_at":null,
            "merged_at":null, "merge_sha":null, "merged_head":null, "merged_base":null, "upstream":null, "revision":1
        })).unwrap();
        let entry = serde_json::from_value(json!({
            "repo":"r", "number":1, "conflicts":[], "told":[], "base":"main", "queued_at":"now", "sequence":1, "state":"passed", "state_at":"now",
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

    #[test]
    fn unmet_jobs_exclude_success_and_service_skips() {
        let (mut repo, _, _) = fixture();
        repo.required_checks = [
            "failed",
            "running",
            "missing",
            "success",
            "paths",
            "post_merge",
            "cancelled",
            "skipped",
        ]
        .map(str::to_string)
        .to_vec();
        let check = |name: &str, status: &str, conclusion: Option<&str>| Check {
            name: name.into(),
            status: status.into(),
            conclusion: conclusion.map(str::to_string),
            ..Check::default()
        };
        let mut checks = vec![
            check("failed", "completed", Some("failure")),
            check("running", "in_progress", None),
            check("success", "completed", Some("success")),
            check("paths", "completed", Some("skipped")),
            check("post_merge", "completed", Some("skipped")),
            check("cancelled", "completed", Some("cancelled")),
            check("skipped", "completed", Some("skipped")),
            check("optional", "completed", Some("failure")),
        ];
        for (i, why) in [(3, "paths"), (4, "post_merge")] {
            checks[i].path_skip = Some(
                serde_json::from_value(json!({"reason": why, "paths": [], "bases": []})).unwrap(),
            );
        }
        assert_eq!(
            missing_head_checks(&repo, &checks, false),
            ["failed", "running", "missing", "cancelled", "skipped"]
        );
        assert_eq!(
            missing_head_checks(&repo, &checks, true),
            [
                "failed",
                "running",
                "missing",
                "paths",
                "post_merge",
                "cancelled",
                "skipped"
            ]
        );
        repo.required_checks.clear();
        assert!(missing_head_checks(&repo, &checks, false).is_empty());
    }

    #[test]
    fn every_next_action_uses_only_this_pull_and_its_conditions() {
        let (mut r, mut p, mut e) = fixture();
        let mut f = Following::default();
        let n = |r: &Repo, p: &Pull, e: &Landing, f: &Following, b: &[LandingReason]| {
            next(r, p, e, f, "head", b).0
        };
        assert_eq!(n(&r, &p, &e, &f, &[]), "auto_merge");
        r.auto_land = false;
        assert_eq!(n(&r, &p, &e, &f, &[]), "owner_merge");
        r.upstream = Some(Upstream {
            provider: "test-host".into(),
            repository: "remote/demo".into(),
            reader: Reader {
                member: "reader".into(),
                chosen: false,
            },
        });
        assert_eq!(n(&r, &p, &e, &f, &[]), "owner_publish");
        r.auto_land = true;
        assert_eq!(n(&r, &p, &e, &f, &[]), "publishing");
        e.error = Some("failed".into());
        assert_eq!(n(&r, &p, &e, &f, &[]), "owner_fix_auto");
        assert_eq!(
            n(&r, &p, &e, &f, &[reason("unanswered", "reply")]),
            "conditions"
        );
        p.upstream = Some(publication());
        assert_eq!(n(&r, &p, &e, &f, &[]), "owner_merge_host");
        p.upstream.as_mut().unwrap().await_checks(60);
        assert_eq!(n(&r, &p, &e, &f, &[]), "host_checks");
        p.upstream.as_mut().unwrap().stopped = Some("stopped".into());
        assert_eq!(n(&r, &p, &e, &f, &[]), "owner_publish");
        p.upstream.as_mut().unwrap().state = "closed".into();
        assert_eq!(
            n(&r, &p, &e, &f, &[reason("host_failed", "failed")]),
            "host_closed"
        );
        p.upstream.as_mut().unwrap().state = "merged".into();
        assert_eq!(
            n(&r, &p, &e, &f, &[reason("already_in_base", "integrated")]),
            "following"
        );
        f.stopped = Some("diverged".into());
        assert_eq!(n(&r, &p, &e, &f, &[]), "owner_fix_following");
    }
}
