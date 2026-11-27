//! Evidence shared by agent failure logs and protected-branch repair tickets.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::{files::valid_attempt_id, junit, queue};
use crate::app::App;
use crate::error::AppResult;
use crate::util;

const ATTEMPT_LIMIT: usize = 8;
const DEFERRAL_LIMIT: usize = 8;
const TEST_LIMIT: usize = 4;
const TEST_HISTORY_LIMIT: usize = 4;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub(crate) struct JobEvidence {
    pub sha: String,
    pub attempt: u64,
    pub conclusion: Option<String>,
    pub runner: Option<String>,
    pub allocation: Option<queue::Allocation>,
    pub runner_claim: Option<queue::RunnerClaim>,
    pub retry_decision: Option<queue::RetryDecision>,
    pub runner_deferral_count: usize,
    pub runner_deferrals: BTreeMap<String, queue::ResourceDeferral>,
    pub resources: Option<queue::AttemptResources>,
    pub peak_memory_mib: Option<u64>,
    pub other_attempt_count: usize,
    pub other_attempts: Vec<OtherAttempt>,
    /// None when no declared report could be read; zero when it had no failing names.
    pub failed_test_count: Option<usize>,
    pub test_failures: Vec<TestHistory>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub(crate) struct OtherAttempt {
    pub attempt: u64,
    pub url: String,
    pub runner: Option<String>,
    pub status: String,
    pub conclusion: Option<String>,
    pub purpose: queue::Purpose,
    pub reused_from: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub(crate) struct TestHistory {
    pub name: String,
    pub url: String,
    pub failure_count: usize,
    pub failures: Vec<junit::Failure>,
}

/// Load evidence only for a local attempt in the already-authorized repository.
pub(crate) async fn load(app: &App, repo: &str, id: &str) -> AppResult<Option<JobEvidence>> {
    if !valid_attempt_id(id) {
        return Ok(None);
    }
    let (repo, id) = (repo.to_owned(), id.to_owned());
    let Some(a) = app
        .store
        .read(move |r| Ok(queue::get_attempt(r, &id)?.filter(|a| a.repo == repo)))
        .await?
    else {
        return Ok(None);
    };
    let names = match &a.failed_tests {
        Some(names) => Some(names.clone()),
        None => junit::read_attempt(app, &a).await,
    };
    let base = app.base_url.clone();
    app.store
        .read(move |r| {
            let job_url = |id: &str| util::item_url(&base, &a.repo_path, "jobs", id);
            let other_attempts = queue::attempts_of(r, &a.repo, &a.sha)?
                .into_iter()
                .rev()
                .filter(|other| other.id != a.id && other.job.name == a.job.name)
                .collect::<Vec<_>>();
            let history_url = format!(
                "{base}{}/test-failures",
                util::repository_page(&a.repo_path)
            );
            let test_failures = names
                .iter()
                .flatten()
                .take(TEST_LIMIT)
                .map(|name| {
                    let mut failures = junit::history(r, &a.repo, name)?;
                    let failure_count = failures.len();
                    failures.truncate(TEST_HISTORY_LIMIT);
                    Ok(TestHistory {
                        name: name.clone(),
                        url: format!("{history_url}?name={}", util::encode_segment(name)),
                        failure_count,
                        failures,
                    })
                })
                .collect::<AppResult<_>>()?;
            Ok(Some(JobEvidence {
                sha: a.sha,
                attempt: a.number,
                conclusion: a.conclusion,
                runner: a.runner,
                allocation: a.allocation,
                runner_claim: a.runner_claim,
                retry_decision: a.retry_decision,
                runner_deferral_count: a.runner_deferrals.len(),
                runner_deferrals: a
                    .runner_deferrals
                    .into_iter()
                    .take(DEFERRAL_LIMIT)
                    .collect(),
                resources: a.resources,
                peak_memory_mib: a.peak_memory_mib,
                other_attempt_count: other_attempts.len(),
                other_attempts: other_attempts
                    .into_iter()
                    .take(ATTEMPT_LIMIT)
                    .map(|other| OtherAttempt {
                        attempt: other.number,
                        url: job_url(&other.id),
                        runner: other.runner,
                        status: other.status,
                        conclusion: other.conclusion,
                        purpose: other.purpose,
                        reused_from: other.reused_from,
                    })
                    .collect(),
                failed_test_count: names.as_ref().map(std::collections::BTreeSet::len),
                test_failures,
            }))
        })
        .await
}
