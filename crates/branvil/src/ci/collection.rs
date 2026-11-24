//! Observe a collecting pull's required checks and hand failures to repair.
use crate::app::App;
use crate::domain::{pulls, tickets};
use crate::error::AppResult;
use crate::landing::{self, HeadChecks};
use crate::model::Repo;
use crate::repair;
use crate::store;
use serde::Serialize;

use super::verify::{self, FailedJob, RepairTicket, Verification, Verified};

#[derive(Debug, Clone, Serialize)]
pub struct BaseFailure {
    pub branch: String,
    pub sha: String,
    pub failed_jobs: Vec<FailedJob>,
    pub repair: Option<RepairTicket>,
}

async fn outcome(
    app: &App,
    repo: &Repo,
    sha: &str,
    main: &str,
) -> AppResult<(HeadChecks, Vec<FailedJob>)> {
    let required = app.ci.pull_required(repo, sha, main).await?;
    let mut report = app.ci.head_checks(repo, sha).await?;
    app.ci.require(repo, sha, &mut report, &required).await?;
    let state = landing::head_checks(&required.names, &report.checks);
    let mut failures: Vec<_> = report
        .checks
        .into_iter()
        .filter(|c| {
            required.names.contains(&c.name) && c.status == "completed" && !c.head_satisfied()
        })
        .map(|c| FailedJob {
            name: c.name,
            attempt_id: c.attempt_id,
            details: c.details,
        })
        .collect();
    failures.sort_by(|a, b| a.name.cmp(&b.name));
    Ok((state, failures))
}

/// Use live checks even before the sweep creates the repair ticket. An observed
/// failure stays active while a retry or a newer tip is still being checked.
pub async fn base_failure(
    app: &App,
    repo: &Repo,
    branch: &str,
    sha: &str,
    main: &str,
) -> AppResult<Option<BaseFailure>> {
    if branch == repo.default_branch || sha.is_empty() {
        return Ok(None);
    }
    let (repo_copy, name) = (repo.clone(), branch.to_owned());
    let (collecting, prior, ticket) = app
        .store
        .read(move |r| {
            let repo = &repo_copy;
            let ticket = repair::open_ticket_for(r, &repo.id, Some(&name))?;
            let collecting = match pulls::open_pull(r, repo, &name, &repo.default_branch)? {
                Some(pull) => ticket.is_some() || pulls::collects_child_tickets(r, repo, &pull)?,
                None => false,
            };
            Ok((
                collecting,
                verify::get_for(r, &repo.id, Some(&name))?,
                ticket,
            ))
        })
        .await?;
    if !collecting {
        return Ok(None);
    }
    let (state, jobs) = outcome(app, repo, sha, main).await?;
    let (sha, failed_jobs) = match state {
        HeadChecks::Failed => (sha.to_owned(), jobs),
        HeadChecks::Running => match prior.filter(|v| v.state == "failed") {
            Some(v) => (v.sha, v.failed_jobs),
            None => return Ok(None),
        },
        HeadChecks::Passed => return Ok(None),
    };
    Ok(Some(BaseFailure {
        branch: branch.to_owned(),
        sha,
        failed_jobs,
        repair: ticket.map(|t| RepairTicket {
            number: t.number,
            assignee: t.assignee.map(|a| a.name),
            url: tickets::ticket_url(&app.base_url, repo, t.number),
        }),
    }))
}

pub async fn advance(app: &App, repo: &Repo) -> AppResult<()> {
    let id = repo.id.clone();
    app.locked(&repo.id, move |app| async move {
        let (repo, branches) = app
            .store
            .read(move |r| {
                let r2 = crate::domain::repo_by_id(r, &id)?;
                let mut branches = Vec::new();
                for p in pulls::open_pulls(r, &r2)? {
                    if p.base == r2.default_branch
                        && p.head != r2.default_branch
                        && (pulls::collects_child_tickets(r, &r2, &p)?
                            || repair::open_ticket_for(r, &r2.id, Some(&p.head))?.is_some())
                    {
                        branches.push(p.head);
                    }
                }
                Ok((r2, branches))
            })
            .await?;
        if branches.is_empty() {
            return Ok(());
        }
        let tips = pulls::tips(&app, &repo).await?;
        let main = tips.get(&repo.default_branch).map_or("", String::as_str);
        for branch in branches {
            if let Some(sha) = tips.get(&branch) {
                observe(&app, &repo, &branch, sha, main).await?;
            }
        }
        Ok(())
    })
    .await
}

async fn observe(app: &App, repo: &Repo, branch: &str, sha: &str, main: &str) -> AppResult<()> {
    let (state, failed_jobs) = outcome(app, repo, sha, main).await?;
    if state == HeadChecks::Running {
        return Ok(());
    }
    let (id, branch, sha, main) = (
        repo.id.clone(),
        branch.to_owned(),
        sha.to_owned(),
        main.to_owned(),
    );
    let closed = app
        .store
        .write(move |w| {
            let prior = verify::get_for(w, &id, Some(&branch))?;
            let state = if state == HeadChecks::Failed {
                "failed"
            } else {
                "passed"
            };
            if prior.as_ref().is_some_and(|v| {
                v.sha == sha
                    && v.base.as_deref() == Some(&main)
                    && v.state == state
                    && v.failed_jobs == failed_jobs
            }) {
                return Ok(vec![]);
            }
            let now = crate::util::now();
            let mut record = Verification {
                sha: sha.clone(),
                base: Some(main),
                state: state.into(),
                started_at: now.clone(),
                finished_at: Some(now.clone()),
                failed_jobs,
                failing_since: prior.as_ref().and_then(|v| v.failing_since.clone()),
                last_verified: prior.and_then(|v| v.last_verified),
            };
            let closed = if state == "failed" {
                record.failing_since.get_or_insert_with(|| sha.clone());
                repair::failed_for(w, &id, Some(&branch), &sha, &record.failed_jobs)?;
                vec![]
            } else {
                record.failing_since = None;
                record.last_verified = Some(Verified {
                    sha: sha.clone(),
                    at: now,
                });
                repair::passed_for(w, &id, Some(&branch), &branch, &sha)?
            };
            w.put(
                store::CI_VERIFICATIONS,
                &verify::key(&id, Some(&branch)),
                &record,
            )?;
            Ok(closed)
        })
        .await?;
    verify::publish_closed(&app.events, repo, closed);
    Ok(())
}
