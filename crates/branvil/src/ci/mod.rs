//! CI. The merge path, the checks endpoint, and ref-change handling talk to CI
//! only through `Ci`; the runner talks to it through the runner API
//! (`api.rs`). See `docs/ci.md`.
//!
//! - The pipeline (`.branvil/ci.toml`) is read from the commit itself, for a
//!   tip of the default branch, a pull request head, and a landing gate alike. What a head
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
pub mod notify;
pub mod pipeline;
pub mod queue;
mod selection;
pub mod settings;
pub mod supervise;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::app::App;
use crate::config::Layout;
use crate::error::AppResult;
use crate::events::Events;
use crate::git::{Git, ZERO_SHA};
use crate::model::Repo;
use crate::refs::RefChange;
use crate::store::{self, Key, Rd, Store};
use crate::util;
use config::CiConfig;
use queue::{Attempt, PipelineState, Registration};

/// How many commits of the first-parent history `Ci::estimate` looks at.
const ESTIMATE_DEPTH: usize = 20;

/// Where a reused result was actually executed (including its log and artifacts).
#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct ReusedFrom {
    pub attempt_id: String,
    pub url: String,
    pub sha: String,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details_url: Option<String>,
    /// Why the check is where it is (a failure reason, "superseded", "no
    /// runner", "waiting for room in the queue", ...).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub details: Option<String>,
    pub artifact_names: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reused_from: Option<ReusedFrom>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cache_refresh: Option<String>,
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

    /// Head admission accepts a service selection, while gates require success.
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
#[derive(Serialize, Clone, Debug)]
pub struct RunnerSeen {
    pub name: String,
    pub last_seen_at: String,
    pub executor: String,
    pub executor_state: String,
    pub executor_detail: Option<String>,
    #[serde(skip)]
    seen: Option<Instant>,
}

#[derive(Clone)]
pub struct Ci {
    inner: Arc<Inner>,
}

struct Inner {
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

    pub async fn needs_gate_commit(&self, repo: &Repo, sha: &str) -> AppResult<bool> {
        let (id, sha) = (repo.id.clone(), sha.to_string());
        self.inner
            .store
            .read(move |r| {
                Ok(queue::get_run(r, &id, &sha)?
                    .and_then(|r| r.selection)
                    .is_some_and(|s| !s.skipped_jobs.is_empty()))
            })
            .await
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

    pub fn reused_from(&self, a: &Attempt, source: &Attempt) -> Option<ReusedFrom> {
        a.reused_from.as_ref().map(|id| ReusedFrom {
            attempt_id: id.clone(),
            url: self.job_url(&a.repo_path, id),
            sha: source.sha.clone(),
        })
    }

    fn check_of(&self, a: &Attempt, source: &Attempt) -> Check {
        Check {
            path_skip: a.path_skip.clone(),
            name: a.job.name.clone(),
            status: a.status.clone(),
            conclusion: a.conclusion.clone(),
            attempt_id: Some(a.id.clone()),
            attempt: Some(a.number),
            details_url: Some(self.job_url(&a.repo_path, &a.id)),
            details: a
                .cache_refresh
                .as_ref()
                .map(|id| {
                    format!(
                        "Reused the result of {}; cache refresh: {}",
                        self.job_url(&a.repo_path, &source.id),
                        self.job_url(&a.repo_path, id)
                    )
                })
                .or_else(|| a.display_details()),
            artifact_names: if source.files_pruned {
                Vec::new()
            } else {
                source.artifacts.iter().map(|x| x.name.clone()).collect()
            },
            reused_from: self.reused_from(a, source),
            cache_refresh: a.cache_refresh.clone(),
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
        let dir = self.inner.git.repo_dir(&repo.path);
        let depth = format!("--max-count={ESTIMATE_DEPTH}");
        let ancestors = match self
            .inner
            .git
            .run_ok(Some(&dir), &["rev-list", "--first-parent", &depth, sha])
            .await
        {
            Ok(out) => out.lines().map(str::to_string).collect::<Vec<_>>(),
            Err(e) => {
                tracing::warn!("CI: cannot list the ancestors of {sha} for estimates: {e}");
                return;
            }
        };
        let id = repo.id.clone();
        let took = self
            .inner
            .store
            .read(move |r| {
                let mut took = BTreeMap::new();
                for sha in &ancestors {
                    for a in queue::attempts_of(r, &id, sha)?.into_iter().rev() {
                        if a.conclusion.as_deref() == Some("success")
                            && a.reused_from.is_none()
                            && let Some(secs) = queue::duration_secs(&a)
                        {
                            took.entry(a.job.name).or_insert(secs);
                        }
                    }
                }
                Ok(took)
            })
            .await;
        match took {
            Ok(took) => {
                for c in checks.iter_mut().filter(|c| running(c)) {
                    c.expected_secs = took.get(&c.name).copied();
                }
            }
            Err(e) => tracing::warn!("CI: cannot read the attempts for estimates: {e}"),
        }
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
        let (run, latest) = self
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
                let latest = latest
                    .into_iter()
                    .map(|a| {
                        let source = queue::result_attempt(r, &a)?;
                        Ok((a, source))
                    })
                    .collect::<AppResult<Vec<_>>>()?;
                Ok((run, latest))
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
        self.run_checks(repo, &run, latest)
    }

    /// Checks of a registered run, shared with required-failure notification.
    fn run_checks(
        &self,
        repo: &Repo,
        run: &queue::Run,
        latest: Vec<(Attempt, Attempt)>,
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
            checks.push(self.check_of(a, source));
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
                            self.inner.config.queue_max
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
            checks_expected: run.pipeline != PipelineState::Absent
                || !repo.required_checks.is_empty(),
        })
    }

    /// Reads the pipeline of commit `sha` (`pipeline::read`).
    async fn read_pipeline(&self, repo: &Repo, sha: &str) -> AppResult<PipelineState> {
        let dir = self.inner.git.repo_dir(&repo.path);
        pipeline::read(&self.inner.git, &dir, sha).await
    }

    /// Registers CI of `sha` for `branch` when the branch wants CI: it is the
    /// default branch, the head of an open pull request, or a current landing
    /// gate. The pipeline is the commit's own. The caller holds the repository lock.
    async fn consider(&self, repo: &Repo, refname: &str, sha: &str) -> AppResult<bool> {
        let Some((branch, gate)) = ci_ref(refname) else {
            return Ok(false);
        };
        if let Some(number) = gate {
            let (id, sha) = (repo.id.clone(), sha.to_string());
            if !self
                .inner
                .store
                .read(move |r| {
                    Ok(
                        crate::landing::current(r, &id, number)?.is_some_and(|entry| {
                            matches!(entry.state.as_str(), "gating" | "passed")
                                && entry.gate.as_deref() == Some(sha.as_str())
                                && entry.head_sha != sha
                        }),
                    )
                })
                .await?
            {
                return Ok(false);
            }
        } else if branch != repo.default_branch {
            let (id, b) = (repo.id.clone(), branch.to_string());
            if !self
                .inner
                .store
                .read(move |r| has_open_pull(r, &id, &b))
                .await?
            {
                return Ok(false);
            }
        }
        let pipeline = self.read_pipeline(repo, sha).await?;
        let selection = self
            .select_jobs(
                repo,
                sha,
                &pipeline,
                gate.is_some() || repo.is_protected(branch),
            )
            .await?;
        let reg = Registration {
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
            protected: gate.is_none() && repo.is_protected(branch),
            pipeline,
            network: repo.ci_network,
        };
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
    /// of the default branch, pull request heads, and current gates get a run.
    pub async fn on_ref_changes(&self, repo: &Repo, changes: &[RefChange]) {
        if let Err(e) = self.apply_ref_changes(repo, changes).await {
            // The reconciler registers what this missed.
            tracing::error!("CI registration for {} failed: {e}", repo.path);
        }
    }

    async fn apply_ref_changes(&self, repo: &Repo, changes: &[RefChange]) -> AppResult<()> {
        let mut left = false;
        for c in changes {
            let Some((branch, _)) = ci_ref(&c.refname) else {
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
        if left {
            self.admit_waiting().await;
        }
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
    /// head, and every current gate again: a run that is missing (a crash between a push and
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
            registered += app
                .locked(&id, move |app| async move {
                    app.ci.reconcile_locked(&repo).await
                })
                .await?;
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
                let mut wanted: Vec<String> = open_pull_heads(r, &id)?
                    .into_iter()
                    .map(|head| format!("refs/heads/{head}"))
                    .collect();
                for entry in crate::landing::active(r, &id)? {
                    if entry
                        .gate
                        .as_ref()
                        .is_some_and(|sha| sha != &entry.head_sha)
                    {
                        wanted.push(format!("refs/branvil/gate/{}", entry.number));
                    }
                }
                Ok(wanted)
            })
            .await?;
        wanted.push(format!("refs/heads/{}", repo.default_branch));
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
        match self
            .inner
            .store
            .write(move |w| queue::admit_waiting(w, queue_max, &now))
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
                    .filter_map(|r| r.seen)
                    .max()
                    .unwrap_or(self.inner.started)
                    .max(self.inner.started),
                runners.values().map(|r| r.last_seen_at.clone()).max(),
            )
        };
        if last_contact.elapsed() > Duration::from_secs(absent) {
            let details = format!(
                "No CI runner has contacted Branvil for {absent} s (last contact: {}). Start `branvil runner` (docs/ci.md)",
                last_text.unwrap_or_else(|| "none since Branvil started".into())
            );
            let now = util::now();
            let before = queue::earlier(&now, absent);
            let changed = self
                .inner
                .store
                .write(move |w| queue::fail_queued(w, &before, &details, &now))
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
    /// and interrupts cache refreshes immediately. It waits for checks, at most
    /// `ci.stop_wait_secs`. The checks
    /// still running then are interrupted and queued again in their place,
    /// ahead of the jobs queued meanwhile (which run after the next start);
    /// it returns how many.
    pub async fn stop(&self) -> AppResult<usize> {
        let wait = self.inner.config.stop_wait_secs;
        let deadline = Instant::now() + Duration::from_secs(wait);
        let _ = self.inner.stopping.set(queue::later(&util::now(), wait));
        // The first count is a write, so it comes after every claim that did
        // not see the service stopping; no claim succeeds after it.
        let store = &self.inner.store;
        let now = util::now();
        let (changed, mut running) = store
            .write(move |w| {
                let changed = queue::interrupt_refreshes(w, &now)?;
                Ok((changed, queue::counts(w)?.1))
            })
            .await?;
        self.publish(&changed);
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
                // scripts/serve.sh reads the first one, as it reads the line
                // that says the service is ready; nobody may read it now.
                if told == 0 {
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

    /// Forgets a runner that was stopped, so the diagnostics stop counting it.
    pub fn forget_runner(&self, name: &str) {
        self.inner.runners.lock().unwrap().remove(&util::fold(name));
    }

    /// Notes a runner request (for diagnostics and the no-runner rule).
    pub fn saw_runner(&self, name: &str, executor: Option<(String, String, Option<String>)>) {
        let mut runners = self.inner.runners.lock().unwrap();
        let entry = runners
            .entry(util::fold(name))
            .or_insert_with(|| RunnerSeen {
                name: name.to_string(),
                last_seen_at: String::new(),
                executor: String::new(),
                executor_state: "unknown".into(),
                executor_detail: None,
                seen: None,
            });
        entry.last_seen_at = util::now();
        entry.seen = Some(Instant::now());
        if let Some((executor, state, detail)) = executor {
            entry.executor = executor;
            entry.executor_state = state;
            entry.executor_detail = detail;
        }
    }

    /// Diagnostics section for `GET /api/v1/admin/diagnostics`.
    pub async fn diagnostics(&self) -> serde_json::Value {
        let counts = self.inner.store.read(queue::counts).await;
        let runners: Vec<RunnerSeen> = self
            .inner
            .runners
            .lock()
            .unwrap()
            .values()
            .cloned()
            .collect();
        let absent = Duration::from_secs(self.inner.config.runner_absent_secs);
        let live: Vec<&RunnerSeen> = runners
            .iter()
            .filter(|r| r.seen.is_some_and(|s| s.elapsed() <= absent))
            .collect();
        let (state, detail) = if let Some(until) = self.inner.stopping.get() {
            (
                "stopping",
                format!(
                    "Branvil is stopping (an update or a restart): it gives out no job and waits until the running ones finish, at most until {until}; queued jobs run after it starts again."
                ),
            )
        } else if live.is_empty() {
            (
                "no_runner",
                format!(
                    "No runner has contacted Branvil in the last {} s; queued jobs conclude startup_failure.",
                    absent.as_secs()
                ),
            )
        } else if live.iter().any(|r| r.executor_state == "ready") {
            ("ready", format!("{} runner(s) connected.", live.len()))
        } else {
            (
                "executor_unavailable",
                live.iter()
                    .find_map(|r| r.executor_detail.clone())
                    .unwrap_or_else(|| "The runner's executor is not usable.".into()),
            )
        };
        let queue = match counts {
            Ok((queued, running, waiting)) => serde_json::json!({
                "queued": queued, "in_progress": running, "waiting_runs": waiting,
                "limit": self.inner.config.queue_max,
            }),
            Err(e) => serde_json::json!({"error": e.message}),
        };
        serde_json::json!({
            "state": state,
            "detail": detail,
            "mode": self.inner.settings.mode(),
            "runners": runners,
            "queue": queue,
        })
    }

    /// Starts the periodic work: sweep every 5 s, reconcile every minute,
    /// retention every hour, and one orphan-file collection at start.
    pub fn start(app: crate::app::Shared) {
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

/// CI branch names preserve the public branch or identify a hidden gate.
fn ci_ref(refname: &str) -> Option<(&str, Option<u64>)> {
    if let Some(branch) = refname.strip_prefix("refs/heads/") {
        return Some((branch, None));
    }
    let branch = refname.strip_prefix("refs/branvil/")?;
    let number = branch.strip_prefix("gate/")?.parse::<u64>().ok()?;
    (number > 0 && branch == format!("gate/{number}")).then_some((branch, Some(number)))
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ci::pipeline::JobDef;

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
            required_checks: vec!["test".into()],
            required_approvals: 1,
            auto_land: false,
            ci_network: false,
            labels: vec![],
            upstream: None,
            revision: 1,
            created_at: String::new(),
        };
        (dir, ci, repo)
    }

    fn job(name: &str) -> JobDef {
        JobDef {
            paths: Vec::new(),
            name: name.into(),
            image: "alpine".into(),
            commands: vec!["true".into()],
            timeout_secs: None,
            artifacts: vec![],
            cache: None,
        }
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
                    bases: vec![],
                    skipped_jobs: vec!["test".into()],
                };
                queue::register(w, reg("head", Some(selection)), 1, "2026-10-27T00:00:00Z")?;
                queue::register(w, reg("busy", None), 1, "2026-10-27T00:00:00Z")?;
                let mut all = reg("head", None);
                all.protected = true;
                all.branch = "main".into();
                queue::register(w, all, 1, "2026-10-27T00:00:01Z")?;
                let run = queue::get_run(w, &id, "head")?.unwrap();
                assert!(run.waiting);
                assert!(
                    !run.failure_pending,
                    "the old skipped attempt is history, not a protected failure"
                );
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
    async fn stopping_interrupts_cache_work_immediately_and_preserves_checks() {
        let (_d, ci, repo) = setup();
        let now = util::now();
        let id = repo.id.clone();
        let path = repo.path.clone();
        let n = now.clone();
        let (check_id, refresh_id, lease) = ci
            .inner
            .store
            .write(move |w| {
                let mut cached = job("test");
                cached.cache = Some(pipeline::CacheDef {
                    paths: vec!["target".into()],
                    key_files: vec![],
                });
                let reg = |sha: &str| Registration {
                    selection: None,
                    repo: id.clone(),
                    repo_path: path.clone(),
                    sha: sha.into(),
                    tree: Some("tree".into()),
                    branch: "main".into(),
                    protected: true,
                    pipeline: PipelineState::Jobs {
                        jobs: vec![cached.clone()],
                    },
                    network: false,
                };
                let digest = format!("sha256:{}", "a".repeat(64));
                queue::register(w, reg("source"), 10, &n)?;
                let c = queue::claim(w, "r", 60, &n)?.unwrap();
                let source = queue::finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "success",
                    None,
                    queue::ResultInfo {
                        image_digest: Some(digest.clone()),
                        cache_key: Some("a".repeat(64)),
                        ..Default::default()
                    },
                    10,
                    &n,
                )?;
                queue::register(w, reg("s1"), 10, &n)?;
                let c = queue::claim(w, "r", 60, &n)?.unwrap();
                let success = queue::finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "success",
                    None,
                    queue::ResultInfo {
                        image_digest: Some(digest),
                        reused_from: Some(source.id),
                        refresh_cache: true,
                        ..Default::default()
                    },
                    10,
                    &n,
                )?;
                let refresh = queue::claim(w, "r", 60, &n)?.unwrap();
                assert_eq!(success.cache_refresh.as_ref(), Some(&refresh.attempt.id));
                Ok((success.id, refresh.attempt.id, refresh.lease))
            })
            .await
            .unwrap();
        assert!(
            ci.head_checks(&repo, "s1")
                .await
                .unwrap()
                .checks
                .iter()
                .all(Check::succeeded)
        );
        let stopped = tokio::time::timeout(Duration::from_millis(500), ci.stop())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            stopped, 0,
            "routine cache interruption is not a check timeout"
        );
        assert!(ci.stopping());
        assert_eq!(
            ci.head_checks(&repo, "s1").await.unwrap().checks[0]
                .attempt_id
                .as_deref(),
            Some(check_id.as_str())
        );
        ci.inner
            .store
            .read(move |r| {
                assert!(queue::leased(r, &refresh_id, &lease, &now).is_err());
                let attempts = queue::attempts_of(r, &repo.id, "s1")?;
                let queued = attempts.iter().find(|a| a.status == "queued").unwrap();
                assert_eq!(queued.purpose, queue::Purpose::CacheRefresh);
                assert!(queued.after_interrupt);
                Ok(())
            })
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn hidden_gate_refs_reconcile_exact_current_gates_and_supersede_on_move_or_delete() {
        let (temp, ci, mut repo) = setup();
        ci.inner.layout.create().unwrap();
        let config = crate::config::Config::load(None, Some(temp.path()), None, None).unwrap();
        let app = App::new(
            config,
            "http://127.0.0.1:7878".into(),
            ci.inner.store.clone(),
            vec![],
            "http://127.0.0.1:7878".into(),
        );
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
        std::fs::create_dir(work.join(".branvil")).unwrap();
        std::fs::write(
            work.join(".branvil/ci.toml"),
            "[[jobs]]\nname = 'test'\nimage = 'alpine'\ncommands = ['true']\n",
        )
        .unwrap();
        app.git
            .run_ok(None, &["-C", work.to_str().unwrap(), "add", "."])
            .await
            .unwrap();
        let mut shas = Vec::new();
        for message in ["first", "second"] {
            let out = app
                .git
                .run_env(
                    None,
                    &[
                        "-C",
                        work.to_str().unwrap(),
                        "commit",
                        "--allow-empty",
                        "-qm",
                        message,
                    ],
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
            shas.push(
                app.git
                    .run_ok(None, &["-C", work.to_str().unwrap(), "rev-parse", "HEAD"])
                    .await
                    .unwrap(),
            );
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
        let mut entry = crate::model::Landing {
            repo: repo.id.clone(),
            number: 7,
            base: "main".into(),
            queued_at: util::now(),
            sequence: 1,
            state: "gating".into(),
            state_at: util::now(),
            gate: Some(shas[0].clone()),
            base_sha: "base".into(),
            head_sha: "head".into(),
            reason: None,
            notified_at: None,
            notify: false,
            resume_publish: false,
            error: None,
        };
        let saved = entry.clone();
        let pull = crate::model::Pull {
            repo: repo.id.clone(),
            number: entry.number,
            title: "Gate ownership".into(),
            body: String::new(),
            state: "open".into(),
            merged: false,
            draft: false,
            author: "member1".into(),
            head: "feature".into(),
            base: entry.base.clone(),
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
        };
        app.store
            .write(move |w| {
                crate::model::put_indexed(
                    w,
                    store::PULLS,
                    &crate::model::Pull::key(&pull.repo, pull.number),
                    None,
                    &pull,
                    crate::model::pull_index(&pull),
                )?;
                w.put(
                    store::LANDING,
                    &crate::model::Pull::key(&saved.repo, saved.number),
                    &saved,
                )
            })
            .await
            .unwrap();
        // The ref already exists, as after stopping between ref creation and registration.
        assert_eq!(app.ci.reconcile(&app).await.unwrap(), 1);
        let (id, sha) = (repo.id.clone(), shas[0].clone());
        let run = app
            .store
            .read(move |r| queue::get_run(r, &id, &sha))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(run.branches, vec!["gate/7"]);
        assert_eq!(run.cache_scope, "branch:gate/7");
        assert_eq!(run.tree, app.git.tree_oid(&dir, &shas[0]).await.unwrap());
        // Register the existing snapshot before moving the ref so branch_left sees both ends.
        crate::refs::record_refs(&app, &repo).await.unwrap();
        let before =
            serde_json::to_value(app.ci.head_checks(&repo, &shas[0]).await.unwrap().checks)
                .unwrap();
        app.git
            .run_ok(Some(&dir), &["update-ref", "-d", "refs/branvil/gate/7"])
            .await
            .unwrap();
        crate::refs::recover(&app).await.unwrap();
        assert_eq!(
            serde_json::to_value(app.ci.head_checks(&repo, &shas[0]).await.unwrap().checks)
                .unwrap(),
            before,
            "repairing a missing gate ref must not cancel its already registered jobs"
        );
        entry.gate = Some(shas[1].clone());
        let saved = entry.clone();
        app.store
            .write(move |w| {
                w.put(
                    store::LANDING,
                    &crate::model::Pull::key(&saved.repo, saved.number),
                    &saved,
                )
            })
            .await
            .unwrap();
        assert_eq!(
            app.ci.reconcile(&app).await.unwrap(),
            0,
            "the row rejects a stale ref"
        );
        assert!(
            app.git
                .update_ref(
                    &dir,
                    "refs/branvil/gate/7",
                    &shas[1],
                    &shas[0],
                    "gate moved"
                )
                .await
                .unwrap()
        );
        crate::refs::record_refs(&app, &repo).await.unwrap();
        let (id, sha) = (repo.id.clone(), shas[0].clone());
        let old = app
            .store
            .read(move |r| queue::get_run(r, &id, &sha))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(old.superseded_by.as_deref(), Some(shas[1].as_str()));
        assert!(
            app.git
                .update_ref(&dir, "refs/branvil/gate/7", ZERO_SHA, &shas[1], "gate left")
                .await
                .unwrap()
        );
        crate::refs::record_refs(&app, &repo).await.unwrap();
        let (id, sha) = (repo.id.clone(), shas[1].clone());
        let old = app
            .store
            .read(move |r| queue::get_run(r, &id, &sha))
            .await
            .unwrap()
            .unwrap();
        assert!(old.superseded_by.is_some());
        assert_eq!(app.ci.reconcile(&app).await.unwrap(), 0);
        assert!(
            app.git
                .update_ref(
                    &dir,
                    "refs/branvil/failed-gate/7",
                    &shas[1],
                    ZERO_SHA,
                    "keep failure"
                )
                .await
                .unwrap()
        );
        crate::refs::record_refs(&app, &repo).await.unwrap();
        assert_eq!(
            app.ci.reconcile(&app).await.unwrap(),
            0,
            "retained failures never get registered"
        );

        // No background tasks run in this fixture. Simulate stopping after
        // saving an owned gate in redb, but before its ref is made reachable.
        assert!(
            app.git
                .update_ref(
                    &dir,
                    "refs/branvil/failed-gate/7",
                    ZERO_SHA,
                    &shas[1],
                    "remove retained fixture ref"
                )
                .await
                .unwrap()
        );
        assert!(
            !app.git
                .all_refs(&dir)
                .await
                .unwrap()
                .contains_key("refs/branvil/gate/7")
        );
        crate::refs::recover(&app).await.unwrap();
        assert_eq!(
            app.git
                .all_refs(&dir)
                .await
                .unwrap()
                .get("refs/branvil/gate/7"),
            Some(&shas[1]),
            "startup recovery must restore an owned gate before any background sweep or backup"
        );
        let bundle = temp.path().join("immediate-backup.bundle");
        app.git
            .run_ok(
                Some(&dir),
                &["bundle", "create", bundle.to_str().unwrap(), "--all"],
            )
            .await
            .unwrap();
        let heads = app
            .git
            .run_ok(
                Some(&dir),
                &["bundle", "list-heads", bundle.to_str().unwrap()],
            )
            .await
            .unwrap();
        assert!(
            heads
                .lines()
                .any(|line| line == format!("{} refs/branvil/gate/7", shas[1])),
            "the immediate backup must carry its recorded gate: {heads}"
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
                    10,
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
}
