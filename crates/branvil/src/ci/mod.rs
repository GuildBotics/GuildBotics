//! CI. The merge path, the checks endpoint, and ref-change handling talk to CI
//! only through `Ci`; the runner talks to it through the runner API
//! (`api.rs`). See `docs/ci.md`.
//!
//! - The pipeline (`.guildbotics-ci.toml`) is read from the commit itself, for a
//!   tip of the default branch and a pull request head alike. What a head
//!   cannot change is the repository's settings: the required checks and
//!   `ci_network`.
//! - Runs and attempts live in redb (`queue.rs`); logs and artifacts are files
//!   under `<data>/ci/` (`files.rs`).
//! - A background task expires leases, fails queued work when no runner shows
//!   up, admits waiting runs, registers every wanted tip again (a run a crash
//!   or a failed registration missed, a run recorded with another pipeline),
//!   and applies retention.

pub mod admin;
pub mod api;
pub mod config;
pub mod files;
pub mod images;
pub mod narrow;
pub mod notify;
mod order;
pub mod pipeline;
pub mod priority;
pub mod queue;
#[cfg(test)]
mod repair_tests;
mod selection;
pub mod settings;
pub mod supervise;
pub mod verify;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::app::App;
use crate::config::Layout;
use crate::error::{AppError, AppResult};
use crate::events::Events;
use crate::git::{Git, ZERO_SHA};
use crate::model::{Member, MemberKind, Repo};
use crate::refs::RefChange;
use crate::store::{self, Key, Rd, Store};
use crate::util;
use config::CiConfig;
use gb_runner::ci::api::RunnerReport;
use queue::{Attempt, PipelineState, Registration};

/// How many commits of the first-parent history `Ci::estimate` looks at.
const ESTIMATE_DEPTH: usize = 20;

/// Where a reused result was actually executed (including its log and artifacts).
#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct ReusedFrom {
    pub attempt_id: String,
    pub url: String,
    pub sha: String,
    pub basis: queue::Basis,
}

/// One check of a commit, in the shape of `docs/api.md` "Checks".
#[derive(Serialize, Clone, Debug, PartialEq, Default)]
pub struct Check {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path_skip: Option<queue::PathSkip>,
    pub name: String,
    /// `queued`, `in_progress`, or `completed`.
    pub status: String,
    /// When completed: `success`, `failure`, `cancelled`, `timed_out`,
    /// `skipped`, or `startup_failure`.
    pub conclusion: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attempt_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attempt: Option<u64>,
    /// The runner that claimed this attempt, if any.
    pub runner: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runner_recovery: Option<queue::RunnerRecovery>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_decision: Option<queue::RetryDecision>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retry_wait: Option<api::RetryWait>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details_url: Option<String>,
    /// Why the check is where it is (a failure reason, "superseded", "no
    /// runner", "waiting for room in the queue", ...).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<String>,
    pub artifact_names: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reused_from: Option<ReusedFrom>,
    /// When its attempt started and finished.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finished_at: Option<String>,
    /// For a running attempt, how long the job took where it last succeeded
    /// (`Ci::estimate`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expected_secs: Option<u64>,
}

impl Check {
    /// A check that has a recorded attempt (as opposed to a placeholder for a
    /// required check the run does not have yet).
    pub fn recorded(&self) -> bool {
        self.attempt_id.is_some()
    }

    pub fn succeeded(&self) -> bool {
        self.status == "completed" && self.conclusion.as_deref() == Some("success")
    }

    /// Required head checks accept the service path selection.
    pub fn head_satisfied(&self) -> bool {
        self.succeeded()
            || (self.status == "completed"
                && self.conclusion.as_deref() == Some("skipped")
                && self.path_skip.is_some())
    }

    fn placeholder(name: &str, status: &str, conclusion: Option<&str>, details: String) -> Self {
        Check {
            name: name.to_string(),
            status: status.to_string(),
            conclusion: conclusion.map(str::to_string),
            details: Some(details),
            ..Check::default()
        }
    }
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct Checks {
    /// Whether this commit has a locally registered CI run.
    pub run_registered: bool,
    pub checks: Vec<Check>,
    /// True when the commit has a pipeline or the repository has required
    /// checks: no checks then means "not registered yet", never "no checks".
    pub checks_expected: bool,
}

/// What the service last heard from a runner (kept in memory: a restarted
/// service learns it again from the runner's next request).
#[derive(Clone, Debug)]
struct RunnerSeen {
    name: String,
    last_seen_at: String,
    report: RunnerReport,
    resource_wait: Option<queue::ResourceWait>,
    seen: Instant,
    stopped: bool,
}

impl RunnerSeen {
    fn online(&self, absent: Duration) -> bool {
        !self.stopped && self.seen.elapsed() <= absent
    }
}

#[derive(Clone)]
pub struct Ci {
    inner: Arc<Inner>,
}

struct Inner {
    #[cfg(test)]
    history_reads: std::sync::atomic::AtomicUsize,
    store: Store,
    git: Git,
    events: Events,
    base_url: String,
    layout: Layout,
    config: CiConfig,
    settings: settings::Control,
    started: Instant,
    runners: Mutex<BTreeMap<String, RunnerSeen>>,
    /// Set by `stop`: when the wait for the running jobs ends.
    stopping: OnceLock<String>,
    /// Serializes log appends (one file per attempt, small writes).
    log_lock: tokio::sync::Mutex<()>,
}

impl Ci {
    pub fn new(
        store: Store,
        git: Git,
        events: Events,
        base_url: String,
        layout: Layout,
        config: CiConfig,
        config_file: std::path::PathBuf,
    ) -> Self {
        Self {
            inner: Arc::new(Inner {
                #[cfg(test)]
                history_reads: std::sync::atomic::AtomicUsize::new(0),
                store,
                git,
                events,
                base_url,
                layout,
                settings: settings::Control::new(config_file, &config),
                config,
                started: Instant::now(),
                runners: Mutex::new(BTreeMap::new()),
                stopping: OnceLock::new(),
                log_lock: tokio::sync::Mutex::new(()),
            }),
        }
    }

    pub fn config(&self) -> &CiConfig {
        &self.inner.config
    }

    /// What the owner changes from the GUI (`settings.rs`).
    pub fn settings(&self) -> &settings::Control {
        &self.inner.settings
    }

    /// The jobs of the pipeline on the tip of `repo`'s default branch (none
    /// without a valid one).
    pub async fn default_jobs(&self, repo: &Repo) -> Vec<pipeline::JobDef> {
        let dir = self.inner.git.repo_dir(&repo.path);
        let tip = format!("refs/heads/{}^{{commit}}", repo.default_branch);
        let sha = match self
            .inner
            .git
            .run(Some(&dir), &["rev-parse", "--verify", "-q", &tip])
            .await
        {
            Ok(out) if out.ok() => out.text().trim().to_string(),
            _ => return Vec::new(),
        };
        match self.read_pipeline(repo, &sha).await {
            Ok(PipelineState::Jobs { jobs }) => jobs,
            _ => Vec::new(),
        }
    }

    fn job_url(&self, repo_path: &str, id: &str) -> String {
        format!("{}/{repo_path}/-/jobs/{id}", self.inner.base_url)
    }

    fn publish(&self, changed: &[(String, String)]) {
        for (path, sha) in changed {
            self.inner.events.publish("check", path, sha.clone());
        }
    }

    pub fn reused_from(&self, a: &Attempt, source: &queue::Source) -> Option<ReusedFrom> {
        let (id, basis) = (a.reused_from.as_ref()?, source.basis?);
        Some(ReusedFrom {
            attempt_id: id.clone(),
            url: self.job_url(&a.repo_path, id),
            sha: source.attempt.sha.clone(),
            basis,
        })
    }

    fn check_of(&self, a: &Attempt, result: &queue::Source) -> Check {
        let source = &result.attempt;
        Check {
            path_skip: a.path_skip.clone(),
            name: a.job.name.clone(),
            status: a.status.clone(),
            conclusion: a.conclusion.clone(),
            attempt_id: Some(a.id.clone()),
            attempt: Some(a.number),
            runner: a.runner.clone(),
            runner_recovery: None,
            retry_decision: a.retry_decision.clone(),
            retry_wait: self.retry_wait(a),
            details_url: Some(self.job_url(&a.repo_path, &a.id)),
            details: a.display_details(),
            artifact_names: if source.files_pruned {
                Vec::new()
            } else {
                source.artifacts.iter().map(|x| x.name.clone()).collect()
            },
            reused_from: self.reused_from(a, result),
            started_at: a.started_at.clone(),
            finished_at: a.finished_at.clone(),
            expected_secs: None,
        }
    }

    /// Fills `expected_secs` of the running attempts among `checks` of `sha`:
    /// how long the same job last took where it succeeded on the commit or
    /// the nearest of its first-parent ancestors (`ESTIMATE_DEPTH` commits in
    /// all). Without one (or when Git cannot say), the checks have none.
    pub async fn estimate(&self, repo: &Repo, sha: &str, checks: &mut [Check]) {
        let running = |c: &Check| c.recorded() && c.status == "in_progress";
        if !checks.iter().any(running) {
            return;
        }
        if let Some(history) = self.job_history(repo, sha).await {
            for c in checks.iter_mut().filter(|c| running(c)) {
                c.expected_secs = history.took.get(&c.name).copied();
            }
        }
    }

    /// Started checks follow execution time; queued checks follow the same
    /// current priority, reuse, and admission sequence used by claim.
    pub async fn order_checks(&self, checks: &mut [Check]) -> AppResult<()> {
        let positions = self
            .inner
            .store
            .read(|r| {
                Ok(queue::scheduled(r, false)?
                    .into_iter()
                    .enumerate()
                    .map(|(i, (a, _))| (a.id, i))
                    .collect::<BTreeMap<_, _>>())
            })
            .await?;
        checks.sort_by(|a, b| check_order(a, &positions).cmp(&check_order(b, &positions)));
        Ok(())
    }

    /// The checks of `sha` in `repo`: the latest attempt of every job of the
    /// commit's run, plus an entry for each required check the run does not
    /// have (see `docs/api.md`, "Checks"). Only attempts of the run's current
    /// job definitions count. Nothing here ever reports success without a
    /// successful attempt of that commit.
    pub async fn head_checks(&self, repo: &Repo, sha: &str) -> AppResult<Checks> {
        let (id, s) = (repo.id.clone(), sha.to_string());
        // Attempts of a job the pipeline no longer has, or had with another
        // definition, are history, not checks.
        let (run, latest, priority, recoveries, queued_details) = self
            .inner
            .store
            .read(move |r| {
                let run = queue::get_run(r, &id, &s)?;
                let latest = match run.as_ref().map(|x| &x.pipeline) {
                    Some(PipelineState::Jobs { jobs }) => {
                        queue::current_attempts(r, &id, &s, jobs)?
                    }
                    _ => Vec::new(),
                };
                let history = if latest
                    .iter()
                    .any(|a| a.number > 1 && a.conclusion.as_deref() == Some("success"))
                {
                    queue::attempts_of(r, &id, &s)?
                } else {
                    Vec::new()
                };
                let mut recoveries = BTreeMap::new();
                let mut queued_details = BTreeMap::new();
                let mut schedule = priority::Snapshot::default();
                let latest = latest
                    .into_iter()
                    .map(|a| {
                        let source = queue::source_of(r, &a)?;
                        if a.status == "queued" {
                            let details = schedule
                                .class_of(r, &a)?
                                .details(a.display_details(), false);
                            queued_details.insert(a.id.clone(), details);
                        }
                        if let Some(recovery) = a.runner_recovery(&history) {
                            recoveries.insert(a.id.clone(), recovery);
                        }
                        Ok((a, source))
                    })
                    .collect::<AppResult<Vec<_>>>()?;
                let priority = run
                    .as_ref()
                    .map(|run| priority::run_class(r, run))
                    .transpose()?;
                Ok((run, latest, priority, recoveries, queued_details))
            })
            .await?;
        let mut checks = Vec::new();
        let Some(run) = run else {
            for name in &repo.required_checks {
                checks.push(Check::placeholder(
                    name,
                    "queued",
                    None,
                    "No CI run is registered for this commit yet.".into(),
                ));
            }
            // An unreadable pipeline is expected rather than "no checks".
            let expected = !repo.required_checks.is_empty()
                || !matches!(
                    self.read_pipeline(repo, sha).await,
                    Ok(PipelineState::Absent)
                );
            return Ok(Checks {
                checks,
                run_registered: false,
                checks_expected: expected,
            });
        };
        let mut report = self.run_checks(repo, &run, latest)?;
        for check in &mut report.checks {
            check.runner_recovery = check
                .attempt_id
                .as_ref()
                .and_then(|id| recoveries.get(id).cloned());
        }
        if report
            .checks
            .iter()
            .any(|c| c.status == "queued" && !c.recorded())
        {
            let order = self.admission_order(repo, sha, &run.pipeline).await;
            report.checks.sort_by_key(|c| {
                order
                    .iter()
                    .position(|name| name == &c.name)
                    .unwrap_or(usize::MAX)
            });
        }
        if let Some(priority) = priority {
            for check in &mut report.checks {
                if check.status == "queued" {
                    check.details = match &check.attempt_id {
                        Some(id) => queued_details.get(id).cloned().flatten(),
                        None => priority.details(check.details.take(), run.waiting),
                    };
                }
            }
        }
        Ok(report)
    }

    /// Checks of a registered run, shared with required-failure notification.
    fn run_checks(
        &self,
        repo: &Repo,
        run: &queue::Run,
        latest: Vec<(Attempt, queue::Source)>,
    ) -> AppResult<Checks> {
        // A protected branch's run requeues another tree's reuse by paths
        // (`jobs_to_run`); while it waits for room, the job is pending.
        let protected = run.cache_scope == "protected";
        let latest = latest
            .into_iter()
            .filter(|(_, s)| !protected || s.basis != Some(queue::Basis::Paths))
            .collect();
        run_checks(
            repo,
            run,
            latest,
            self.inner.config.queue_max,
            |a, source| self.check_of(a, source),
        )
    }

    /// Reads the pipeline of commit `sha` (`pipeline::read`).
    async fn read_pipeline(&self, repo: &Repo, sha: &str) -> AppResult<PipelineState> {
        let dir = self.inner.git.repo_dir(&repo.path);
        pipeline::read(&self.inner.git, &dir, sha).await
    }

    /// Registers CI of `sha` for `branch` when the branch wants CI: it is the
    /// default branch, the head of an open pull request, or an open pull request head. The pipeline is the commit's own. The caller holds the repository lock.
    async fn consider(&self, repo: &Repo, refname: &str, sha: &str) -> AppResult<bool> {
        let Some(branch) = ci_ref(refname) else {
            return Ok(false);
        };
        if branch == repo.default_branch {
            return Ok(false);
        }
        let (id, b) = (repo.id.clone(), branch.to_string());
        if !self
            .inner
            .store
            .read(move |r| has_open_pull(r, &id, &b))
            .await?
        {
            return Ok(false);
        }
        let reg = self.registration(repo, branch, sha).await?;
        self.register(repo, sha, reg).await
    }

    async fn registration(&self, repo: &Repo, branch: &str, sha: &str) -> AppResult<Registration> {
        let pipeline = self.read_pipeline(repo, sha).await?;
        let selection = self
            .select_jobs(repo, sha, &pipeline, repo.is_protected(branch))
            .await?;
        let inputs = self.job_inputs(repo, sha, &pipeline).await?;
        let reg = Registration {
            order: Vec::new(),
            inputs,
            selection,
            repo: repo.id.clone(),
            repo_path: repo.path.clone(),
            sha: sha.to_string(),
            tree: Some(
                self.inner
                    .git
                    .run_ok(
                        Some(&self.inner.git.repo_dir(&repo.path)),
                        &["rev-parse", &format!("{sha}^{{tree}}")],
                    )
                    .await?
                    .trim()
                    .to_string(),
            ),
            branch: branch.to_string(),
            protected: repo.is_protected(branch),
            pipeline,
            network: repo.ci_network,
        };
        Ok(reg)
    }

    async fn register(&self, repo: &Repo, sha: &str, mut reg: Registration) -> AppResult<bool> {
        let snapshot = (
            repo.id.clone(),
            sha.to_string(),
            reg.pipeline.clone(),
            reg.selection.clone(),
            reg.protected,
        );
        let needs_order = self
            .inner
            .store
            .read(move |r| {
                let (id, sha, pipeline, selection, protected) = snapshot;
                let Some(mut run) = queue::get_run(r, &id, &sha)? else {
                    return Ok(true);
                };
                if run.waiting {
                    return Ok(false);
                }
                run.pipeline = pipeline;
                run.selection = selection;
                if protected {
                    run.cache_scope = "protected".into();
                    run.selection = None;
                }
                Ok(!queue::jobs_to_run(r, &run)?.is_empty())
            })
            .await?;
        if needs_order {
            reg.order = self.admission_order(repo, sha, &reg.pipeline).await;
        }
        let queue_max = self.inner.config.queue_max;
        let now = util::now();
        let changed = self
            .inner
            .store
            .write(move |w| queue::register(w, reg, queue_max, &now))
            .await?;
        self.write_skip_logs(repo, sha).await?;
        Ok(changed)
    }

    /// Called once per batch of ref changes Branvil recorded (push, merge,
    /// startup rescan), after the database reflects them, under the
    /// repository lock. Older commits a branch left are superseded; new tips
    /// of the default branch and open pull request heads get a run.
    pub async fn on_ref_changes(&self, repo: &Repo, changes: &[RefChange]) {
        if let Err(e) = self.apply_ref_changes(repo, changes).await {
            // The reconciler registers what this missed.
            tracing::error!("CI registration for {} failed: {e}", repo.path);
        }
    }

    async fn apply_ref_changes(&self, repo: &Repo, changes: &[RefChange]) -> AppResult<()> {
        let mut left = false;
        // Attach new owners before dropping old ones in the same ref batch.
        // In particular a merged head's queued jobs must survive its deletion.
        for c in changes {
            let Some(branch) = ci_ref(&c.refname) else {
                continue;
            };
            if c.new != ZERO_SHA {
                // A failed registration does not hold up the other changes;
                // the reconciler retries it.
                match self.consider(repo, &c.refname, &c.new).await {
                    Ok(true) => self.publish(&[(repo.path.clone(), c.new.clone())]),
                    Ok(false) => {}
                    Err(e) => tracing::error!(
                        "CI registration of {} {branch} {} failed: {e}",
                        repo.path,
                        c.new
                    ),
                }
            }
        }
        if let Err(e) = self.advance_verification(repo, false).await {
            tracing::error!("CI verification of {} failed: {e}", repo.path);
        }
        for c in changes {
            let Some(branch) = ci_ref(&c.refname) else {
                continue;
            };
            if c.old != ZERO_SHA {
                let (id, old, b) = (repo.id.clone(), c.old.clone(), branch.to_string());
                let by = if c.new == ZERO_SHA {
                    "a deletion of the branch".to_string()
                } else {
                    c.new.clone()
                };
                let now = util::now();
                if self
                    .inner
                    .store
                    .write(move |w| queue::branch_left(w, &id, &old, &b, &by, &now))
                    .await?
                {
                    left = true;
                    self.publish(&[(repo.path.clone(), c.old.clone())]);
                }
            }
        }
        if left {
            self.admit_waiting().await;
        }
        self.advance_verification(repo, false).await?;
        Ok(())
    }

    /// A pull request was opened or reopened: its head gets a run.
    pub async fn on_pull_opened(&self, app: &App, repo: &Repo, head: &str) {
        let (r, h) = (repo.clone(), head.to_string());
        let result = app
            .locked(&repo.id, move |app| async move {
                let ci = &app.ci;
                let dir = ci.inner.git.repo_dir(&r.path);
                if let Some(sha) = ci.inner.git.branch_tips(&dir).await?.get(&h)
                    && ci.consider(&r, &format!("refs/heads/{h}"), sha).await?
                {
                    ci.publish(&[(r.path.clone(), sha.clone())]);
                }
                AppResult::Ok(())
            })
            .await;
        if let Err(e) = result {
            tracing::error!("CI registration for {} {head} failed: {e}", repo.path);
        }
    }

    /// Registers the tips of the default branch, every open pull request
    /// head again: a run that is missing (a crash between a push and
    /// its registration, a failed registration) is created, and a run whose
    /// recorded pipeline is not the commit's own gets it. Returns how many
    /// runs changed.
    pub async fn reconcile(&self, app: &App) -> AppResult<usize> {
        let repos: Vec<Repo> = self
            .inner
            .store
            .read(|r| r.prefix_all(store::REPOS, &[]))
            .await?;
        let mut registered = 0;
        for repo in repos {
            let id = repo.id.clone();
            let result = app
                .locked(&id, move |app| async move {
                    app.ci.reconcile_locked(&repo).await
                })
                .await;
            match result {
                Ok(count) => registered += count,
                Err(e) => tracing::warn!(
                    "CI: reconciling repository {id} failed (retried in a minute): {e}"
                ),
            }
        }
        Ok(registered)
    }

    /// `reconcile` of one repository, under its lock.
    async fn reconcile_locked(&self, repo: &Repo) -> AppResult<usize> {
        let mut registered = 0;
        let dir = self.inner.git.repo_dir(&repo.path);
        let tips = self.inner.git.all_refs(&dir).await?;
        let id = repo.id.clone();
        let mut wanted: Vec<String> = self
            .inner
            .store
            .read(move |r| {
                let wanted: Vec<String> = open_pull_heads(r, &id)?
                    .into_iter()
                    .map(|head| format!("refs/heads/{head}"))
                    .collect();
                Ok(wanted)
            })
            .await?;
        match self.advance_verification(repo, true).await {
            Ok(count) => registered += count,
            Err(e) => tracing::warn!(
                "CI: verifying {} failed (retried in a minute): {e}",
                repo.path
            ),
        }
        if let Err(e) = self.advance_narrowings(repo).await {
            tracing::warn!(
                "CI: narrowing {} failed (retried in a minute): {e}",
                repo.path
            );
        }
        wanted.sort();
        wanted.dedup();
        for branch in wanted {
            let Some(sha) = tips.get(&branch) else {
                continue;
            };
            match self.consider(repo, &branch, sha).await {
                Ok(true) => {
                    tracing::warn!("CI: reconciled the run of {} {branch} {sha}", repo.path);
                    registered += 1;
                    self.publish(&[(repo.path.clone(), sha.clone())]);
                }
                Ok(false) => {}
                Err(e) => tracing::warn!(
                    "CI: registering {} {branch} {sha} failed (retried in a minute): {e}",
                    repo.path
                ),
            }
        }
        Ok(registered)
    }

    async fn admit_waiting(&self) {
        let (queue_max, now) = (self.inner.config.queue_max, util::now());
        let candidates = match self
            .inner
            .store
            .read(move |r| queue::waiting_candidates(r, queue_max))
            .await
        {
            Ok(runs) => runs,
            Err(e) => {
                tracing::error!("CI: reading waiting candidates failed: {e}");
                return;
            }
        };
        let mut orders = queue::AdmissionOrders::new();
        for run in candidates {
            let id = run.repo.clone();
            match self
                .inner
                .store
                .read(move |r| r.get::<Repo>(store::REPOS, &Repo::key(&id)))
                .await
            {
                Ok(Some(repo)) => {
                    let order = self.admission_order(&repo, &run.sha, &run.pipeline).await;
                    orders.insert((run.repo, run.sha), order);
                }
                Ok(None) => {}
                Err(e) => tracing::warn!("CI: reading waiting repository failed: {e}"),
            }
        }
        match self
            .inner
            .store
            .write(move |w| queue::admit_waiting(w, queue_max, &orders, &now))
            .await
        {
            Ok(admitted) => self.publish(&admitted),
            Err(e) => tracing::error!("CI: admitting waiting runs failed: {e}"),
        }
    }

    /// One pass of the periodic work: expired leases, queued work nobody
    /// takes, waiting runs.
    pub async fn sweep(&self) -> AppResult<()> {
        let now = util::now();
        let changed = self
            .inner
            .store
            .write(move |w| queue::expire(w, &now))
            .await?;
        self.publish(&changed);
        let absent = self.inner.config.runner_absent_secs;
        let (last_contact, last_text) = {
            let runners = self.inner.runners.lock().unwrap();
            (
                runners
                    .values()
                    .map(|r| r.seen)
                    .max()
                    .unwrap_or(self.inner.started)
                    .max(self.inner.started),
                runners.values().map(|r| r.last_seen_at.clone()).max(),
            )
        };
        if last_contact.elapsed() > Duration::from_secs(absent) {
            let details = format!(
                "No CI runner has contacted the server for {absent} s (last contact: {}). Start `guildbotics runner` (docs/ci.md)",
                last_text.unwrap_or_else(|| "none since Branvil started".into())
            );
            let now = util::now();
            let before = queue::earlier(&now, absent);
            let changed = self
                .inner
                .store
                .write(move |w| {
                    queue::fail_unavailable(w, &before, &now, |a| {
                        Some(format!(
                            "{details}; missing image {} for CPU arm64 or amd64",
                            a.job.image
                        ))
                    })
                })
                .await?;
            self.publish(&changed);
        }
        if self.inner.started.elapsed() >= Duration::from_secs(absent) {
            let live: Vec<RunnerSeen> = self
                .inner
                .runners
                .lock()
                .unwrap()
                .values()
                .filter(|r| r.online(Duration::from_secs(absent)))
                .cloned()
                .collect();
            let available = live
                .iter()
                .filter(|r| r.report.can_run())
                .flat_map(|r| r.report.images.clone())
                .collect();
            let available =
                images::current(&self.inner.layout.root, available).map_err(AppError::internal)?;
            let now = util::now();
            let before = queue::earlier(&now, absent);
            let changed = self
                .inner
                .store
                .write(move |w| {
                    queue::fail_unavailable(w, &before, &now, |a| {
                        (!available.iter().any(|i| i.supports(&a.job.image))).then(|| {
                            let reasons = live.iter().map(|r| format!("runner {} (CPU {}): {}", r.name,
                                &r.report.architecture,
                                r.report.executor_detail.as_deref().unwrap_or(if r.report.can_run() {"image not cached"} else {"runner version or executor unavailable"}))).collect::<Vec<_>>().join("; ");
                            format!("No ready CI runner can execute image {}: {reasons}. Check runner diagnostics for executor/synchronization failures. Register this CPU's archive under Settings → CI or `guildbotics environment image load --ci`, or for registry images run `guildbotics runner pull {}` on that runner", a.job.image, a.job.image)
                        })
                    })
                })
                .await?;
            self.publish(&changed);
        }
        self.admit_waiting().await;
        Ok(())
    }

    /// Whether the service is stopping (`stop`): it gives out no job.
    pub fn stopping(&self) -> bool {
        self.inner.stopping.get().is_some()
    }

    /// The first part of a stop (SIGTERM, an update): gives out no more jobs
    /// and waits for running checks, at most
    /// `ci.stop_wait_secs`. The checks
    /// still running then are interrupted and queued again in their place,
    /// ahead of the jobs queued meanwhile (which run after the next start);
    /// it returns how many. With `announce`, it says on stdout that it waits
    /// (`cli::serve`).
    pub async fn stop(&self, announce: bool) -> AppResult<usize> {
        let wait = self.inner.config.stop_wait_secs;
        let deadline = Instant::now() + Duration::from_secs(wait);
        let _ = self.inner.stopping.set(queue::later(&util::now(), wait));
        // The first count is a write, so it comes after every claim that did
        // not see the service stopping; no claim succeeds after it.
        let store = &self.inner.store;
        let mut running = store.write(|w| Ok(queue::counts(w)?.1)).await?;
        let mut told = 0;
        while running > 0 {
            if Instant::now() >= deadline {
                let why = format!(
                    "Branvil stopped (an update or a restart) before it finished, {wait} s after it stopped giving out jobs"
                );
                let now = util::now();
                let changed = store
                    .write(move |w| queue::interrupt_running(w, None, &why, &now))
                    .await?;
                self.publish(&changed);
                return Ok(changed.len());
            }
            if running != told {
                // The standalone service announces the wait once on stdout.
                if told == 0 && announce {
                    use std::io::Write;
                    let mut out = std::io::stdout().lock();
                    let _ = writeln!(
                        out,
                        "branvil stopping: waiting for {running} running CI job(s) to finish, at most {wait} s"
                    )
                    .and_then(|_| out.flush());
                }
                tracing::info!(
                    "stopping: waiting for {running} running CI job(s) to finish, at most {wait} s after the stop began; new jobs wait for the next start"
                );
                told = running;
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
            running = store.read(|r| Ok(queue::counts(r)?.1)).await?;
        }
        Ok(0)
    }

    /// Keeps the last report after the supervisor stops the local runner.
    pub fn runner_stopped(&self, name: &str) {
        if let Some(runner) = self
            .inner
            .runners
            .lock()
            .unwrap()
            .get_mut(&util::fold(name))
        {
            runner.stopped = true;
        }
    }

    fn reported_runners(&self, r: &impl Rd) -> AppResult<Vec<RunnerSeen>> {
        let reports = self.inner.runners.lock().unwrap().clone();
        let mut runners = Vec::new();
        for (_, mut seen) in reports {
            if !seen.report.can_run()
                || !r
                    .get::<Member>(store::MEMBERS, &Member::key(&seen.name))?
                    .is_some_and(|member| member.kind == MemberKind::Runner)
            {
                continue;
            }
            seen.report.images = images::current(&self.inner.layout.root, seen.report.images)
                .map_err(AppError::internal)?;
            runners.push(seen);
        }
        Ok(runners)
    }

    fn ready_alternatives(&self, r: &impl Rd, runner: &str) -> AppResult<Vec<queue::ClaimRunner>> {
        let settings = self.settings();
        let absent = Duration::from_secs(self.inner.config.runner_absent_secs);
        let own = util::fold(runner);
        Ok(self
            .reported_runners(r)?
            .into_iter()
            .filter_map(|seen| {
                let (limits, _) = settings.runner_limits(&seen.name);
                (util::fold(&seen.name) != own
                    && seen.online(absent)
                    && seen.report.accept
                    && seen.report.running_jobs < limits.max_jobs as usize)
                    .then_some(queue::ClaimRunner {
                        name: seen.name,
                        images: seen.report.images,
                        limits,
                        machine: seen.report.machine,
                    })
            })
            .collect())
    }

    fn matching_runners(&self, r: &impl Rd, image: &str) -> AppResult<Vec<String>> {
        Ok(self
            .reported_runners(r)?
            .into_iter()
            .filter(|seen| seen.report.images.iter().any(|i| i.supports(image)))
            .map(|seen| seen.name)
            .collect())
    }

    fn retry_wait(&self, attempt: &Attempt) -> Option<api::RetryWait> {
        use api::RetryWait;
        if attempt.status != "queued" {
            return None;
        }
        let retry = attempt.retry_decision.as_ref()?;
        if let Some(name) = &retry.selection.runner {
            let reports = self.inner.runners.lock().unwrap();
            let absent = Duration::from_secs(self.inner.config.runner_absent_secs);
            let seen = reports
                .get(&util::fold(name))
                .filter(|seen| seen.online(absent));
            return Some(match seen {
                None => RetryWait::Offline,
                Some(seen) if !seen.report.can_run() => RetryWait::Unavailable,
                Some(seen) => match &seen.resource_wait {
                    Some(wait) if wait.attempt_id == attempt.id => {
                        RetryWait::Resources(wait.clone())
                    }
                    _ => {
                        let (limits, _) = self.settings().runner_limits(name);
                        if seen.report.running_jobs >= limits.max_jobs as usize {
                            RetryWait::Busy
                        } else {
                            RetryWait::Waiting
                        }
                    }
                },
            });
        }
        retry
            .prefer_other_until
            .as_ref()
            .filter(|until| util::now() < **until)
            .map(|until| RetryWait::PreferOther {
                until: until.clone(),
            })
    }

    pub fn report_runner(&self, name: &str, report: RunnerReport) -> Instant {
        let seen = Instant::now();
        self.inner.runners.lock().unwrap().insert(
            util::fold(name),
            RunnerSeen {
                name: name.to_string(),
                last_seen_at: util::now(),
                report,
                resource_wait: None,
                seen,
                stopped: false,
            },
        );
        seen
    }

    pub(super) fn report_resource_wait(
        &self,
        name: &str,
        seen: Instant,
        wait: Option<queue::ResourceWait>,
    ) {
        if let Some(report) = self
            .inner
            .runners
            .lock()
            .unwrap()
            .get_mut(&util::fold(name))
            && report.seen == seen
        {
            report.resource_wait = wait;
        }
    }

    /// Diagnostics section for `GET /api/v1/admin/diagnostics`.
    pub async fn diagnostics(&self) -> serde_json::Value {
        match self.runner_diagnostics(None).await {
            Ok(view) => view,
            Err(e) => serde_json::json!({"error": e.message}),
        }
    }

    pub(super) async fn runner_diagnostics(
        &self,
        required_images: Option<&[String]>,
    ) -> AppResult<serde_json::Value> {
        let runners = self.inner.runners.lock().unwrap().clone();
        let claimed_waits = runners
            .iter()
            .filter_map(|(name, runner)| Some((name.clone(), runner.resource_wait.clone()?)))
            .collect::<Vec<_>>();
        let (members, running, (queued, in_progress, waiting), resource_waits) = self
            .inner
            .store
            .read(move |r| {
                let members: Vec<Member> = r.prefix_all(store::MEMBERS, &[])?;
                let mut waits = BTreeMap::new();
                for (name, wait) in claimed_waits {
                    if queue::get_attempt(r, &wait.attempt_id)?
                        .is_some_and(|a| a.status == "queued")
                    {
                        waits.insert(name, wait);
                    }
                }
                Ok((
                    members,
                    queue::running_by_runner(r)?,
                    queue::counts(r)?,
                    waits,
                ))
            })
            .await?;
        let absent = Duration::from_secs(self.inner.config.runner_absent_secs);
        let members: Vec<_> = members
            .iter()
            .filter(|m| m.kind == MemberKind::Runner)
            .collect();
        let live: Vec<_> = members
            .iter()
            .filter_map(|m| runners.get(&util::fold(&m.name)))
            .filter(|r| r.online(absent))
            .collect();
        let (state, detail, version_mismatch) = if let Some(until) = self.inner.stopping.get() {
            (
                "stopping",
                format!(
                    "Branvil is stopping (an update or a restart): it gives out no job and waits until the running ones finish, at most until {until}; queued jobs run after it starts again."
                ),
                None,
            )
        } else if live.is_empty() {
            (
                "no_runner",
                format!(
                    "No runner has contacted Branvil in the last {} s; queued jobs conclude startup_failure.",
                    absent.as_secs()
                ),
                None,
            )
        } else if live.iter().any(|r| r.report.can_run()) {
            (
                "ready",
                format!("{} runner(s) connected.", live.len()),
                None,
            )
        } else if let Some(runner) = live.iter().find(|r| !r.report.matches_hub_version()) {
            (
                "executor_unavailable",
                format!(
                    "{} reported version {}, which differs from the Hub.",
                    runner.name, runner.report.version
                ),
                Some(serde_json::json!({"name": runner.name, "version": runner.report.version})),
            )
        } else {
            (
                "executor_unavailable",
                live.iter()
                    .find_map(|r| r.report.executor_detail.clone())
                    .unwrap_or_else(|| "The runner's executor is not usable.".into()),
                None,
            )
        };
        let mut rows = Vec::new();
        for member in members {
            let key = util::fold(&member.name);
            let seen = runners.get(&key);
            let report = seen.map(|r| &r.report);
            let connection = match seen {
                Some(r) if r.online(absent) => "online",
                Some(_) => "offline",
                None => "never_seen",
            };
            let missing_images = match (required_images, report) {
                (Some(required), Some(report)) => {
                    let held = images::current(&self.inner.layout.root, report.images.clone())
                        .map_err(AppError::internal)?;
                    Some(
                        required
                            .iter()
                            .filter(|reference| {
                                !held.iter().any(|image| {
                                    image.architecture == report.architecture
                                        && image.supports(reference)
                                })
                            })
                            .collect::<Vec<_>>(),
                    )
                }
                _ => None,
            };
            let (limits, limits_source) = self.inner.settings.runner_limits(&member.name);
            rows.push(serde_json::json!({
                "name": member.name,
                "connection": connection,
                "last_seen_at": seen.map(|r| &r.last_seen_at),
                "running_jobs": running.get(&key).copied().unwrap_or(0),
                "version": report.map(|r| &r.version),
                "version_matches": report.map(RunnerReport::matches_hub_version),
                "max_jobs": report.map(|r| r.max_jobs),
                "executor": report.map(|r| &r.executor),
                "executor_state": report.map(|r| r.executor_state),
                "executor_detail": report.and_then(|r| r.executor_detail.as_ref()),
                "architecture": report.map(|r| &r.architecture),
                "machine": report.and_then(|r| r.machine.as_ref()),
                "resource_wait": resource_waits.get(&key).filter(|_| connection == "online"),
                "missing_images": missing_images,
                "limits": limits,
                "limits_source": limits_source,
            }));
        }
        Ok(serde_json::json!({
            "state": state,
            "detail": detail,
            "version_mismatch": version_mismatch,
            "mode": self.inner.settings.mode(),
            "runners": rows,
            "queue": {
                "queued": queued, "in_progress": in_progress, "waiting_runs": waiting,
                "limit": self.inner.config.queue_max,
            },
        }))
    }

    /// Starts the periodic work: sweep every 5 s, reconcile every minute,
    /// retention every hour, and one orphan-file collection at start.
    pub fn start(app: crate::app::Shared) {
        verify::start(app.clone());
        tokio::spawn(async move {
            let ci = app.ci.clone();
            match files::collect_orphans(&app).await {
                Ok(0) => {}
                Ok(n) => tracing::info!("CI: removed {n} orphaned log/artifact file(s)"),
                Err(e) => tracing::warn!("CI: orphan collection failed: {e}"),
            }
            let mut tick: u64 = 0;
            loop {
                if tick.is_multiple_of(12)
                    && let Err(e) = ci.reconcile(&app).await
                {
                    tracing::warn!("CI: reconcile failed: {e}");
                }
                if let Err(e) = ci.sweep().await {
                    tracing::warn!("CI: sweep failed: {e}");
                }
                if tick.is_multiple_of(720)
                    && let Err(e) = files::apply_retention(&app).await
                {
                    tracing::warn!("CI: retention failed: {e}");
                }
                tick += 1;
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
        });
    }
}

fn check_order<'a>(c: &'a Check, positions: &BTreeMap<String, usize>) -> (u8, &'a str, usize) {
    if let Some(at) = c.started_at.as_deref().or(c.finished_at.as_deref()) {
        return (0, at, 0);
    }
    if c.status != "queued" {
        return (1, "", 0);
    }
    let position = c
        .attempt_id
        .as_ref()
        .and_then(|id| positions.get(id))
        .copied();
    (
        if position.is_some() { 2 } else { 3 },
        "",
        position.unwrap_or(usize::MAX),
    )
}

/// Only public branch refs request CI.
fn ci_ref(refname: &str) -> Option<&str> {
    if let Some(branch) = refname.strip_prefix("refs/heads/") {
        return Some(branch);
    }
    None
}

/// Whether an open pull request has `branch` as its head.
fn has_open_pull(r: &impl Rd, repo: &str, branch: &str) -> AppResult<bool> {
    let prefix = Key::new().s(repo).s(branch).bytes();
    Ok(!r
        .prefix_raw(store::IX_OPEN_PULLS, &prefix, None, 1)?
        .is_empty())
}

/// The head branches of every open pull request of a repository.
fn open_pull_heads(r: &impl Rd, repo: &str) -> AppResult<Vec<String>> {
    let mut heads = Vec::new();
    for (k, _) in r.prefix_raw(
        store::IX_OPEN_PULLS,
        &Key::new().s(repo).bytes(),
        None,
        usize::MAX,
    )? {
        if let Some(p) = store::decode_key(&k, "sss") {
            heads.push(p[1].s().to_string());
        }
    }
    Ok(heads)
}

/// Build registered checks inside or outside a store transaction using the same rules.
fn run_checks<S>(
    repo: &Repo,
    run: &queue::Run,
    latest: Vec<(Attempt, S)>,
    queue_max: usize,
    check_of: impl Fn(&Attempt, &S) -> Check,
) -> AppResult<Checks> {
    // A formerly skipped job now waiting for capacity is pending, not a
    // satisfied check left over from the old base snapshot.
    let latest: Vec<_> = latest
        .into_iter()
        .filter(|(a, _)| run.includes(a))
        .collect();
    let mut checks = Vec::new();
    if let PipelineState::Invalid { message } = &run.pipeline {
        checks.push(Check::placeholder(
            "pipeline",
            "completed",
            Some("failure"),
            format!(
                "The {} of {} is invalid: {message}",
                pipeline::PIPELINE_PATH,
                run.sha
            ),
        ));
    }
    for (a, source) in &latest {
        checks.push(check_of(a, source));
    }
    if let PipelineState::Jobs { jobs } = &run.pipeline {
        for j in jobs
            .iter()
            .filter(|j| !latest.iter().any(|(a, _)| a.job.name == j.name))
        {
            checks.push(match &run.superseded_by {
                Some(by) => Check::placeholder(
                    &j.name,
                    "completed",
                    Some("cancelled"),
                    format!("Superseded by {by} before it was queued."),
                ),
                None if run.waiting => Check::placeholder(
                    &j.name,
                    "queued",
                    None,
                    format!(
                        "Waiting for room in the CI queue ({} attempts at most).",
                        queue_max
                    ),
                ),
                None => Check::placeholder(
                    &j.name,
                    "queued",
                    None,
                    "Not queued yet; the reconciler queues it within a minute.".into(),
                ),
            });
        }
    }
    for name in &repo.required_checks {
        if checks.iter().any(|c| &c.name == name) {
            continue;
        }
        let why = match &run.pipeline {
            PipelineState::Absent => format!(
                "{} has no {}, so the required check {name} cannot run.",
                run.sha,
                pipeline::PIPELINE_PATH
            ),
            PipelineState::Invalid { .. } => {
                format!("The pipeline is invalid, so the required check {name} cannot run.")
            }
            PipelineState::Jobs { .. } => format!(
                "The {} of {} defines no job named {name}.",
                pipeline::PIPELINE_PATH,
                run.sha
            ),
        };
        checks.push(Check::placeholder(name, "completed", Some("failure"), why));
    }
    Ok(Checks {
        run_registered: true,
        checks,
        checks_expected: run.pipeline != PipelineState::Absent || !repo.required_checks.is_empty(),
    })
}

#[cfg(test)]
mod verification_tests;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ci::pipeline::JobDef;
    use gb_runner::ci::api::ExecutorState;

    pub(super) fn setup() -> (tempfile::TempDir, Ci, Repo) {
        let dir = tempfile::tempdir().unwrap();
        let layout = Layout::new(dir.path());
        let store = Store::open(&dir.path().join("t.redb"), 1 << 20, 1).unwrap();
        let git = Git::new(layout.clone(), 2, Duration::from_secs(10));
        let config = CiConfig {
            queue_max: 1,
            ..CiConfig::default()
        };
        let ci = Ci::new(
            store,
            git,
            Events::new(),
            "http://127.0.0.1:7878".into(),
            layout,
            config,
            std::path::PathBuf::from("/nonexistent/branvil.toml"),
        );
        let repo = Repo {
            id: "repo_1".into(),
            path: "acme/demo".into(),
            workspace: "ws_1".into(),
            default_branch: "main".into(),
            protected_branches: vec![],
            post_merge_jobs: Vec::new(),
            required_checks: vec!["test".into()],
            required_approvals: 1,
            auto_land: false,
            hold_while_failing: false,
            ci_network: false,
            responder: None,
            labels: vec![],
            upstream: None,
            revision: 1,
            created_at: String::new(),
        };
        (dir, ci, repo)
    }

    fn job(name: &str) -> JobDef {
        JobDef {
            verified_sha: false,
            paths: Vec::new(),
            name: name.into(),
            image: "alpine".into(),
            commands: vec!["true".into()],
            timeout_secs: None,
            tmpfs_mib: None,
            artifacts: vec![],
            cache: None,
        }
    }

    #[tokio::test]
    async fn expired_runner_reports_stay_in_the_list_without_affecting_readiness() {
        let (_dir, ci, _) = setup();
        use crate::domain::members::{NewMember, insert_member};
        ci.inner
            .store
            .write(|w| {
                for name in ["expired", "ready", "silent"] {
                    insert_member(
                        w,
                        &NewMember {
                            name: name.into(),
                            kind: MemberKind::Runner,
                            display_name: None,
                        },
                    )?;
                }
                Ok(())
            })
            .await
            .unwrap();
        let report = RunnerReport {
            executor: "fixture".into(),
            executor_state: ExecutorState::Ready,
            executor_detail: None,
            architecture: "arm64".into(),
            images: Vec::new(),
            version: gb_base::BUILD_VERSION.into(),
            max_jobs: 2,
            accept: false,
            running_jobs: 0,
            machine: None,
        };
        ci.report_runner("expired", report.clone());
        ci.inner
            .runners
            .lock()
            .unwrap()
            .get_mut("expired")
            .unwrap()
            .seen -= Duration::from_secs(ci.config().runner_absent_secs + 1);
        let view = ci.diagnostics().await;
        assert_eq!(view["state"], "no_runner");
        assert_eq!(view["runners"][0]["name"], "expired");
        assert_eq!(view["runners"][0]["connection"], "offline");
        assert!(view["runners"][0]["last_seen_at"].is_string());
        ci.report_runner("ready", report);
        let view = ci.diagnostics().await;
        assert_eq!(view["state"], "ready");
        assert_eq!(view["runners"][0]["connection"], "offline");
        assert_eq!(view["runners"][1]["connection"], "online");
        assert_eq!(view["runners"][2]["connection"], "never_seen");
    }

    #[tokio::test]
    async fn missing_images_expire_even_while_an_incompatible_runner_is_online() {
        for case in ["missing", "ready", "unavailable", "sync"] {
            let matching = case == "ready";
            let (_dir, mut ci, _) = setup();
            Arc::get_mut(&mut ci.inner).unwrap().started =
                Instant::now() - Duration::from_secs(121);
            ci.inner
                .store
                .write(|w| {
                    queue::register(
                        w,
                        Registration {
                            order: Vec::new(),
                            inputs: Default::default(),
                            selection: None,
                            repo: "repo_1".into(),
                            repo_path: "acme/demo".into(),
                            sha: "sha".into(),
                            tree: None,
                            branch: "feature".into(),
                            protected: false,
                            network: false,
                            pipeline: PipelineState::Jobs {
                                jobs: vec![job("test")],
                            },
                        },
                        10,
                        "2026-01-01T00:00:00Z",
                    )?;
                    Ok(())
                })
                .await
                .unwrap();
            ci.report_runner(
                "r",
                RunnerReport {
                    executor: "test".into(),
                    executor_state: if case == "unavailable" {
                        ExecutorState::Unavailable
                    } else {
                        ExecutorState::Ready
                    },
                    executor_detail: match case {
                        "unavailable" => Some("msb was not found".into()),
                        "sync" => Some("image synchronization failed: disk full".into()),
                        _ => None,
                    },
                    architecture: "amd64".into(),
                    images: vec![images::Image {
                        reference: if matching { "alpine" } else { "other" }.into(),
                        architecture: "amd64".into(),
                        digest: "sha256:test".into(),
                    }],
                    version: gb_base::BUILD_VERSION.into(),
                    max_jobs: 1,
                    accept: true,
                    running_jobs: 0,
                    machine: None,
                },
            );
            ci.sweep().await.unwrap();
            let attempts = ci
                .inner
                .store
                .read(|r| queue::attempts_of(r, "repo_1", "sha"))
                .await
                .unwrap();
            if matching {
                assert_eq!(attempts[0].status, "queued");
            } else {
                assert_eq!(attempts[0].conclusion.as_deref(), Some("startup_failure"));
                let details = attempts[0].details.as_deref().unwrap();
                assert!(details.contains("alpine") && details.contains("amd64"));
                assert!(details.contains("Settings → CI") && details.contains("runner pull"));
                if case == "unavailable" {
                    assert!(details.contains("msb was not found"), "{details}");
                }
                if case == "sync" {
                    assert!(details.contains("disk full"), "{details}");
                }
            }
        }
    }

    #[tokio::test]
    async fn displayed_checks_follow_execution_time_and_current_claim_order() {
        let (_d, ci, mut repo) = setup();
        repo.required_checks.clear();
        let id = repo.id.clone();
        ci.inner
            .store
            .write(move |w| {
                queue::register(
                    w,
                    Registration {
                        order: vec!["z".into(), "a".into(), "m".into()],
                        inputs: Default::default(),
                        selection: None,
                        repo: id,
                        repo_path: "acme/demo".into(),
                        sha: "sha".into(),
                        tree: None,
                        branch: "feature".into(),
                        protected: false,
                        network: false,
                        pipeline: PipelineState::Jobs {
                            jobs: vec![job("a"), job("m"), job("z")],
                        },
                    },
                    10,
                    "2026-11-14T00:00:00Z",
                )?;
                Ok(())
            })
            .await
            .unwrap();
        let mut checks = ci.head_checks(&repo, "sha").await.unwrap().checks;
        ci.order_checks(&mut checks).await.unwrap();
        assert_eq!(
            checks.iter().map(|c| c.name.as_str()).collect::<Vec<_>>(),
            ["z", "a", "m"]
        );
        let claimed = ci
            .inner
            .store
            .write(|w| queue::claim(w, "runner", 60, "2026-11-14T00:00:01Z"))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            checks[0].attempt_id.as_deref(),
            Some(claimed.attempt.id.as_str())
        );
        let mut checks = ci.head_checks(&repo, "sha").await.unwrap().checks;
        checks.push(Check {
            name: "older-host-check".into(),
            status: "completed".into(),
            started_at: Some("2026-11-14T00:00:00Z".into()),
            ..Check::default()
        });
        checks.push(Check {
            name: "not-admitted".into(),
            status: "queued".into(),
            ..Check::default()
        });
        ci.order_checks(&mut checks).await.unwrap();
        assert_eq!(
            checks.iter().map(|c| c.name.as_str()).collect::<Vec<_>>(),
            ["older-host-check", "z", "a", "m", "not-admitted"]
        );
    }

    #[tokio::test]
    async fn waiting_and_queued_checks_explain_current_deferral() {
        let (_d, ci, repo) = setup();
        ci.inner
            .store
            .write(|w| {
                for sha in ["queued", "waiting"] {
                    queue::register(
                        w,
                        Registration {
                            order: Vec::new(),
                            inputs: Default::default(),
                            repo: "repo_1".into(),
                            repo_path: "acme/demo".into(),
                            sha: sha.into(),
                            tree: None,
                            branch: "feature".into(),
                            protected: false,
                            network: false,
                            pipeline: PipelineState::Jobs {
                                jobs: vec![job("test")],
                            },
                            selection: None,
                        },
                        1,
                        "2026-10-27T00:00:00Z",
                    )?;
                }
                Ok(())
            })
            .await
            .unwrap();
        let queued = ci.head_checks(&repo, "queued").await.unwrap();
        assert_eq!(
            queued.checks[0].details.as_deref(),
            Some("Deferred: no repository record. It runs when no other job is queued.")
        );
        let waiting = ci.head_checks(&repo, "waiting").await.unwrap();
        assert_eq!(
            waiting.checks[0].details.as_deref(),
            Some(
                "Waiting for room in the CI queue (1 attempts at most). Deferred: no repository record."
            )
        );
    }

    #[tokio::test]
    async fn non_default_refs_without_open_pulls_do_not_register_even_when_protected() {
        let (_d, ci, mut repo) = setup();
        repo.protected_branches = vec!["release".into()];
        for branch in ["release", "feature"] {
            // Admission must reject the ref before trying to read its pipeline.
            assert!(
                !ci.consider(&repo, &format!("refs/heads/{branch}"), "missing")
                    .await
                    .unwrap()
            );
        }
    }

    #[tokio::test]
    async fn a_reselected_skip_is_pending_while_waiting_and_never_a_protected_failure() {
        let (_d, ci, mut repo) = setup();
        repo.required_checks = vec!["test".into()];
        let (id, path) = (repo.id.clone(), repo.path.clone());
        let saved = repo.clone();
        ci.inner
            .store
            .write(move |w| {
                w.put(store::REPOS, &Repo::key(&saved.id), &saved)?;
                let reg = |sha: &str, selection| Registration {
                    order: Vec::new(),
                    inputs: Default::default(),
                    repo: id.clone(),
                    repo_path: path.clone(),
                    sha: sha.into(),
                    tree: None,
                    branch: "feature".into(),
                    protected: false,
                    network: false,
                    pipeline: PipelineState::Jobs {
                        jobs: vec![job("test")],
                    },
                    selection,
                };
                let selection = queue::JobSelection {
                    post_merge_jobs: Vec::new(),
                    bases: vec![],
                    skipped_jobs: vec!["test".into()],
                };
                queue::register(w, reg("head", Some(selection)), 1, "2026-10-27T00:00:00Z")?;
                // Fill the active-class budget; deferred work no longer blocks main.
                let mut busy = reg("busy", None);
                busy.branch = "main".into();
                busy.protected = true;
                queue::register(w, busy, 1, "2026-10-27T00:00:00Z")?;
                let mut all = reg("head", None);
                all.protected = true;
                all.branch = "main".into();
                queue::register(w, all, 1, "2026-10-27T00:00:01Z")?;
                let run = queue::get_run(w, &id, "head")?.unwrap();
                assert!(run.waiting);
                Ok(())
            })
            .await
            .unwrap();
        let report = ci.head_checks(&repo, "head").await.unwrap();
        assert_eq!(report.checks[0].status, "queued");
        assert!(report.checks[0].path_skip.is_none());
        assert!(!report.checks[0].head_satisfied());
    }

    #[tokio::test]
    async fn a_paths_reuse_is_pending_while_its_protected_run_waits() {
        let (_d, ci, repo) = setup();
        let (id, path) = (repo.id.clone(), repo.path.clone());
        let saved = repo.clone();
        ci.inner
            .store
            .write(move |w| {
                w.put(store::REPOS, &Repo::key(&saved.id), &saved)?;
                let reg = |sha: &str, branch: &str| Registration {
                    order: Vec::new(),
                    inputs: BTreeMap::from([("test".to_string(), "d1".to_string())]),
                    repo: id.clone(),
                    repo_path: path.clone(),
                    sha: sha.into(),
                    tree: Some(format!("tree-{sha}")),
                    branch: branch.into(),
                    protected: branch == "main",
                    network: false,
                    pipeline: PipelineState::Jobs {
                        jobs: vec![JobDef {
                            verified_sha: false,
                            paths: vec!["src/**".into()],
                            ..job("test")
                        }],
                    },
                    selection: None,
                };
                let now = "2026-10-27T00:00:00Z";
                let digest =
                    "sha256:2222222222222222222222222222222222222222222222222222222222222222";
                let mut source = None;
                for sha in ["source", "head"] {
                    queue::register(w, reg(sha, "feature"), 1, now)?;
                    let c = queue::claim(w, "r", 60, now)?.unwrap();
                    let info = queue::ResultInfo {
                        image_digest: Some(digest.into()),
                        reused_from: source.take(),
                        ..Default::default()
                    };
                    let done =
                        queue::finish(w, &c.attempt.id, &c.lease, "success", None, info, now)?;
                    source = Some(done.id);
                }
                queue::register(w, reg("busy", "main"), 1, now)?;
                queue::register(w, reg("head", "main"), 1, now)?;
                assert!(queue::get_run(w, &id, "head")?.unwrap().waiting);
                Ok(())
            })
            .await
            .unwrap();
        let report = ci.head_checks(&repo, "head").await.unwrap();
        assert_eq!(report.checks[0].status, "queued", "{:?}", report.checks[0]);
        assert!(report.checks[0].reused_from.is_none());
    }

    /// A service over `ci`'s store, for tests that register through `App`.
    pub(super) fn app(temp: &tempfile::TempDir, ci: &Ci) -> crate::app::Shared {
        ci.inner.layout.create().unwrap();
        let config = crate::config::Config::load(None, temp.path()).unwrap();
        App::new(
            config,
            "http://127.0.0.1:7878".into(),
            ci.inner.store.clone(),
            std::sync::Arc::new(crate::vault::Memory::default()),
            "http://127.0.0.1:7878".into(),
        )
    }

    /// Commits everything in the work tree `work` and returns the commit.
    pub(super) async fn commit_all(git: &Git, work: &std::path::Path, message: &str) -> String {
        let work = work.to_str().unwrap();
        git.run_ok(None, &["-C", work, "add", "-A"]).await.unwrap();
        let out = git
            .run_env(
                None,
                &["-C", work, "commit", "--allow-empty", "-qm", message],
                &[
                    ("GIT_AUTHOR_NAME", "Test"),
                    ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
                    ("GIT_COMMITTER_NAME", "Test"),
                    ("GIT_COMMITTER_EMAIL", "test@example.invalid"),
                ],
            )
            .await
            .unwrap();
        assert!(out.ok(), "{}", out.stderr);
        git.run_ok(None, &["-C", work, "rev-parse", "HEAD"])
            .await
            .unwrap()
    }

    /// An open pull request of `repo` from `head` into `base`.
    pub(super) fn open_pull(repo: &str, number: u64, head: &str, base: &str) -> crate::model::Pull {
        crate::model::Pull {
            repo: repo.into(),
            number,
            title: "Gate ownership".into(),
            body: String::new(),
            state: "open".into(),
            merged: false,
            draft: false,
            author: "member1".into(),
            head: head.into(),
            base: base.into(),
            reviewers: vec![],
            tickets: vec![],
            labels: vec![],
            reviewed_by: vec![],
            ready_at: None,
            created_at: util::now(),
            updated_at: util::now(),
            closed_at: None,
            merged_at: None,
            merge_sha: None,
            merged_head: None,
            merged_base: None,
            upstream: None,
            revision: 1,
        }
    }

    pub(super) fn put_pull(w: &store::Wtx, pull: &crate::model::Pull) -> AppResult<()> {
        crate::model::put_indexed(
            w,
            store::PULLS,
            &crate::model::Pull::key(&pull.repo, pull.number),
            None,
            pull,
            crate::model::pull_index(pull),
        )
    }

    /// The input digest by its definition, independently of `Ci::job_inputs`.
    pub(super) async fn raw_digest(
        git: &Git,
        dir: &std::path::Path,
        sha: &str,
        paths: &[&str],
    ) -> String {
        let specs: Vec<String> = paths.iter().map(|p| format!(":(glob){p}")).collect();
        let empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
        let mut args = vec![
            "diff-tree",
            "-r",
            "-z",
            "--no-renames",
            "--raw",
            empty_tree,
            sha,
            "--",
        ];
        args.extend(specs.iter().map(String::as_str));
        let out = git.run(Some(dir), &args).await.unwrap();
        assert!(out.ok() && !out.stdout.is_empty(), "{}", out.stderr);
        util::hex(&util::sha256(&out.stdout))
    }

    /// A pipeline of `lint` with `paths` and `test` without.
    pub(super) fn inputs_pipeline(paths: &str) -> String {
        format!(
            "[[jobs]]\nname = 'lint'\nimage = 'alpine'\ncommands = ['true']\npaths = [{paths}]\n\n\
             [[jobs]]\nname = 'test'\nimage = 'alpine'\ncommands = ['true']\n"
        )
    }

    #[tokio::test]
    async fn registrations_record_the_inputs_of_the_jobs_with_paths() {
        let (temp, ci, repo) = setup();
        let app = app(&temp, &ci);
        let (saved, pull) = (repo.clone(), open_pull(&repo.id, 1, "feature", "main"));
        app.store
            .write(move |w| {
                w.put(store::REPOS, &Repo::key(&saved.id), &saved)?;
                put_pull(w, &pull)
            })
            .await
            .unwrap();
        let dir = app.git.repo_dir(&repo.path);
        app.git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        let w = work.to_str().unwrap();
        app.git
            .run_ok(None, &["init", "-q", "-b", "main", w])
            .await
            .unwrap();
        std::fs::create_dir_all(work.join("src")).unwrap();
        std::fs::write(
            work.join(".guildbotics-ci.toml"),
            inputs_pipeline("'src/**'"),
        )
        .unwrap();
        std::fs::write(work.join("src/a.txt"), "a").unwrap();
        std::fs::write(work.join("src/b.txt"), "b").unwrap();
        let main = commit_all(&app.git, &work, "main").await;
        std::fs::write(work.join("src/a.txt"), "changed").unwrap();
        let head = commit_all(&app.git, &work, "head").await;
        let push = |refspecs: Vec<String>| {
            let (git, dir) = (app.git.clone(), dir.clone());
            async move {
                let mut args = vec!["-C", w, "push", "-q", dir.to_str().unwrap()];
                args.extend(refspecs.iter().map(String::as_str));
                git.run_ok(None, &args).await.unwrap();
            }
        };
        push(vec![
            format!("{main}:refs/heads/main"),
            format!("{head}:refs/heads/feature"),
        ])
        .await;
        let run = |sha: &str| {
            let (id, sha) = (repo.id.clone(), sha.to_string());
            let store = app.store.clone();
            async move {
                store
                    .read(move |r| queue::get_run(r, &id, &sha))
                    .await
                    .unwrap()
                    .unwrap()
            }
        };
        let lint = |digest: String| BTreeMap::from([("lint".to_string(), digest)]);
        // The default branch and the pull request's head.
        assert_eq!(app.ci.reconcile(&app).await.unwrap(), 2);
        let expected = lint(raw_digest(&app.git, &dir, &head, &["src/**"]).await);
        assert_eq!(
            run(&main).await.inputs,
            lint(raw_digest(&app.git, &dir, &main, &["src/**"]).await)
        );
        assert_eq!(run(&head).await.inputs, expected);
        assert_ne!(run(&main).await.inputs, expected);
        // An upgraded run has none until the reconciler registers it again;
        // a run read with other paths gets the digest of the commit's own.
        for stale in [BTreeMap::new(), lint("stale".into())] {
            let mut old = run(&head).await;
            old.inputs = stale.clone();
            if !stale.is_empty()
                && let PipelineState::Jobs { jobs } = &mut old.pipeline
            {
                jobs[0].paths = vec!["other/**".into()];
            }
            app.store
                .write(move |w| queue::put_run(w, &old))
                .await
                .unwrap();
            assert_eq!(app.ci.reconcile(&app).await.unwrap(), 1);
            assert_eq!(run(&head).await.inputs, expected);
        }
        // A commit that changes the job's paths has the digest of its paths.
        std::fs::write(
            work.join(".guildbotics-ci.toml"),
            inputs_pipeline("'src/a.txt'"),
        )
        .unwrap();
        let next = commit_all(&app.git, &work, "narrower paths").await;
        push(vec![format!("{next}:refs/heads/feature")]).await;
        assert_eq!(app.ci.reconcile(&app).await.unwrap(), 1);
        let narrow = raw_digest(&app.git, &dir, &next, &["src/a.txt"]).await;
        assert_ne!(narrow, raw_digest(&app.git, &dir, &next, &["src/**"]).await);
        assert_eq!(run(&next).await.inputs, lint(narrow));
    }

    #[tokio::test]
    async fn hidden_gate_refs_reconcile_exact_current_gates_and_supersede_on_move_or_delete() {
        let (temp, ci, mut repo) = setup();
        let app = app(&temp, &ci);
        // Even an explicit protection pattern cannot make a hidden gate save caches.
        repo.protected_branches = vec!["gate/7".into()];
        let saved_repo = repo.clone();
        app.store
            .write(move |w| w.put(store::REPOS, &Repo::key(&saved_repo.id), &saved_repo))
            .await
            .unwrap();
        let dir = app.git.repo_dir(&repo.path);
        app.git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        app.git
            .run_ok(
                None,
                &["init", "-q", "-b", "feature", work.to_str().unwrap()],
            )
            .await
            .unwrap();
        std::fs::write(
            work.join(".guildbotics-ci.toml"),
            "[[jobs]]\nname = 'test'\nimage = 'alpine'\ncommands = ['true']\n",
        )
        .unwrap();
        let mut shas = Vec::new();
        for message in ["first", "second"] {
            shas.push(commit_all(&app.git, &work, message).await);
        }
        app.git
            .run_ok(
                None,
                &[
                    "-C",
                    work.to_str().unwrap(),
                    "push",
                    dir.to_str().unwrap(),
                    "HEAD:refs/heads/staging",
                ],
            )
            .await
            .unwrap();
        assert!(
            app.git
                .update_ref(
                    &dir,
                    "refs/branvil/gate/7",
                    &shas[0],
                    ZERO_SHA,
                    "create gate"
                )
                .await
                .unwrap()
        );
        assert!(
            app.git
                .update_ref(
                    &dir,
                    "refs/heads/staging",
                    ZERO_SHA,
                    &shas[1],
                    "remove staging"
                )
                .await
                .unwrap()
        );
        assert_eq!(
            app.ci.reconcile(&app).await.unwrap(),
            0,
            "unowned hidden ref is not CI"
        );
        let (id, sha) = (repo.id.clone(), shas[0].clone());
        assert!(
            app.store
                .read(move |r| queue::get_run(r, &id, &sha))
                .await
                .unwrap()
                .is_none()
        );
    }

    #[tokio::test]
    async fn a_commit_without_a_run_is_pending_never_no_checks() {
        let (_d, ci, mut repo) = setup();
        let c = ci.head_checks(&repo, "s1").await.unwrap();
        assert!(c.checks_expected);
        assert_eq!(c.checks.len(), 1);
        assert_eq!(c.checks[0].status, "queued");
        assert!(!c.checks[0].recorded() && !c.checks[0].succeeded());
        assert!(
            c.checks[0]
                .details
                .as_deref()
                .unwrap()
                .contains("No CI run")
        );
        // Without required checks, a commit Git cannot read (there is no
        // repository here) is still expected, never "no checks" (a commit
        // without a pipeline: `tests/pulls.rs`).
        repo.required_checks.clear();
        let c = ci.head_checks(&repo, "s1").await.unwrap();
        assert!(c.checks_expected && c.checks.is_empty());
    }

    #[tokio::test]
    async fn a_run_takes_its_commits_pipeline_and_counts_only_its_jobs() {
        let (_d, ci, repo) = setup();
        let reg = |jobs: Vec<JobDef>| Registration {
            order: Vec::new(),
            inputs: Default::default(),
            selection: None,
            repo: repo.id.clone(),
            repo_path: repo.path.clone(),
            sha: "s1".into(),
            tree: Some("tree-s1".into()),
            branch: "feature".into(),
            protected: false,
            pipeline: PipelineState::Jobs { jobs },
            network: false,
        };
        let run = |r: Registration| {
            let now = util::now();
            ci.inner
                .store
                .write(move |w| queue::register(w, r, 10, &now))
        };
        let succeed = || {
            ci.inner.store.write(|w| {
                let now = util::now();
                let c = queue::claim(w, "r", 60, &now)?.unwrap();
                queue::finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "success",
                    None,
                    queue::ResultInfo::default(),
                    &now,
                )
            })
        };
        let test = |c: &Checks| c.checks.iter().find(|x| x.name == "test").cloned().unwrap();
        // Recorded with another pipeline (a base's), and its test succeeded.
        assert!(run(reg(vec![job("test")])).await.unwrap());
        succeed().await.unwrap();
        assert!(test(&ci.head_checks(&repo, "s1").await.unwrap()).succeeded());
        // The commit's own pipeline has no test: the old success is not a check.
        assert!(run(reg(vec![job("lint")])).await.unwrap());
        let c = ci.head_checks(&repo, "s1").await.unwrap();
        let t = test(&c);
        assert_eq!(t.conclusion.as_deref(), Some("failure"));
        assert!(!t.recorded(), "{t:?}");
        assert!(c.checks.iter().any(|x| x.name == "lint" && x.recorded()));
        // Its test is another definition: it runs again, and only that counts.
        let mut changed = job("test");
        changed.commands = vec!["echo changed".into()];
        assert!(run(reg(vec![changed.clone(), job("lint")])).await.unwrap());
        let t = test(&ci.head_checks(&repo, "s1").await.unwrap());
        assert_eq!((t.status.as_str(), t.attempt), ("queued", Some(2)));
        // The same pipeline again changes nothing.
        assert!(!run(reg(vec![changed, job("lint")])).await.unwrap());
    }

    #[tokio::test]
    async fn waiting_runs_and_missing_jobs_show_why() {
        let (_d, ci, repo) = setup();
        let now = util::now();
        let reg = |sha: &str, jobs: Vec<JobDef>| Registration {
            order: Vec::new(),
            inputs: Default::default(),
            selection: None,
            repo: repo.id.clone(),
            repo_path: repo.path.clone(),
            sha: sha.into(),
            tree: Some(format!("tree-{sha}")),
            branch: "feature".into(),
            protected: false,
            pipeline: PipelineState::Jobs { jobs },
            network: false,
        };
        let (r1, r2) = (reg("s1", vec![job("lint")]), reg("s2", vec![job("test")]));
        let n = now.clone();
        ci.inner
            .store
            .write(move |w| {
                queue::register(w, r1, 1, &n)?;
                queue::register(w, r2, 1, &n)
            })
            .await
            .unwrap();
        // s1 took the only slot: s2's job waits, visibly.
        let c = ci.head_checks(&repo, "s2").await.unwrap();
        assert_eq!(c.checks.len(), 1);
        assert_eq!(c.checks[0].status, "queued");
        assert!(
            c.checks[0]
                .details
                .as_deref()
                .unwrap()
                .contains("Waiting for room")
        );
        // s1's pipeline has no job named like the required check.
        let c = ci.head_checks(&repo, "s1").await.unwrap();
        let test = c.checks.iter().find(|x| x.name == "test").unwrap();
        assert_eq!(test.conclusion.as_deref(), Some("failure"));
        assert!(
            test.details
                .as_deref()
                .unwrap()
                .contains("defines no job named test")
        );
        let lint = c.checks.iter().find(|x| x.name == "lint").unwrap();
        assert!(lint.recorded());
        assert_eq!(
            lint.details_url.as_deref().unwrap(),
            format!(
                "http://127.0.0.1:7878/acme/demo/-/jobs/{}",
                lint.attempt_id.as_deref().unwrap()
            )
        );
    }
    #[tokio::test]
    async fn verification_batches_main_and_marks_only_completed_success() {
        let (temp, ci, mut repo) = setup();
        repo.required_checks.clear();
        let app = app(&temp, &ci);
        let saved = repo.clone();
        app.store
            .write(move |w| w.put(store::REPOS, &Repo::key(&saved.id), &saved))
            .await
            .unwrap();
        let dir = app.git.repo_dir(&repo.path);
        app.git.init_bare(&dir, "main").await.unwrap();
        let work = temp.path().join("work");
        app.git
            .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
            .await
            .unwrap();
        std::fs::write(work.join(".guildbotics-ci.toml"), "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n\n[[jobs]]\nname='lint'\nimage='alpine'\ncommands=['true']\n").unwrap();
        let mut shas = vec![];
        for message in ["first", "second", "third"] {
            shas.push(commit_all(&app.git, &work, message).await);
        }
        app.git
            .run_ok(
                None,
                &[
                    "-C",
                    work.to_str().unwrap(),
                    "push",
                    "-q",
                    dir.to_str().unwrap(),
                    "HEAD:refs/heads/main",
                ],
            )
            .await
            .unwrap();
        app.git
            .update_ref(&dir, "refs/heads/main", &shas[0], &shas[2], "test")
            .await
            .unwrap();
        crate::refs::record_refs(&app, &repo).await.unwrap();
        assert_eq!(
            ci.inner
                .store
                .read(|r| queue::pending_count(r, 100))
                .await
                .unwrap(),
            2,
            "verification bypasses queue_max=1"
        );
        let first = ci
            .inner
            .store
            .write(|w| queue::claim(w, "runner", 60, &util::now()))
            .await
            .unwrap()
            .unwrap();
        for n in 1..3 {
            app.git
                .update_ref(&dir, "refs/heads/main", &shas[n], &shas[n - 1], "test")
                .await
                .unwrap();
            crate::refs::record_refs(&app, &repo).await.unwrap();
        }
        let id = repo.id.clone();
        let sha = shas[0].clone();
        let run = ci
            .inner
            .store
            .read(move |r| queue::get_run(r, &id, &sha))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(run.branches, vec!["main"]);
        assert!(run.superseded_by.is_none());
        let id = repo.id.clone();
        let sha = shas[2].clone();
        assert!(
            ci.inner
                .store
                .read(move |r| queue::get_run(r, &id, &sha))
                .await
                .unwrap()
                .is_none()
        );
        let second = ci
            .inner
            .store
            .write(|w| queue::claim(w, "runner", 60, &util::now()))
            .await
            .unwrap()
            .unwrap();
        for c in [first, second] {
            ci.inner
                .store
                .write(move |w| {
                    queue::finish(
                        w,
                        &c.attempt.id,
                        &c.lease,
                        "success",
                        None,
                        queue::ResultInfo::default(),
                        &util::now(),
                    )
                })
                .await
                .unwrap();
        }
        // Simulate stopping after the ref was created but before recording success.
        app.git
            .update_ref(
                &dir,
                &format!("refs/branvil/verified/{}", shas[0]),
                &shas[0],
                ZERO_SHA,
                "test",
            )
            .await
            .unwrap();
        ci.advance_verification(&repo, true).await.unwrap();
        let id = repo.id.clone();
        let v = ci
            .inner
            .store
            .read(move |r| verify::get(r, &id))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(v.state, "running");
        assert_eq!(v.sha, shas[2]);
        assert_eq!(v.last_verified.unwrap().sha, shas[0]);
        assert_eq!(v.base, Some(shas[0].clone()));
        let id = repo.id.clone();
        let sha = shas[0].clone();
        assert!(
            ci.inner
                .store
                .read(move |r| queue::get_run(r, &id, &sha))
                .await
                .unwrap()
                .unwrap()
                .branches
                .is_empty()
        );
        // Same tree and no cache: the next verification can reuse the actual successes.
        for _ in 0..2 {
            let c = ci
                .inner
                .store
                .write(|w| queue::claim(w, "runner", 60, &util::now()))
                .await
                .unwrap()
                .unwrap();
            ci.inner
                .store
                .write(move |w| {
                    queue::finish(
                        w,
                        &c.attempt.id,
                        &c.lease,
                        "success",
                        None,
                        queue::ResultInfo::default(),
                        &util::now(),
                    )
                })
                .await
                .unwrap();
        }
        ci.advance_verification(&repo, true).await.unwrap();
        let id = repo.id.clone();
        let v = ci
            .inner
            .store
            .read(move |r| verify::get(r, &id))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(v.state, "passed");
        assert_eq!(v.last_verified.unwrap().sha, shas[2]);
        assert_eq!(ci.advance_verification(&repo, true).await.unwrap(), 0);
    }
    #[tokio::test]
    async fn verification_finishes_invalid_missing_empty_and_exhausted_failures() {
        for kind in ["absent", "invalid", "missing", "failed", "recovered"] {
            let (temp, ci, mut repo) = setup();
            if kind != "missing" {
                repo.required_checks.clear();
            }
            let app = app(&temp, &ci);
            let saved = repo.clone();
            app.store
                .write(move |w| w.put(store::REPOS, &Repo::key(&saved.id), &saved))
                .await
                .unwrap();
            let dir = app.git.repo_dir(&repo.path);
            app.git.init_bare(&dir, "main").await.unwrap();
            let work = temp.path().join("work");
            app.git
                .run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
                .await
                .unwrap();
            if kind != "absent" {
                let pipeline = if kind == "invalid" {
                    "invalid"
                } else {
                    "[[jobs]]\nname='lint'\nimage='alpine'\ncommands=['true']\n"
                };
                std::fs::write(work.join(".guildbotics-ci.toml"), pipeline).unwrap();
            }
            let sha = commit_all(&app.git, &work, "verification").await;
            app.git
                .run_ok(
                    None,
                    &[
                        "-C",
                        work.to_str().unwrap(),
                        "push",
                        "-q",
                        dir.to_str().unwrap(),
                        "HEAD:refs/heads/main",
                    ],
                )
                .await
                .unwrap();
            ci.advance_verification(&repo, true).await.unwrap();
            if !matches!(kind, "absent" | "invalid") {
                for n in 0..=queue::MAX_AUTO_RETRIES {
                    let c = ci
                        .inner
                        .store
                        .write(|w| queue::claim(w, "r", 60, &util::now()))
                        .await
                        .unwrap()
                        .unwrap();
                    let conclusion = if kind == "missing" || (kind == "recovered" && n == 1) {
                        "success"
                    } else {
                        "failure"
                    };
                    ci.inner
                        .store
                        .write(move |w| {
                            queue::finish(
                                w,
                                &c.attempt.id,
                                &c.lease,
                                conclusion,
                                None,
                                queue::ResultInfo::default(),
                                &util::now(),
                            )
                        })
                        .await
                        .unwrap();
                    ci.advance_verification(&repo, true).await.unwrap();
                    let id = repo.id.clone();
                    let v = ci
                        .inner
                        .store
                        .read(move |r| verify::get(r, &id))
                        .await
                        .unwrap()
                        .unwrap();
                    let state = verify::main_state(&app, &repo).await.unwrap();
                    assert_eq!(
                        state.latest_verified.is_some(),
                        conclusion == "success" && kind == "recovered"
                    );
                    if conclusion == "success" {
                        break;
                    }
                    assert_eq!(
                        state.current.unwrap().retry.number,
                        (n + 1).min(queue::MAX_AUTO_RETRIES)
                    );
                    assert_eq!(
                        state.state,
                        if n < queue::MAX_AUTO_RETRIES {
                            "verifying"
                        } else {
                            "failing"
                        }
                    );
                    assert_eq!(
                        v.state,
                        if n < queue::MAX_AUTO_RETRIES {
                            "running"
                        } else {
                            "failed"
                        }
                    );
                }
            }
            ci.advance_verification(&repo, true).await.unwrap();
            let id = repo.id.clone();
            let v = ci
                .inner
                .store
                .read(move |r| verify::get(r, &id))
                .await
                .unwrap()
                .unwrap();
            let passed = matches!(kind, "absent" | "recovered");
            assert_eq!(v.state, if passed { "passed" } else { "failed" });
            if passed {
                assert!(v.failing_since.is_none());
                assert_eq!(v.last_verified.unwrap().sha, sha);
            } else {
                assert_eq!(v.failing_since, Some(sha.clone()));
                assert!(!v.failed_jobs.is_empty());
                assert_eq!(v.failed_jobs[0].attempt_id.is_some(), kind == "failed");
            }
            let refs = app.git.all_refs(&dir).await.unwrap();
            assert_eq!(
                refs.contains_key(&format!("refs/branvil/verified/{sha}")),
                passed
            );
        }
    }
}
