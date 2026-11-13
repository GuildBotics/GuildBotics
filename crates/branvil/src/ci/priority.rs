//! Claim-time scheduling decisions, derived only from the current store snapshot.
use std::collections::BTreeMap;

use super::{Check, queue};
use crate::domain::pulls;
use crate::error::AppResult;
use crate::model::{Pull, Repo};
use crate::store::{self, Key, Rd};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Class {
    Protected,
    Landing,
    Normal,
    Deferred,
    Last,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Priority {
    pub class: Class,
    pub reason: Option<String>,
}

impl Priority {
    fn new(class: Class) -> Self {
        Self {
            class,
            reason: None,
        }
    }
    fn reason(class: Class, reason: impl Into<String>) -> Self {
        Self {
            class,
            reason: Some(reason.into()),
        }
    }
}

/// Memoization belongs to one claim, never to persistent state.
#[derive(Default)]
pub(crate) struct Snapshot {
    runs: BTreeMap<(String, String), Priority>,
}

/// PR/check memoization while classifying one run (which can have several branches).
#[derive(Default)]
struct Classifier {
    pulls: BTreeMap<(String, u64, String), Priority>,
    failed: BTreeMap<(String, String), Option<String>>,
}

pub fn class_of(r: &impl Rd, attempt: &queue::Attempt) -> AppResult<Priority> {
    if attempt.purpose == queue::Purpose::CacheRefresh {
        return Ok(Priority::reason(Class::Last, "cache refresh"));
    }
    Classifier::default().run(r, attempt)
}

impl Snapshot {
    pub(crate) fn class_of(&mut self, r: &impl Rd, a: &queue::Attempt) -> AppResult<Priority> {
        if a.purpose == queue::Purpose::CacheRefresh {
            return Ok(Priority::reason(Class::Last, "cache refresh"));
        }
        let key = (a.repo.clone(), a.sha.clone());
        if let Some(p) = self.runs.get(&key) {
            return Ok(p.clone());
        }
        let p = class_of(r, a)?;
        self.runs.insert(key, p.clone());
        Ok(p)
    }
}

impl Classifier {
    fn run(&mut self, r: &impl Rd, a: &queue::Attempt) -> AppResult<Priority> {
        let Some(run) = queue::get_run(r, &a.repo, &a.sha)? else {
            return Ok(Priority::reason(Class::Last, "no CI run"));
        };
        let Some(repo) = r.get::<Repo>(store::REPOS, &Repo::key(&run.repo))? else {
            return Ok(Priority::reason(Class::Last, "no repository record"));
        };
        let mut best: Option<Priority> = None;
        for branch in &run.branches {
            let p = self.branch(r, &repo, &run, branch)?;
            if best.as_ref().is_none_or(|b| p.class < b.class) {
                best = Some(p);
            }
        }
        Ok(best.unwrap_or_else(|| Priority::reason(Class::Last, "no branch wants this commit")))
    }

    fn branch(
        &mut self,
        r: &impl Rd,
        repo: &Repo,
        run: &queue::Run,
        branch: &str,
    ) -> AppResult<Priority> {
        if repo.is_protected(branch) {
            return Ok(Priority::new(Class::Protected));
        }
        if let Some(number) = branch.strip_prefix("gate/").and_then(|s| s.parse().ok())
            && crate::landing::current(r, &repo.id, number)?.is_some_and(|e| {
                matches!(e.state.as_str(), "gating" | "passed")
                    && e.gate.as_deref() == Some(&run.sha)
            })
        {
            return Ok(Priority::new(Class::Landing));
        }
        let mut best: Option<Priority> = None;
        for (_, number) in r.prefix_raw(
            store::IX_OPEN_PULLS,
            &Key::new().s(&repo.id).s(branch).bytes(),
            None,
            usize::MAX,
        )? {
            let number = String::from_utf8_lossy(&number)
                .parse::<u64>()
                .map_err(|_| crate::error::AppError::internal("invalid open pull index"))?;
            if let Some(p) = r.get::<Pull>(store::PULLS, &Pull::key(&repo.id, number))? {
                let key = (repo.id.clone(), number, run.sha.clone());
                let priority = if let Some(cached) = self.pulls.get(&key) {
                    cached.clone()
                } else {
                    let priority = if p.draft {
                        Priority::reason(Class::Last, "draft pull request")
                    } else {
                        let run_key = (repo.id.clone(), run.sha.clone());
                        let failed = match self.failed.entry(run_key) {
                            std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
                            std::collections::btree_map::Entry::Vacant(entry) => {
                                entry.insert(failed_check(r, repo, run)?)
                            }
                        };
                        pull_class(r, repo, run, &p, failed.as_deref())?
                    };
                    self.pulls.insert(key, priority.clone());
                    priority
                };
                if best.as_ref().is_none_or(|b| priority.class < b.class) {
                    best = Some(priority);
                }
            }
        }
        Ok(best.unwrap_or_else(|| Priority::reason(Class::Last, "no open pull request")))
    }
}

fn failed_check(r: &impl Rd, repo: &Repo, run: &queue::Run) -> AppResult<Option<String>> {
    if repo.required_checks.is_empty() {
        return Ok(None);
    }
    let jobs = match &run.pipeline {
        queue::PipelineState::Jobs { jobs } => jobs.as_slice(),
        _ => &[],
    };
    let latest = queue::current_attempts(r, &repo.id, &run.sha, jobs)?
        .into_iter()
        .map(|a| (a.clone(), a))
        .collect();
    let checks = super::run_checks(repo, run, latest, 0, |a, _| Check {
        name: a.job.name.clone(),
        status: a.status.clone(),
        conclusion: a.conclusion.clone(),
        path_skip: a.path_skip.clone(),
        ..Check::default()
    })?;
    Ok(checks
        .checks
        .into_iter()
        .find(|c| {
            repo.required_checks.contains(&c.name) && c.status == "completed" && !c.head_satisfied()
        })
        .map(|c| c.name))
}

fn pull_class(
    r: &impl Rd,
    repo: &Repo,
    run: &queue::Run,
    p: &Pull,
    failed: Option<&str>,
) -> AppResult<Priority> {
    if let Some(name) = failed {
        return Ok(Priority::reason(
            Class::Deferred,
            format!("required check {name} failed on this commit"),
        ));
    }
    let states = pulls::review_states_in(r, p, &run.sha)?;
    let approvals = pulls::approvals(&states);
    if approvals < repo.required_approvals
        && let Some(s) = states
            .iter()
            .find(|s| !s.outdated && s.verdict.as_deref() == Some("request_changes"))
        && !pulls::unanswered(r, p, "")?.is_empty()
    {
        return Ok(Priority::reason(
            Class::Deferred,
            format!(
                "changes requested by {} (approvals {approvals}/{})",
                s.member, repo.required_approvals
            ),
        ));
    }
    Ok(Priority::new(if approvals >= repo.required_approvals {
        Class::Landing
    } else {
        Class::Normal
    }))
}
