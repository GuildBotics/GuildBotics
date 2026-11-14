//! The persistent CI queue: runs (one per repository and commit) and attempts
//! (one per try of a job), and every state change between them. Each public
//! function here is one short write (or read) transaction body; nothing here
//! waits on Git, the network, or a runner.
//!
//! States of an attempt:
//!
//! ```text
//! queued ──claim──▶ in_progress ──finish──▶ completed(conclusion)
//!   │                   │  └─lease expires,─▶ completed(cancelled, "interrupted")
//!   │                   │    service stops     └─(first interruption) new attempt
//!   │                   │                        queued in the old one's place
//!   └─cancel/supersede─▶ completed(cancelled)     (in_progress: cancel_requested,
//!   └─no runner───────▶ completed(startup_failure)  the runner stops and finishes)
//! ```

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use super::pipeline::JobDef;
use crate::error::{AppError, AppResult};
use crate::model::{IxEntry, put_indexed};
use crate::store::{self, Key, Rd, Wtx};
use crate::util;

pub const CONCLUSIONS: [&str; 6] = [
    "success",
    "failure",
    "cancelled",
    "timed_out",
    "skipped",
    "startup_failure",
];

pub const MAX_AUTO_RETRIES: u64 = 3;

const MAX_DETAILS: usize = 4 << 10;

/// What the run's pipeline turned out to be.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum PipelineState {
    /// The commit has no `.branvil/ci.toml`.
    Absent,
    /// It has one and it is invalid.
    Invalid {
        message: String,
    },
    Jobs {
        jobs: Vec<JobDef>,
    },
}

/// The exact PR/base snapshot used to select a commit's jobs.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct SelectionBase {
    pub pull: u64,
    pub base: String,
    pub base_sha: String,
    pub merge_base: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct JobSelection {
    pub bases: Vec<SelectionBase>,
    pub skipped_jobs: Vec<String>,
}

/// A service decision, never a conclusion supplied by a runner.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct PathSkip {
    pub paths: Vec<String>,
    pub bases: Vec<SelectionBase>,
}

impl PathSkip {
    pub fn reason(&self) -> String {
        format!(
            "no file matching paths changed since {}",
            self.bases
                .iter()
                .map(|b| format!(
                    "{} (!{} base {} at {})",
                    b.merge_base.as_deref().unwrap_or("unknown"),
                    b.pull,
                    b.base,
                    b.base_sha
                ))
                .collect::<Vec<_>>()
                .join("; ")
        )
    }
}

/// The CI of one commit of one repository.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Run {
    pub selection: Option<JobSelection>,
    pub repo: String,
    pub repo_path: String,
    pub sha: String,
    /// The commit's tree. None only for upgraded runs whose commit is no
    /// longer available in Git; those runs cannot supply reusable results.
    pub tree: Option<String>,
    pub seq: u64,
    pub created_at: String,
    /// Branches whose tip this commit is and that asked for CI of it. When
    /// the last one moves away, the run is superseded.
    pub branches: Vec<String>,
    /// The commit's own pipeline.
    pub pipeline: PipelineState,
    /// `protected` (a protected branch asked) or `branch:<name>`; only the
    /// jobs of a `protected` run save the dependency cache.
    pub cache_scope: String,
    /// The jobs did not fit into the queue yet; admitted in order when room
    /// frees up.
    pub waiting: bool,
    pub superseded_by: Option<String>,
    /// Protected branches that asked for this run, retained when they move.
    pub protected_branches: Vec<String>,
    /// Successful delivery of this run's required-check failure notice.
    pub failure_notified_at: Option<String>,
    /// The input digest of every job of the pipeline with paths, by job
    /// name, as of the latest registration (`Ci::job_inputs`).
    pub inputs: BTreeMap<String, String>,
    /// A final required-check failure awaits delivery (maintained with every
    /// run/attempt change, so sweeps never scan successful history).
    pub failure_pending: bool,
}

impl Run {
    pub fn skips(&self, name: &str) -> bool {
        self.selection
            .as_ref()
            .is_some_and(|s| s.skipped_jobs.iter().any(|n| n == name))
    }

    /// A previous selection is history once this job must actually execute.
    pub fn includes(&self, a: &Attempt) -> bool {
        a.path_skip.is_none() || self.skips(&a.job.name)
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct ArtifactMeta {
    pub name: String,
    pub bytes: u64,
}

/// Why the job is running; only checks affect commit readiness.
#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Purpose {
    Check,
    CacheRefresh,
}

/// An automatic retry belongs to the first failed attempt's retry budget.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct AutoRetry {
    pub root_attempt: String,
    pub number: u64,
}

/// One try of one job.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Attempt {
    pub path_skip: Option<PathSkip>,
    pub id: String,
    pub purpose: Purpose,
    /// The refresh requested by this reused check, including an existing one.
    pub cache_refresh: Option<String>,
    pub auto_retry: Option<AutoRetry>,
    pub recovery_notified_at: Option<String>,
    /// An absence retry stays queued until a runner claims it.
    pub wait_for_runner: bool,
    pub repo: String,
    pub repo_path: String,
    pub sha: String,
    pub job: JobDef,
    /// The configuration digest of the image the job ran on, as its runner
    /// reported it (none when it did not get that far).
    pub image_digest: Option<String>,
    /// The actual execution whose result this attempt reused, reported at finish.
    pub reused_from: Option<String>,
    /// The computed dependency-cache key; while a refresh is unfinished,
    /// the key whose save it was requested to refresh.
    pub cache_key: Option<String>,
    /// Manual and automatic retries (including interruption requeues) must execute.
    pub allow_reuse: bool,
    pub number: u64,
    pub seq: u64,
    pub status: String,
    pub conclusion: Option<String>,
    pub details: Option<String>,
    /// SHA-256 of the current lease (the lease itself is only given to the
    /// runner that claimed the attempt).
    pub lease_hash: Option<String>,
    pub lease_expires_at: Option<String>,
    pub cancel_requested: bool,
    pub runner: Option<String>,
    pub cache_scope: String,
    pub network: bool,
    pub created_at: String,
    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    /// This attempt was queued because the previous one was interrupted.
    pub after_interrupt: bool,
    /// Ended because a newer commit replaced its commit on every branch.
    pub superseded: bool,
    pub artifacts: Vec<ArtifactMeta>,
    /// Logs and artifacts were deleted by retention.
    pub files_pruned: bool,
    /// The run's input digest of the job when it was claimed; none for a
    /// job without paths or an attempt claimed before its run had one.
    pub inputs: Option<String>,
}

impl Attempt {
    fn recovery_hint(&self, next: Option<&Attempt>) -> String {
        if let Some(next) = next
            && let Some(retry) = &next.auto_retry
        {
            return format!(
                "Automatic retry {}/{MAX_AUTO_RETRIES} queued as {}",
                retry.number, next.id
            );
        }
        match self.purpose {
            Purpose::Check => "retry the job",
            Purpose::CacheRefresh => {
                "no manual retry is needed; a later check will request a new refresh if needed"
            }
        }
        .into()
    }

    pub fn display_details(&self) -> Option<String> {
        match &self.auto_retry {
            Some(retry) => Some(format!(
                "Automatic retry {}/{MAX_AUTO_RETRIES} of {}.{}",
                retry.number,
                retry.root_attempt,
                self.details
                    .as_ref()
                    .map(|d| format!(" {d}"))
                    .unwrap_or_default()
            )),
            None => self.details.clone(),
        }
    }

    pub fn key(id: &str) -> Vec<u8> {
        Key::new().s(id).bytes()
    }
}

pub fn run_key(repo: &str, sha: &str) -> Vec<u8> {
    Key::new().s(repo).s(sha).bytes()
}

pub fn run_index(r: &Run) -> Vec<IxEntry> {
    let mut out = Vec::new();
    if r.failure_pending {
        out.push(IxEntry {
            table: store::IX_CI_UNNOTIFIED,
            key: run_key(&r.repo, &r.sha),
            value: run_key(&r.repo, &r.sha),
        });
    }
    if let Some(tree) = &r.tree {
        out.push(IxEntry {
            table: store::IX_CI_TREE,
            key: Key::new().s(&r.repo).s(tree).s(&r.sha).bytes(),
            value: run_key(&r.repo, &r.sha),
        });
    }
    if r.waiting {
        out.push(IxEntry {
            table: store::IX_CI_WAITING,
            key: Key::new().u(r.seq).bytes(),
            value: run_key(&r.repo, &r.sha),
        });
    }
    out
}

pub fn attempt_index(a: &Attempt) -> Vec<IxEntry> {
    let mut out = vec![IxEntry {
        table: store::IX_CI_SHA,
        key: Key::new()
            .s(&a.repo)
            .s(&a.sha)
            .s(&a.job.name)
            .u(a.number)
            .bytes(),
        value: a.id.as_bytes().to_vec(),
    }];
    if a.auto_retry.is_some()
        && a.conclusion.as_deref() == Some("success")
        && a.recovery_notified_at.is_none()
    {
        out.push(IxEntry {
            table: store::IX_CI_RECOVERED,
            key: Attempt::key(&a.id),
            value: a.id.as_bytes().to_vec(),
        });
    }
    if a.purpose == Purpose::CacheRefresh
        && a.status != "completed"
        && let Some(key) = &a.cache_key
    {
        out.push(IxEntry {
            table: store::IX_CI_REFRESH,
            key: Key::new().s(&a.repo).s(&a.job.name).s(key).bytes(),
            value: a.id.as_bytes().to_vec(),
        });
    }
    if a.status == "completed"
        && a.conclusion.as_deref() == Some("success")
        && a.reused_from.is_none()
        && !a.files_pruned
        && let Some(digest) = &a.inputs
    {
        out.push(IxEntry {
            table: store::IX_CI_INPUTS,
            key: Key::new()
                .s(&a.repo)
                .s(&a.job.name)
                .s(digest)
                .s(&a.id)
                .bytes(),
            value: a.id.as_bytes().to_vec(),
        });
    }
    match a.status.as_str() {
        "queued" => out.push(IxEntry {
            table: store::IX_CI_PENDING,
            key: Key::new().u(a.seq).bytes(),
            value: a.id.as_bytes().to_vec(),
        }),
        "in_progress" => out.push(IxEntry {
            table: store::IX_CI_ACTIVE,
            key: Key::new().s(&a.id).bytes(),
            value: Vec::new(),
        }),
        _ => {}
    }
    out
}

/// Every CI index entry the records imply (for `branvil admin check`).
pub fn expected_index(r: &impl Rd) -> AppResult<Vec<IxEntry>> {
    let mut out = Vec::new();
    for run in r.prefix_all::<Run>(store::CI_RUNS, &[])? {
        out.extend(run_index(&run));
    }
    for a in r.prefix_all::<Attempt>(store::CI_ATTEMPTS, &[])? {
        out.extend(attempt_index(&a));
    }
    Ok(out)
}

/// The required checks are evaluated in the same transaction as an attempt
/// change. Missing required jobs are failures even without an attempt. A
/// superseded attempt is routine cancellation, not an operational failure.
fn refresh_failure(r: &impl Rd, run: &mut Run) -> AppResult<()> {
    run.failure_pending = false;
    if run.protected_branches.is_empty() || run.failure_notified_at.is_some() {
        return Ok(());
    }
    let Some(repo) =
        r.get::<crate::model::Repo>(store::REPOS, &crate::model::Repo::key(&run.repo))?
    else {
        return Ok(());
    };
    let jobs = match &run.pipeline {
        PipelineState::Jobs { jobs } => jobs.as_slice(),
        _ => &[],
    };
    let latest = current_attempts(r, &run.repo, &run.sha, jobs)?;
    let mut failed = false;
    for name in &repo.required_checks {
        if !jobs.iter().any(|j| &j.name == name) {
            failed = true;
            continue;
        }
        let Some(a) = latest
            .iter()
            .find(|a| &a.job.name == name && run.includes(a))
        else {
            if run.superseded_by.is_some() {
                continue;
            }
            return Ok(());
        };
        if a.status != "completed" {
            return Ok(());
        }
        failed |= a.conclusion.as_deref() != Some("success") && !a.superseded;
    }
    run.failure_pending = failed;
    Ok(())
}

pub fn put_run(w: &Wtx, new: &Run) -> AppResult<()> {
    let old = get_run(w, &new.repo, &new.sha)?;
    let mut new = new.clone();
    refresh_failure(w, &mut new)?;
    if old.as_ref() == Some(&new) {
        return Ok(());
    }
    put_indexed(
        w,
        store::CI_RUNS,
        &run_key(&new.repo, &new.sha),
        old.as_ref().map(run_index),
        &new,
        run_index(&new),
    )
}

fn put_attempt(w: &Wtx, old: Option<&Attempt>, new: &Attempt) -> AppResult<()> {
    put_indexed(
        w,
        store::CI_ATTEMPTS,
        &Attempt::key(&new.id),
        old.map(attempt_index),
        new,
        attempt_index(new),
    )?;
    if let Some(run) = get_run(w, &new.repo, &new.sha)? {
        put_run(w, &run)?;
    }
    Ok(())
}

pub fn get_run(r: &impl Rd, repo: &str, sha: &str) -> AppResult<Option<Run>> {
    r.get(store::CI_RUNS, &run_key(repo, sha))
}

pub fn get_attempt(r: &impl Rd, id: &str) -> AppResult<Option<Attempt>> {
    r.get(store::CI_ATTEMPTS, &Attempt::key(id))
}

/// Every attempt of a commit, in (job, number) order.
pub fn attempts_of(r: &impl Rd, repo: &str, sha: &str) -> AppResult<Vec<Attempt>> {
    let prefix = Key::new().s(repo).s(sha).bytes();
    let mut out = Vec::new();
    for (_, id) in r.prefix_raw(store::IX_CI_SHA, &prefix, None, usize::MAX)? {
        let id = String::from_utf8_lossy(&id).into_owned();
        if let Some(a) = get_attempt(r, &id)? {
            out.push(a);
        }
    }
    Ok(out)
}

/// The latest attempt of every job of `jobs` that ran that very definition;
/// attempts of another definition are history.
pub fn current_attempts(
    r: &impl Rd,
    repo: &str,
    sha: &str,
    jobs: &[JobDef],
) -> AppResult<Vec<Attempt>> {
    let mut out: Vec<Attempt> = Vec::new();
    for a in attempts_of(r, repo, sha)? {
        if a.purpose != Purpose::Check || !jobs.contains(&a.job) {
            continue;
        }
        match out.last_mut() {
            Some(last) if last.job.name == a.job.name => *last = a,
            _ => out.push(a),
        }
    }
    Ok(out)
}

/// The jobs of `run`'s pipeline that have no current attempt, or whose
/// current attempt ended because the commit was replaced, and, once a
/// protected branch wants the commit (a fast-forward merge), the jobs with a
/// dependency cache whose current attempt ran for a pull request (only a
/// protected branch's run saves the cache) and the jobs whose current
/// attempt reused another tree's result by the contents of paths (a
/// protected branch's check is an actual run): they run again as one.
pub(super) fn jobs_to_run(r: &impl Rd, run: &Run) -> AppResult<Vec<JobDef>> {
    let PipelineState::Jobs { jobs } = &run.pipeline else {
        return Ok(Vec::new());
    };
    let current = current_attempts(r, &run.repo, &run.sha, jobs)?;
    let protected = run.cache_scope == "protected";
    let mut out = Vec::new();
    for j in jobs.iter().filter(|j| !run.skips(&j.name)) {
        let again = match current.iter().find(|a| a.job.name == j.name) {
            None => true,
            Some(a) => {
                a.superseded
                    || a.path_skip.is_some()
                    || (protected
                        && j.cache.is_some()
                        && a.status != "queued"
                        && a.cache_scope != "protected")
                    || (protected && source_of(r, a)?.basis == Some(Basis::Paths))
            }
        };
        if again {
            out.push(j.clone());
        }
    }
    Ok(out)
}

fn next_number(r: &impl Rd, repo: &str, sha: &str, job: &str) -> AppResult<u64> {
    let prefix = Key::new().s(repo).s(sha).s(job).bytes();
    let last = r
        .prefix_raw(store::IX_CI_SHA, &prefix, None, usize::MAX)?
        .last()
        .and_then(|(k, _)| store::decode_key(k, "sssu"))
        .map(|p| p[3].u())
        .unwrap_or(0);
    Ok(last + 1)
}

/// Queued attempts, up to `limit`.
pub fn pending_count(r: &impl Rd, limit: usize) -> AppResult<usize> {
    Ok(r.prefix_raw(store::IX_CI_PENDING, &[], None, limit)?.len())
}

/// Capacity for the incoming class, with a hypothetical manual retry applied.
fn admission_count(
    r: &impl Rd,
    class: super::priority::Class,
    limit: usize,
    retry_id: Option<String>,
) -> AppResult<usize> {
    if class >= super::priority::Class::Deferred {
        return pending_count(r, limit);
    }
    let mut snapshot = retry_id
        .as_deref()
        .map(super::priority::Snapshot::retrying)
        .unwrap_or_default();
    let mut count = 0;
    for (_, id) in r.prefix_raw(store::IX_CI_PENDING, &[], None, usize::MAX)? {
        let a = get_attempt(r, &String::from_utf8_lossy(&id))?
            .ok_or_else(|| AppError::internal("pending attempt is missing"))?;
        if snapshot.class_of(r, &a)?.class < super::priority::Class::Deferred {
            count += 1;
            if count >= limit {
                break;
            }
        }
    }
    Ok(count)
}

fn cut(text: &str) -> String {
    if text.len() <= MAX_DETAILS {
        return text.to_string();
    }
    let mut end = MAX_DETAILS;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}…", &text[..end])
}

fn new_attempt(w: &Wtx, run: &Run, job: &JobDef, network: bool, now: &str) -> AppResult<Attempt> {
    let number = next_number(w, &run.repo, &run.sha, &job.name)?;
    Ok(Attempt {
        path_skip: None,
        id: util::new_id("att"),
        purpose: Purpose::Check,
        cache_refresh: None,
        auto_retry: None,
        recovery_notified_at: None,
        wait_for_runner: false,
        repo: run.repo.clone(),
        repo_path: run.repo_path.clone(),
        sha: run.sha.clone(),
        job: job.clone(),
        image_digest: None,
        reused_from: None,
        cache_key: None,
        allow_reuse: true,
        number,
        seq: w.next("ci_seq")?,
        status: "queued".into(),
        conclusion: None,
        details: None,
        lease_hash: None,
        lease_expires_at: None,
        cancel_requested: false,
        runner: None,
        cache_scope: run.cache_scope.clone(),
        network,
        created_at: now.to_string(),
        started_at: None,
        finished_at: None,
        after_interrupt: false,
        superseded: false,
        artifacts: Vec::new(),
        files_pruned: false,
        inputs: None,
    })
}

fn conclude(a: &mut Attempt, conclusion: &str, details: Option<String>, now: &str) {
    a.status = "completed".into();
    a.conclusion = Some(conclusion.to_string());
    a.details = details.map(|d| cut(&d));
    a.lease_hash = None;
    a.lease_expires_at = None;
    a.finished_at = Some(now.to_string());
}

/// What a registration asks for.
pub struct Registration {
    /// Admission order only; never persisted.
    pub order: Vec<String>,
    pub selection: Option<JobSelection>,
    pub repo: String,
    pub repo_path: String,
    pub sha: String,
    pub tree: Option<String>,
    pub branch: String,
    pub protected: bool,
    pub pipeline: PipelineState,
    pub network: bool,
    /// The run's `inputs` from now on.
    pub inputs: BTreeMap<String, String>,
}

/// Records that `branch` wants CI of `sha`. Creates the run or adds the
/// branch to the existing run, and queues (when they fit) the jobs without a
/// current attempt: all of a new run's, the replaced ones of a superseded run
/// that becomes a tip again. Returns true when anything visible changed.
pub fn register(w: &Wtx, reg: Registration, queue_max: usize, now: &str) -> AppResult<bool> {
    let scope = if reg.protected {
        "protected".to_string()
    } else {
        format!("branch:{}", reg.branch)
    };
    let old = get_run(w, &reg.repo, &reg.sha)?;
    let mut run = match &old {
        Some(old) => {
            let mut run = old.clone();
            if !run.branches.contains(&reg.branch) {
                run.branches.push(reg.branch.clone());
            }
            if reg.protected {
                run.cache_scope = scope;
                if !run.protected_branches.contains(&reg.branch) {
                    run.protected_branches.push(reg.branch.clone());
                }
            }
            run.superseded_by = None;
            // The registration read the commit's own pipeline. The comparison
            // is of the parsed jobs, so a change to what the parser yields for
            // the same file runs the jobs again.
            if run.pipeline != reg.pipeline {
                take_pipeline(
                    w,
                    &mut run,
                    reg.pipeline,
                    "Stopped: the commit's pipeline does not define this job so.",
                    now,
                )?;
            }
            run
        }
        None => Run {
            selection: None,
            repo: reg.repo,
            repo_path: reg.repo_path,
            sha: reg.sha,
            tree: reg.tree,
            seq: w.next("ci_seq")?,
            created_at: now.to_string(),
            protected_branches: if reg.protected {
                vec![reg.branch.clone()]
            } else {
                vec![]
            },
            branches: vec![reg.branch],
            pipeline: reg.pipeline,
            cache_scope: scope,
            waiting: false,
            superseded_by: None,
            failure_notified_at: None,
            failure_pending: false,
            inputs: BTreeMap::new(),
        },
    };
    run.inputs = reg.inputs;
    run.selection = if run.cache_scope == "protected" {
        None
    } else {
        reg.selection
    };
    // Skipped attempts consume no queue capacity, including while waiting.
    let mut skipped = false;
    if let PipelineState::Jobs { jobs } = &run.pipeline {
        let current = current_attempts(w, &run.repo, &run.sha, jobs)?;
        for job in jobs.iter().filter(|j| run.skips(&j.name)) {
            let skip = PathSkip {
                paths: job.paths.clone(),
                bases: run.selection.as_ref().unwrap().bases.clone(),
            };
            let old = current.iter().find(|a| a.job.name == job.name);
            // Keep real executions and manual retries. Refresh only decisions.
            if old.is_some_and(|a| !a.superseded && a.path_skip.is_none())
                || old.is_some_and(|a| a.path_skip.as_ref() == Some(&skip))
            {
                continue;
            }
            let mut a = new_attempt(w, &run, job, reg.network, now)?;
            conclude(&mut a, "skipped", Some(skip.reason()), now);
            a.path_skip = Some(skip);
            put_attempt(w, None, &a)?;
            skipped = true;
        }
    }
    let queued = !run.waiting && {
        let todo = jobs_to_run(w, &run)?;
        // A pull request's job still running stops: its protected branch's
        // runs instead.
        for a in current_attempts(w, &run.repo, &run.sha, &todo)? {
            if a.status == "in_progress" && !a.superseded && a.cache_scope != "protected" {
                stop(
                    w,
                    &a,
                    "Stopped: it runs again as a protected branch's job, which saves the cache.",
                    false,
                    now,
                )?;
            }
        }
        admit_jobs(w, &mut run, &todo, &reg.order, reg.network, queue_max, now)?
    };
    refresh_failure(w, &mut run)?;
    let changed = old.as_ref() != Some(&run);
    put_run(w, &run)?;
    Ok(queued || skipped || changed)
}

/// Gives `run` (written by the caller) its commit's pipeline. Every
/// unfinished attempt of a job definition the pipeline does not have is
/// stopped with `why`, so nothing runs, or is queued again after an
/// interruption, that is not the commit's. Returns how many it stopped.
pub fn take_pipeline(
    w: &Wtx,
    run: &mut Run,
    pipeline: PipelineState,
    why: &str,
    now: &str,
) -> AppResult<usize> {
    run.pipeline = pipeline;
    let jobs: &[JobDef] = match &run.pipeline {
        PipelineState::Jobs { jobs } => jobs,
        _ => &[],
    };
    let mut stopped = 0;
    for a in attempts_of(w, &run.repo, &run.sha)? {
        if !jobs.contains(&a.job) && stop(w, &a, why, false, now)? {
            stopped += 1;
        }
    }
    Ok(stopped)
}

/// Ends an unfinished attempt: a queued one is concluded `cancelled`, a
/// running one is asked to stop. Returns false for a finished one.
fn stop(w: &Wtx, a: &Attempt, why: &str, superseded: bool, now: &str) -> AppResult<bool> {
    let mut x = a.clone();
    match a.status.as_str() {
        "queued" => conclude(&mut x, "cancelled", Some(why.to_string()), now),
        "in_progress" => {
            x.cancel_requested = true;
            x.details = Some(why.to_string());
        }
        _ => return Ok(false),
    }
    x.superseded |= superseded;
    put_attempt(w, Some(a), &x)?;
    Ok(true)
}

/// Queues `jobs` of `run` when they all fit, else marks the run waiting.
/// Returns whether it queued any.
fn admit_jobs(
    w: &Wtx,
    run: &mut Run,
    jobs: &[JobDef],
    order: &[String],
    network: bool,
    queue_max: usize,
    now: &str,
) -> AppResult<bool> {
    if jobs.is_empty() {
        run.waiting = false;
        return Ok(false);
    }
    if admission_count(
        w,
        super::priority::run_class(w, run)?.class,
        queue_max + 1,
        None,
    )? + jobs.len()
        > queue_max
    {
        run.waiting = true;
        return Ok(false);
    }
    let mut jobs: Vec<_> = jobs.iter().collect();
    jobs.sort_by_key(|job| {
        order
            .iter()
            .position(|name| name == &job.name)
            .unwrap_or(usize::MAX)
    });
    for job in jobs {
        let a = new_attempt(w, run, job, network, now)?;
        put_attempt(w, None, &a)?;
    }
    run.waiting = false;
    Ok(true)
}

/// Ephemeral admission orders by repository and commit.
pub type AdmissionOrders = BTreeMap<(String, String), Vec<String>>;

/// The same capacity snapshot is used before Git reads and at admission.
/// Earlier candidates consume virtual slots in their own class.
pub fn waiting_candidates(r: &impl Rd, queue_max: usize) -> AppResult<Vec<Run>> {
    let mut waiting = Vec::new();
    for (_, key) in r.prefix_raw(store::IX_CI_WAITING, &[], None, usize::MAX)? {
        if let Some(run) = r.get::<Run>(store::CI_RUNS, &key)? {
            waiting.push((super::priority::run_class(r, &run)?.class, run.seq, run));
        }
    }
    waiting.sort_by_key(|(class, seq, _)| (*class, *seq));
    let mut admitted = Vec::new();
    let (mut added, mut active_added) = (0, 0);
    for (class, _, run) in waiting {
        let count = jobs_to_run(r, &run)?.len();
        let active = class < super::priority::Class::Deferred;
        let extra = if active { active_added } else { added };
        if count != 0 && admission_count(r, class, queue_max + 1, None)? + extra + count > queue_max
        {
            break;
        }
        added += count;
        if active {
            active_added += count;
        }
        admitted.push(run);
    }
    Ok(admitted)
}

/// Admits waiting runs in class order, then registration order, while they fit.
/// Returns the (repo path, sha) of every run admitted.
pub fn admit_waiting(
    w: &Wtx,
    queue_max: usize,
    orders: &AdmissionOrders,
    now: &str,
) -> AppResult<Vec<(String, String)>> {
    let mut admitted = Vec::new();
    for mut run in waiting_candidates(w, queue_max)? {
        let network = network_of(w, &run.repo)?;
        let jobs = jobs_to_run(w, &run)?;
        let order = orders
            .get(&(run.repo.clone(), run.sha.clone()))
            .map(Vec::as_slice)
            .unwrap_or_default();
        admit_jobs(w, &mut run, &jobs, order, network, queue_max, now)?;
        if run.waiting {
            break;
        }
        put_run(w, &run)?;
        admitted.push((run.repo_path.clone(), run.sha.clone()));
    }
    Ok(admitted)
}

fn network_of(r: &impl Rd, repo: &str) -> AppResult<bool> {
    Ok(
        r.get::<crate::model::Repo>(store::REPOS, &crate::model::Repo::key(repo))?
            .map(|x| x.ci_network)
            .unwrap_or(false),
    )
}

/// `branch` moved away from `sha` (to `by`, or was deleted). When no branch
/// wants the commit any more, its queued attempts are cancelled and its
/// running ones asked to stop: their results can only ever describe `sha`.
pub fn branch_left(
    w: &Wtx,
    repo: &str,
    sha: &str,
    branch: &str,
    by: &str,
    now: &str,
) -> AppResult<bool> {
    let Some(old) = get_run(w, repo, sha)? else {
        return Ok(false);
    };
    let mut run = old.clone();
    run.branches.retain(|b| b != branch);
    if run.branches.is_empty() && run.superseded_by.is_none() {
        run.superseded_by = Some(by.to_string());
        run.waiting = false;
        let reason = format!("Superseded: {branch} moved from {sha} to {by}.");
        for a in attempts_of(w, repo, sha)? {
            if a.purpose == Purpose::Check {
                stop(w, &a, &reason, true, now)?;
            }
        }
    }
    if run == old {
        return Ok(false);
    }
    put_run(w, &run)?;
    Ok(true)
}

/// What a runner receives with a claimed attempt.
#[derive(Serialize, Debug)]
pub struct Claimed {
    pub attempt: Attempt,
    pub lease: String,
    pub reuse: Option<Reuse>,
}

/// A successful execution the runner may reuse after checking its image and cache.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Reuse {
    pub attempt_id: String,
    pub sha: String,
    pub image_digest: String,
    pub cache_key: Option<String>,
    pub basis: Basis,
}

/// What makes a result reusable: the same tree, or the same input digest
/// of the job's paths (`Ci::job_inputs`). Never stored: a reused attempt's
/// basis follows from its run's tree and its source's (`reused_basis`).
#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum Basis {
    Tree,
    Paths,
}

/// The basis on which `a` (with the scope and inputs copied at its claim)
/// may reuse a result. Protected and gate runs check every job for real, so
/// they reuse only the same tree; so do jobs without paths and attempts
/// whose run has no digest of the job.
fn basis(a: &Attempt) -> Basis {
    if a.cache_scope == "protected"
        || a.cache_scope.starts_with("branch:gate/")
        || a.job.paths.is_empty()
        || a.inputs.is_none()
    {
        Basis::Tree
    } else {
        Basis::Paths
    }
}

fn tree_of(r: &impl Rd, a: &Attempt) -> AppResult<Option<String>> {
    Ok(get_run(r, &a.repo, &a.sha)?.and_then(|run| run.tree))
}

/// The attempt that owns a result's files (`result_attempt`), and how the
/// result was reused (`reused_basis`).
pub struct Source {
    pub attempt: Attempt,
    pub basis: Option<Basis>,
}

pub fn source_of(r: &impl Rd, a: &Attempt) -> AppResult<Source> {
    let attempt = result_attempt(r, a)?;
    let basis = reused_basis(r, a, &attempt)?;
    Ok(Source { attempt, basis })
}

/// How `a` reused `source` (`result_attempt`): the same tree, or else the
/// same contents of paths. None when `a` is not reused.
pub fn reused_basis(r: &impl Rd, a: &Attempt, source: &Attempt) -> AppResult<Option<Basis>> {
    if a.reused_from.is_none() {
        return Ok(None);
    }
    let tree = tree_of(r, a)?;
    Ok(Some(if tree.is_some() && tree == tree_of(r, source)? {
        Basis::Tree
    } else {
        Basis::Paths
    }))
}

/// The execution metadata reported by the runner at finish.
#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct ResultInfo {
    pub image_digest: Option<String>,
    pub reused_from: Option<String>,
    pub cache_key: Option<String>,
    #[serde(default)]
    pub refresh_cache: bool,
}

fn reusable_shape(a: &Attempt, source: &Attempt) -> bool {
    a.purpose == Purpose::Check
        && a.allow_reuse
        && a.id != source.id
        && a.repo == source.repo
        && a.job == source.job
        && a.network == source.network
        && source.status == "completed"
        && source.conclusion.as_deref() == Some("success")
        && source.reused_from.is_none()
        && source.image_digest.is_some()
        && (a.job.cache.is_none() || source.cache_key.is_some())
}

fn can_reuse(r: &impl Rd, a: &Attempt, source: &Attempt) -> AppResult<bool> {
    if !reusable_shape(a, source) {
        return Ok(false);
    }
    Ok(match basis(a) {
        Basis::Tree => {
            let tree = tree_of(r, a)?;
            tree.is_some() && tree == tree_of(r, source)?
        }
        Basis::Paths => a.inputs == source.inputs,
    })
}

/// What `reuse_sources` reads for `a`; a claim reads each once.
fn sources_key(a: &Attempt) -> (Basis, String, String, String) {
    match basis(a) {
        Basis::Tree => (Basis::Tree, a.repo.clone(), a.sha.clone(), String::new()),
        Basis::Paths => (
            Basis::Paths,
            a.repo.clone(),
            a.job.name.clone(),
            a.inputs.clone().unwrap_or_default(),
        ),
    }
}

/// The candidate history of `a`'s basis: every attempt of the runs with its
/// tree, or the kept actual successes of its job with its input digest.
fn reuse_sources(r: &impl Rd, a: &Attempt) -> AppResult<Vec<Attempt>> {
    if let (Basis::Paths, Some(digest)) = (basis(a), &a.inputs) {
        let prefix = Key::new().s(&a.repo).s(&a.job.name).s(digest).bytes();
        let mut sources = Vec::new();
        for (_, id) in r.prefix_raw(store::IX_CI_INPUTS, &prefix, None, usize::MAX)? {
            let id = String::from_utf8_lossy(&id);
            sources.push(
                get_attempt(r, &id)?
                    .ok_or_else(|| AppError::internal("inputs index attempt is missing"))?,
            );
        }
        return Ok(sources);
    }
    let Some(tree) = tree_of(r, a)? else {
        return Ok(Vec::new());
    };
    let prefix = Key::new().s(&a.repo).s(&tree).bytes();
    let mut sources = Vec::new();
    for (_, key) in r.prefix_raw(store::IX_CI_TREE, &prefix, None, usize::MAX)? {
        let run: Run = r
            .get(store::CI_RUNS, &key)?
            .ok_or_else(|| AppError::internal("tree index run is missing"))?;
        sources.extend(attempts_of(r, &a.repo, &run.sha)?);
    }
    Ok(sources)
}

fn reuse_candidate(a: &Attempt, sources: &[Attempt]) -> Option<Reuse> {
    if !a.allow_reuse || a.purpose != Purpose::Check {
        return None;
    }
    sources
        .iter()
        .filter(|source| !source.files_pruned && reusable_shape(a, source))
        .max_by_key(|source| (&source.finished_at, source.seq))
        .map(|source| Reuse {
            attempt_id: source.id.clone(),
            sha: source.sha.clone(),
            image_digest: source.image_digest.clone().unwrap(),
            cache_key: source.cache_key.clone(),
            basis: basis(a),
        })
}

/// The attempt that owns the result's artifact files. Reuse never chains.
pub fn result_attempt(r: &impl Rd, a: &Attempt) -> AppResult<Attempt> {
    match &a.reused_from {
        Some(id) => {
            get_attempt(r, id)?.ok_or_else(|| AppError::internal("reuse source is missing"))
        }
        None => Ok(a.clone()),
    }
}

/// The current claim order, without changing any attempt or lease. Claims only
/// inspect reuse within the best class; displays can request every class.
pub fn scheduled(r: &impl Rd, first_only: bool) -> AppResult<Vec<(Attempt, Option<Reuse>)>> {
    let mut snapshot = super::priority::Snapshot::default();
    let mut best = super::priority::Class::Last;
    let mut candidates = Vec::new();
    for (_, id) in r.prefix_raw(store::IX_CI_PENDING, &[], None, usize::MAX)? {
        let id = String::from_utf8_lossy(&id);
        let mut a =
            get_attempt(r, &id)?.ok_or_else(|| AppError::internal("pending attempt is missing"))?;
        let class = snapshot.class_of(r, &a)?.class;
        if first_only && class > best {
            continue;
        }
        if first_only && class < best {
            best = class;
            candidates.clear();
        }
        // A protected branch may have come to want this commit since admission.
        let run = get_run(r, &a.repo, &a.sha)?;
        if let Some(run) = &run {
            a.cache_scope = run.cache_scope.clone();
        }
        a.inputs = run.and_then(|run| run.inputs.get(&a.job.name).cloned());
        candidates.push((class, a));
    }
    let mut sources = std::collections::BTreeMap::new();
    let mut ordered = Vec::new();
    for (class, a) in candidates {
        let reuse = if a.allow_reuse && a.purpose == Purpose::Check {
            let sources = match sources.entry(sources_key(&a)) {
                std::collections::btree_map::Entry::Occupied(entry) => entry.into_mut(),
                std::collections::btree_map::Entry::Vacant(entry) => {
                    entry.insert(reuse_sources(r, &a)?)
                }
            };
            reuse_candidate(&a, sources)
        } else {
            None
        };
        if first_only && reuse.is_some() {
            return Ok(vec![(a, reuse)]);
        }
        ordered.push((class, a, reuse));
    }
    ordered.sort_by_key(|(class, a, reuse)| (*class, reuse.is_none(), a.seq));
    Ok(ordered
        .into_iter()
        .map(|(_, a, reuse)| (a, reuse))
        .collect())
}

/// Hands the highest-priority queued attempt to `runner` under a new lease.
pub fn claim(w: &Wtx, runner: &str, lease_secs: u64, now: &str) -> AppResult<Option<Claimed>> {
    let Some((mut a, reuse)) = scheduled(w, true)?.into_iter().next() else {
        return Ok(None);
    };
    let old = get_attempt(w, &a.id)?.unwrap();
    let lease = format!("bvl_{}", util::random_secret::<24>());
    a.status = "in_progress".into();
    a.wait_for_runner = false;
    a.lease_hash = Some(lease_hash(&lease));
    a.lease_expires_at = Some(later(now, lease_secs));
    a.runner = Some(runner.to_string());
    a.started_at = Some(now.to_string());
    put_attempt(w, Some(&old), &a)?;
    Ok(Some(Claimed {
        attempt: a,
        lease,
        reuse,
    }))
}

fn lease_hash(lease: &str) -> String {
    util::hex(&util::sha256(lease.as_bytes()))
}

/// How long a completed attempt ran, from its claim to its conclusion.
pub fn duration_secs(a: &Attempt) -> Option<u64> {
    let at = |t: &Option<String>| chrono::DateTime::parse_from_rfc3339(t.as_deref()?).ok();
    let secs = (at(&a.finished_at)? - at(&a.started_at)?).num_seconds();
    u64::try_from(secs).ok()
}

pub fn later(now: &str, secs: u64) -> String {
    let t = chrono::DateTime::parse_from_rfc3339(now)
        .map(|t| t.with_timezone(&chrono::Utc))
        .unwrap_or_else(|_| chrono::Utc::now());
    util::format_time(t + chrono::Duration::seconds(secs as i64))
}

pub fn earlier(now: &str, secs: u64) -> String {
    let t = chrono::DateTime::parse_from_rfc3339(now)
        .map(|t| t.with_timezone(&chrono::Utc))
        .unwrap_or_else(|_| chrono::Utc::now());
    util::format_time(t - chrono::Duration::seconds(secs as i64))
}

/// The attempt, when `lease` is its current, unexpired lease. Anything else
/// (expired, concluded, superseded by a re-queue, another runner's) is
/// `409 conflict`.
pub fn leased(r: &impl Rd, id: &str, lease: &str, now: &str) -> AppResult<Attempt> {
    let stale = || {
        AppError::conflict(format!(
            "The lease is not the current lease of attempt {id} (expired, cancelled, or superseded)."
        ))
    };
    let a = get_attempt(r, id)?.ok_or_else(stale)?;
    let current = a.lease_hash.as_deref().unwrap_or("");
    let sent = lease_hash(lease);
    let same = bool::from(subtle::ConstantTimeEq::ct_eq(
        current.as_bytes(),
        sent.as_bytes(),
    ));
    if a.status != "in_progress" || !same || a.lease_expires_at.as_deref().unwrap_or("") < now {
        return Err(stale());
    }
    Ok(a)
}

/// Renews the lease; tells the runner whether to stop.
pub fn heartbeat(w: &Wtx, id: &str, lease: &str, lease_secs: u64, now: &str) -> AppResult<Attempt> {
    let old = leased(w, id, lease, now)?;
    let mut a = old.clone();
    a.lease_expires_at = Some(later(now, lease_secs));
    put_attempt(w, Some(&old), &a)?;
    Ok(a)
}

/// Concludes a leased attempt with the runner's result. A cancellation that
/// was requested wins over the reported conclusion.
#[allow(clippy::too_many_arguments)]
pub fn finish(
    w: &Wtx,
    id: &str,
    lease: &str,
    conclusion: &str,
    details: Option<String>,
    info: ResultInfo,
    queue_max: usize,
    now: &str,
) -> AppResult<Attempt> {
    if !CONCLUSIONS.contains(&conclusion) {
        return Err(AppError::invalid(format!(
            "conclusion must be one of {}.",
            CONCLUSIONS.join(", ")
        )));
    }
    if info
        .image_digest
        .as_deref()
        .is_some_and(|d| !super::pipeline::valid_digest(d))
    {
        return Err(AppError::invalid(
            "image_digest must be a configuration digest, sha256:<64 hex>.",
        ));
    }
    let old = leased(w, id, lease, now)?;
    let mut a = old.clone();
    if let Some(key) = &info.cache_key
        && (key.len() != 64 || !key.bytes().all(|b| b.is_ascii_hexdigit()) || a.job.cache.is_none())
    {
        return Err(AppError::invalid(
            "cache_key must be a SHA-256 key of a job with a cache.",
        ));
    }
    if info.refresh_cache
        && (info.reused_from.is_none() || a.cache_scope != "protected" || a.job.cache.is_none())
    {
        return Err(AppError::invalid(
            "refresh_cache requires a reused protected cache job.",
        ));
    }
    let mut refresh_key = None;
    let mut rerun = None;
    if let Some(id) = &info.reused_from {
        let source = get_attempt(w, id)?.ok_or_else(|| AppError::invalid("No reuse source."))?;
        if conclusion != "success"
            || info.cache_key.is_some()
            || !can_reuse(w, &a, &source)?
            || info.image_digest != source.image_digest
        {
            return Err(AppError::invalid(
                "The reuse source must be a successful execution of the same tree (or contents of paths), job, network, and image.",
            ));
        }
        if info.refresh_cache {
            refresh_key = source.cache_key.clone();
        }
        // A protected branch came to want the commit after the claim: its
        // check must be an actual run or a reuse of the same tree.
        if let Some(run) = get_run(w, &a.repo, &a.sha)?
            && run.cache_scope == "protected"
            && basis(&a) == Basis::Paths
            && (run.tree.is_none() || run.tree != tree_of(w, &source)?)
            && !a.cancel_requested
        {
            let mut next = new_attempt(w, &run, &a.job, a.network, now)?;
            next.seq = a.seq;
            rerun = Some(next);
        }
    }
    a.image_digest = info.image_digest;
    a.cache_key = info.cache_key;
    if let Some(next) = rerun {
        conclude(
            &mut a,
            "cancelled",
            Some("Stopped: the commit became a protected branch's; it runs again as one.".into()),
            now,
        );
        put_attempt(w, Some(&old), &a)?;
        put_attempt(w, None, &next)?;
        return Ok(a);
    }
    if a.cancel_requested {
        let why = a.details.clone().unwrap_or_else(|| "Cancelled.".into());
        let reported = details.map(|d| format!(" (the runner reported {conclusion}: {d})"));
        conclude(
            &mut a,
            "cancelled",
            Some(format!("{why}{}", reported.unwrap_or_default())),
            now,
        );
    } else {
        conclude(&mut a, conclusion, details, now);
        a.reused_from = info.reused_from;
        if let Some(key) = refresh_key {
            a.cache_refresh = enqueue_refresh(w, &a, &key, queue_max, now)?;
        }
    }
    let next = if matches!(a.conclusion.as_deref(), Some("failure" | "timed_out")) {
        prepare_auto_retry(w, &a, false, now)?
    } else {
        None
    };
    put_attempt(w, Some(&old), &a)?;
    if let Some(next) = next {
        put_attempt(w, None, &next)?;
    }
    Ok(a)
}

/// Prepare the replacement before choosing the finished attempt's guidance.
/// Save the finished attempt first: both use its old queue position, even
/// when new admissions have filled the queue.
fn prepare_auto_retry(
    w: &Wtx,
    old: &Attempt,
    wait_for_runner: bool,
    now: &str,
) -> AppResult<Option<Attempt>> {
    if old.purpose != Purpose::Check || old.cancel_requested || old.superseded {
        return Ok(None);
    }
    let Some(run) = get_run(w, &old.repo, &old.sha)? else {
        return Ok(None);
    };
    let number = old.auto_retry.as_ref().map_or(1, |r| r.number + 1);
    if run.cache_scope != "protected" || run.superseded_by.is_some() || number > MAX_AUTO_RETRIES {
        return Ok(None);
    }
    let Some(repo) =
        w.get::<crate::model::Repo>(store::REPOS, &crate::model::Repo::key(&old.repo))?
    else {
        return Ok(None);
    };
    let PipelineState::Jobs { jobs } = &run.pipeline else {
        return Ok(None);
    };
    if !repo.required_checks.contains(&old.job.name)
        || !current_attempts(w, &old.repo, &old.sha, jobs)?
            .iter()
            .any(|a| a.id == old.id)
    {
        return Ok(None);
    }
    let mut next = new_attempt(w, &run, &old.job, old.network, now)?;
    next.seq = old.seq;
    next.auto_retry = Some(AutoRetry {
        root_attempt: old
            .auto_retry
            .as_ref()
            .map_or_else(|| old.id.clone(), |r| r.root_attempt.clone()),
        number,
    });
    next.allow_reuse = false;
    next.wait_for_runner = wait_for_runner;
    Ok(Some(next))
}

pub fn mark_recovery_notified(w: &Wtx, id: &str, now: &str) -> AppResult<()> {
    if let Some(old) = get_attempt(w, id)? {
        let mut next = old.clone();
        next.recovery_notified_at = Some(now.into());
        put_attempt(w, Some(&old), &next)?;
    }
    Ok(())
}

/// One unfinished refresh per repository, job name, and cache key. Refreshes
/// use the same bounded queue as checks, but never make a run wait for room.
fn enqueue_refresh(
    w: &Wtx,
    check: &Attempt,
    key: &str,
    queue_max: usize,
    now: &str,
) -> AppResult<Option<String>> {
    let prefix = Key::new().s(&check.repo).s(&check.job.name).s(key).bytes();
    if let Some((_, id)) = w
        .prefix_raw(store::IX_CI_REFRESH, &prefix, None, 1)?
        .into_iter()
        .next()
    {
        return Ok(Some(String::from_utf8_lossy(&id).into_owned()));
    }
    if pending_count(w, queue_max + 1)? >= queue_max {
        return Ok(None);
    }
    let run = get_run(w, &check.repo, &check.sha)?
        .ok_or_else(|| AppError::internal("attempt without a run"))?;
    let mut a = new_attempt(w, &run, &check.job, check.network, now)?;
    a.purpose = Purpose::CacheRefresh;
    a.cache_scope = "protected".into();
    a.cache_key = Some(key.into());
    a.allow_reuse = false;
    let id = a.id.clone();
    put_attempt(w, None, &a)?;
    Ok(Some(id))
}

/// Records an uploaded artifact (the file is already in place).
pub fn add_artifact(
    w: &Wtx,
    id: &str,
    lease: &str,
    meta: ArtifactMeta,
    now: &str,
) -> AppResult<()> {
    let old = leased(w, id, lease, now)?;
    let mut a = old.clone();
    a.artifacts.retain(|x| x.name != meta.name);
    a.artifacts.push(meta);
    put_attempt(w, Some(&old), &a)
}

/// Cancels a queued attempt now, or asks the runner of a running one to stop.
/// A completed attempt is returned unchanged.
pub fn cancel(w: &Wtx, id: &str, who: &str, now: &str) -> AppResult<Attempt> {
    let old =
        get_attempt(w, id)?.ok_or_else(|| AppError::not_found(format!("No CI attempt {id}.")))?;
    let mut a = old.clone();
    let reason = format!("Cancelled by {who}.");
    match old.status.as_str() {
        "queued" => conclude(&mut a, "cancelled", Some(reason), now),
        "in_progress" if !old.cancel_requested => {
            a.cancel_requested = true;
            a.details = Some(reason);
        }
        _ => return Ok(old),
    }
    put_attempt(w, Some(&old), &a)?;
    Ok(a)
}

/// Queues a new attempt of the same job (with the same recorded definition)
/// after the job's latest current attempt (`current_attempts`) completed.
pub fn retry(w: &Wtx, id: &str, queue_max: usize, now: &str) -> AppResult<Attempt> {
    let old =
        get_attempt(w, id)?.ok_or_else(|| AppError::not_found(format!("No CI attempt {id}.")))?;
    if old.purpose == Purpose::CacheRefresh {
        return Err(AppError::conflict(
            "Cache refresh attempts cannot be manually retried.",
        ));
    }
    if old.status != "completed" {
        return Err(AppError::conflict(format!(
            "Attempt {id} is {}; only a completed attempt can be retried.",
            old.status
        )));
    }
    let run = get_run(w, &old.repo, &old.sha)?
        .ok_or_else(|| AppError::internal("attempt without a run"))?;
    let jobs: &[JobDef] = match &run.pipeline {
        PipelineState::Jobs { jobs } => jobs,
        _ => &[],
    };
    if !current_attempts(w, &old.repo, &old.sha, jobs)?
        .iter()
        .any(|a| a.id == old.id)
    {
        return Err(AppError::conflict(format!(
            "Attempt {id} is not the latest attempt of job {} as the commit's pipeline defines it.",
            old.job.name
        )));
    }
    let mut snapshot = super::priority::Snapshot::retrying(&old.id);
    let class = snapshot.class_of(w, &old)?.class;
    if admission_count(w, class, queue_max + 1, Some(old.id.clone()))? >= queue_max {
        return Err(AppError::unavailable(format!(
            "The CI queue is full ({queue_max} queued attempts); retry later."
        )));
    }
    let network = network_of(w, &old.repo)?;
    let mut a = new_attempt(w, &run, &old.job, network, now)?;
    a.allow_reuse = false;
    put_attempt(w, None, &a)?;
    Ok(a)
}

/// Handles every in-progress attempt whose lease expired before `now` (the
/// runner died, lost the service, or the service was down) as interrupted
/// (`interrupt`).
pub fn expire(w: &Wtx, now: &str) -> AppResult<Vec<(String, String)>> {
    let mut changed = Vec::new();
    for old in active(w)? {
        if old.lease_expires_at.as_deref().unwrap_or("") >= now {
            continue;
        }
        interrupt(
            w,
            &old,
            "the runner stopped renewing its lease (runner lost or Branvil restarted)",
            now,
        )?;
        changed.push((old.repo_path.clone(), old.sha.clone()));
    }
    Ok(changed)
}

/// Handles every in-progress attempt (of `runner` only, when given) as
/// interrupted (`why`): the service stopped before it finished, or the
/// runner did.
pub fn interrupt_running(
    w: &Wtx,
    runner: Option<&str>,
    why: &str,
    now: &str,
) -> AppResult<Vec<(String, String)>> {
    let mut changed = Vec::new();
    for old in active(w)? {
        if runner.is_some_and(|r| old.runner.as_deref().map(util::fold) != Some(util::fold(r))) {
            continue;
        }
        interrupt(w, &old, why, now)?;
        changed.push((old.repo_path.clone(), old.sha.clone()));
    }
    Ok(changed)
}

fn active(r: &impl Rd) -> AppResult<Vec<Attempt>> {
    let mut out = Vec::new();
    for (k, _) in r.prefix_raw(store::IX_CI_ACTIVE, &[], None, usize::MAX)? {
        if let Some(parts) = store::decode_key(&k, "s")
            && let Some(a) = get_attempt(r, parts[0].s())?
        {
            out.push(a);
        }
    }
    Ok(out)
}

/// Cache work must not hold up a restart. Called after claims are disabled.
pub fn interrupt_refreshes(w: &Wtx, now: &str) -> AppResult<Vec<(String, String)>> {
    let mut changed = Vec::new();
    for a in active(w)? {
        if a.purpose == Purpose::CacheRefresh {
            interrupt(w, &a, "Branvil stopped during cache refresh", now)?;
            changed.push((a.repo_path, a.sha));
        }
    }
    Ok(changed)
}

/// Requeue the same work at its original position without carrying runner state.
fn replacement(w: &Wtx, run: &Run, old: &Attempt, now: &str) -> AppResult<Attempt> {
    let mut next = new_attempt(w, run, &old.job, old.network, now)?;
    next.seq = old.seq;
    next.allow_reuse = old.allow_reuse;
    next.auto_retry = old.auto_retry.clone();
    next.purpose = old.purpose;
    next.cache_key = old.cache_key.clone();
    next.cache_scope = old.cache_scope.clone();
    Ok(next)
}

/// Yield current running checks in one transaction; queued work keeps its place.
pub fn yield_checks(
    w: &Wtx,
    repo: &str,
    sha: &str,
    who: &str,
    now: &str,
) -> AppResult<Vec<Attempt>> {
    let Some(run) = get_run(w, repo, sha)? else {
        return Ok(Vec::new());
    };
    if super::priority::run_class(w, &run)?.class < super::priority::Class::Deferred {
        return Ok(Vec::new());
    }
    let PipelineState::Jobs { jobs } = &run.pipeline else {
        return Ok(Vec::new());
    };
    let mut yielded = Vec::new();
    for old in current_attempts(w, repo, sha, jobs)? {
        if old.purpose != Purpose::Check
            || old.status != "in_progress"
            || old.cancel_requested
            || old.superseded
        {
            continue;
        }
        let mut stopped = old.clone();
        stopped.cancel_requested = true;
        stopped.details = Some(format!(
            "Yielded: {who} is changing this commit; queued again in its place."
        ));
        let next = replacement(w, &run, &old, now)?;
        put_attempt(w, Some(&old), &stopped)?;
        put_attempt(w, None, &next)?;
        yielded.push(next);
    }
    Ok(yielded)
}

/// Concludes the in-progress attempt `old` `cancelled` as interrupted
/// (`why`); the first interruption of a job that was not cancelled or
/// superseded is queued again as a new attempt in the place `old` had in the
/// queue, ahead of what was queued after it. A second interruption may use
/// the automatic retry budget of a protected required check.
fn interrupt(w: &Wtx, old: &Attempt, why: &str, now: &str) -> AppResult<()> {
    let mut a = old.clone();
    let requeue = !old.cancel_requested && !old.superseded && !old.after_interrupt;
    // Its definition is the run's: taking another pipeline stops unfinished
    // attempts of definitions it does not have.
    let next = if requeue && let Some(run) = get_run(w, &old.repo, &old.sha)? {
        let mut next = replacement(w, &run, old, now)?;
        next.after_interrupt = true;
        Some(next)
    } else if old.after_interrupt {
        prepare_auto_retry(w, old, false, now)?
    } else {
        None
    };
    let details = if old.cancel_requested {
        format!(
            "{} It ended unfinished: {why}.",
            old.details.clone().unwrap_or_else(|| "Cancelled.".into())
        )
    } else if requeue {
        format!("Interrupted: {why}; queued again in its place.")
    } else {
        format!(
            "Interrupted twice: {why}; {}.",
            old.recovery_hint(next.as_ref())
        )
    };
    conclude(&mut a, "cancelled", Some(details), now);
    put_attempt(w, Some(old), &a)?;
    if let Some(next) = next {
        put_attempt(w, None, &next)?;
    }
    Ok(())
}

/// Concludes `startup_failure` for queued attempts created before `before`
/// (no runner took them in time), except retries already waiting for a runner.
pub fn fail_queued(
    w: &Wtx,
    before: &str,
    details: &str,
    now: &str,
) -> AppResult<Vec<(String, String)>> {
    let mut changed = Vec::new();
    for (_, id) in w.prefix_raw(store::IX_CI_PENDING, &[], None, usize::MAX)? {
        let id = String::from_utf8_lossy(&id).into_owned();
        let Some(old) = get_attempt(w, &id)? else {
            continue;
        };
        // Automatic retries wait for a runner instead of exhausting their
        // entire budget while no runner is available.
        if old.wait_for_runner || old.created_at.as_str() >= before {
            continue;
        }
        let next = prepare_auto_retry(w, &old, true, now)?;
        let mut a = old.clone();
        conclude(
            &mut a,
            "startup_failure",
            Some(format!("{details}; {}.", old.recovery_hint(next.as_ref()))),
            now,
        );
        put_attempt(w, Some(&old), &a)?;
        if let Some(next) = next {
            put_attempt(w, None, &next)?;
        }
        changed.push((old.repo_path.clone(), old.sha.clone()));
    }
    Ok(changed)
}

/// Completed attempts finished before `cutoff` whose files still exist.
pub fn prunable(r: &impl Rd, cutoff: &str, limit: usize) -> AppResult<Vec<Attempt>> {
    let mut out = Vec::new();
    for a in r.prefix_all::<Attempt>(store::CI_ATTEMPTS, &[])? {
        if a.status == "completed"
            && !a.files_pruned
            && a.finished_at.as_deref().is_some_and(|f| f < cutoff)
        {
            out.push(a);
            if out.len() >= limit {
                break;
            }
        }
    }
    Ok(out)
}

pub fn mark_pruned(w: &Wtx, id: &str) -> AppResult<()> {
    if let Some(old) = get_attempt(w, id)? {
        let mut a = old.clone();
        a.files_pruned = true;
        put_attempt(w, Some(&old), &a)?;
    }
    Ok(())
}

/// Counts for diagnostics: (queued, in progress, waiting runs).
pub fn counts(r: &impl Rd) -> AppResult<(usize, usize, usize)> {
    Ok((
        r.prefix_raw(store::IX_CI_PENDING, &[], None, usize::MAX)?
            .len(),
        r.prefix_raw(store::IX_CI_ACTIVE, &[], None, usize::MAX)?
            .len(),
        r.prefix_raw(store::IX_CI_WAITING, &[], None, usize::MAX)?
            .len(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;

    fn require_test(w: &Wtx) -> AppResult<()> {
        let repo = crate::model::Repo {
            id: "repo_1".into(),
            path: "acme/demo".into(),
            workspace: "ws".into(),
            default_branch: "main".into(),
            protected_branches: vec![],
            required_checks: vec!["test".into()],
            required_approvals: 0,
            auto_land: false,
            ci_network: false,
            labels: vec![],
            upstream: None,
            revision: 1,
            created_at: T0.into(),
        };
        w.put(store::REPOS, &crate::model::Repo::key(&repo.id), &repo)
    }

    #[test]
    fn automatic_retries_are_atomic_bounded_and_execute_even_with_a_full_queue() {
        let (_dir, s) = store();
        let root = s
            .write_sync(|w| {
                require_test(w)?;
                register(w, reg("sha", "main", &["test"]), 1, T0)?;
                let first = claim(w, "r", 60, T0)?.unwrap();
                register(w, reg("other", "feature", &["other"]), 1, T0)?;
                Ok(first)
            })
            .unwrap();
        let mut current_claim = root;
        let root_id = current_claim.attempt.id.clone();
        let root_seq = current_claim.attempt.seq;
        for n in 1..=4 {
            let result = s
                .write_sync(|w| {
                    let done = finish(
                        w,
                        &current_claim.attempt.id,
                        &current_claim.lease,
                        if n % 2 == 0 { "timed_out" } else { "failure" },
                        Some("exit 1".into()),
                        ResultInfo::default(),
                        1,
                        T0,
                    )?;
                    let current = latest_attempts(w, "repo_1", "sha")?.remove(0);
                    let run = get_run(w, "repo_1", "sha")?.unwrap();
                    if n <= MAX_AUTO_RETRIES {
                        assert_eq!(
                            current.status, "queued",
                            "failure must never be the current check"
                        );
                        assert!(!run.failure_pending);
                        assert!(w.prefix_keys(store::IX_CI_UNNOTIFIED, &[])?.is_empty());
                        assert_eq!(
                            current.auto_retry.as_ref().unwrap(),
                            &AutoRetry {
                                root_attempt: root_id.clone(),
                                number: n,
                            }
                        );
                        assert!(!current.allow_reuse);
                        assert_eq!(current.seq, root_seq);
                        let next = claim(w, "r", 60, T0)?.unwrap();
                        assert_eq!(next.attempt.id, current.id);
                        assert!(next.reuse.is_none());
                        Ok(Some(next))
                    } else {
                        assert_eq!(done.id, current.id);
                        assert!(run.failure_pending);
                        assert_eq!(w.prefix_keys(store::IX_CI_UNNOTIFIED, &[])?.len(), 1);
                        Ok(None)
                    }
                })
                .unwrap();
            if let Some(next) = result {
                current_claim = next;
            }
        }
        assert_eq!(
            s.read_sync(|r| attempts_of(r, "repo_1", "sha"))
                .unwrap()
                .len(),
            4
        );
    }

    #[test]
    fn automatic_retry_targets_only_required_protected_checks_and_retryable_endings() {
        for case in [
            "pr",
            "optional",
            "refresh",
            "startup",
            "cancelled",
            "skipped",
            "success",
            "cancel_request",
            "superseded",
            "definition",
        ] {
            let (_dir, s) = store();
            s.write_sync(|w| {
                require_test(w)?;
                let branch = if case == "pr" { "feature" } else { "main" };
                let name = if case == "optional" {
                    "optional"
                } else {
                    "test"
                };
                register(w, reg("sha", branch, &[name]), 10, T0)?;
                let c = claim(w, "r", 60, T0)?.unwrap();
                if case == "refresh" {
                    let mut a = c.attempt.clone();
                    a.purpose = Purpose::CacheRefresh;
                    put_attempt(w, Some(&c.attempt), &a)?;
                }
                match case {
                    "cancel_request" => {
                        cancel(w, &c.attempt.id, "owner", T0)?;
                    }
                    "superseded" => {
                        branch_left(w, "repo_1", "sha", branch, "new", T0)?;
                    }
                    "definition" => {
                        register(w, reg("sha", branch, &["other"]), 10, T0)?;
                    }
                    _ => {}
                }
                let conclusion = match case {
                    "startup" => "startup_failure",
                    "cancelled" => "cancelled",
                    "skipped" => "skipped",
                    "success" => "success",
                    _ => "failure",
                };
                finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    conclusion,
                    None,
                    ResultInfo::default(),
                    10,
                    T0,
                )?;
                assert_eq!(
                    attempts_of(w, "repo_1", "sha")?
                        .iter()
                        .filter(|a| a.auto_retry.is_some())
                        .count(),
                    0,
                    "{case}"
                );
                Ok(())
            })
            .unwrap();
        }
    }

    #[test]
    fn recovery_guidance_follows_queued_retries_and_returns_to_manual_after_the_budget() {
        for cause in ["lease", "stop", "no_runner"] {
            let (_dir, s) = store();
            s.write_sync(|w| {
                require_test(w)?;
                register(w, reg("sha", "main", &["test"]), 10, T0)?;
                for number in 1..=4 {
                    let start = later(T0, number * 300);
                    let first_end = later(&start, 61);
                    let second_end = later(&start, 122);
                    let ended = if cause == "no_runner" {
                        // A retry waiting for a runner cannot fail for absence
                        // again until it was claimed and requeued by an interruption.
                        let queued = latest_attempts(w, "repo_1", "sha")?.remove(0);
                        if queued.wait_for_runner {
                            claim(w, "runner", 60, &start)?.unwrap();
                            interrupt_running(w, None, "service stopped", &start)?;
                        }
                        let id = latest_attempts(w, "repo_1", "sha")?.remove(0).id;
                        fail_queued(w, &first_end, "Start the runner", &first_end)?;
                        get_attempt(w, &id)?.unwrap()
                    } else {
                        claim(w, "runner", 60, &start)?.unwrap();
                        if cause == "lease" {
                            expire(w, &first_end)?;
                        } else {
                            interrupt_running(w, None, "service stopped", &first_end)?;
                        }
                        let second = claim(w, "runner", 60, &first_end)?.unwrap();
                        if cause == "lease" {
                            expire(w, &second_end)?;
                        } else {
                            interrupt_running(w, None, "service stopped", &second_end)?;
                        }
                        get_attempt(w, &second.attempt.id)?.unwrap()
                    };
                    let details = ended.details.as_deref().unwrap();
                    let current = latest_attempts(w, "repo_1", "sha")?.remove(0);
                    if number <= MAX_AUTO_RETRIES {
                        assert_eq!(current.status, "queued", "{cause}");
                        assert!(
                            details.contains(&format!(
                                "Automatic retry {number}/3 queued as {}",
                                current.id
                            )),
                            "{cause}: {details}"
                        );
                        assert!(!details.contains("retry the job"), "{cause}: {details}");
                        assert!(retry(w, &ended.id, 10, &second_end).is_err());
                        assert!(!get_run(w, "repo_1", "sha")?.unwrap().failure_pending);
                    } else {
                        assert_eq!(current.id, ended.id, "{cause}");
                        assert!(details.contains("retry the job"), "{cause}: {details}");
                        assert!(!details.contains("queued as"), "{cause}: {details}");
                        assert!(get_run(w, "repo_1", "sha")?.unwrap().failure_pending);
                        let manual = retry(w, &ended.id, 10, &second_end)?;
                        assert!(manual.auto_retry.is_none());
                        assert_eq!(
                            claim(w, "runner", 60, &second_end)?.unwrap().attempt.id,
                            manual.id
                        );
                    }
                }
                Ok(())
            })
            .unwrap();
        }
    }

    #[test]
    fn absent_runner_retry_waits_then_interruptions_preserve_its_budget() {
        let (_dir, s) = store();
        s.write_sync(|w| {
            require_test(w)?;
            register(w, reg("sha", "main", &["test"]), 10, T0)?;
            fail_queued(w, T1, "No runner", T1)?;
            let root = attempts_of(w, "repo_1", "sha")?.remove(0);
            for _ in 0..5 {
                fail_queued(w, T3, "Still no runner", T3)?;
            }
            let waiting = latest_attempts(w, "repo_1", "sha")?.remove(0);
            assert!(waiting.wait_for_runner);
            assert_eq!(waiting.auto_retry.as_ref().unwrap().number, 1);
            assert_eq!(attempts_of(w, "repo_1", "sha")?.len(), 2);
            let claimed = claim(w, "returned", 60, T3)?.unwrap();
            assert!(!claimed.attempt.wait_for_runner);
            assert!(claimed.reuse.is_none());
            interrupt_running(w, None, "stopped", T3)?;
            let requeued = claim(w, "returned", 60, T3)?.unwrap();
            assert_eq!(requeued.attempt.auto_retry, waiting.auto_retry);
            assert!(requeued.attempt.after_interrupt);
            assert!(!requeued.attempt.allow_reuse);
            interrupt_running(w, None, "stopped again", T3)?;
            let next = latest_attempts(w, "repo_1", "sha")?.remove(0);
            assert_eq!(next.status, "queued");
            assert!(!next.after_interrupt);
            assert_eq!(
                next.auto_retry.unwrap(),
                AutoRetry {
                    root_attempt: root.id,
                    number: 2,
                }
            );
            assert!(!get_run(w, "repo_1", "sha")?.unwrap().failure_pending);
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn second_interruption_starts_a_retry_and_manual_retry_starts_a_new_budget() {
        let (_dir, s) = store();
        s.write_sync(|w| {
            require_test(w)?;
            register(w, reg("sha", "main", &["test"]), 10, T0)?;
            claim(w, "r", 1, T0)?;
            expire(w, T1)?;
            let interrupted = claim(w, "r", 1, T1)?.unwrap();
            expire(w, T2)?;
            let auto = claim(w, "r", 60, T2)?.unwrap();
            assert_eq!(
                auto.attempt.auto_retry.as_ref().unwrap(),
                &AutoRetry {
                    root_attempt: interrupted.attempt.id,
                    number: 1,
                }
            );
            let success = finish(
                w,
                &auto.attempt.id,
                &auto.lease,
                "success",
                None,
                ResultInfo::default(),
                10,
                T2,
            )?;
            assert_eq!(w.prefix_keys(store::IX_CI_RECOVERED, &[])?.len(), 1);
            let manual = retry(w, &success.id, 10, T2)?;
            assert!(manual.auto_retry.is_none());
            let c = claim(w, "r", 60, T2)?.unwrap();
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "failure",
                None,
                ResultInfo::default(),
                10,
                T2,
            )?;
            let auto = latest_attempts(w, "repo_1", "sha")?.remove(0);
            assert_eq!(
                auto.auto_retry.unwrap(),
                AutoRetry {
                    root_attempt: manual.id,
                    number: 1,
                }
            );
            Ok(())
        })
        .unwrap();
    }

    fn job(name: &str) -> JobDef {
        JobDef {
            paths: Vec::new(),
            name: name.into(),
            image: "alpine".into(),
            commands: vec!["true".into()],
            timeout_secs: Some(60),
            artifacts: vec![],
            cache: None,
        }
    }

    #[test]
    fn yielded_replacements_keep_queue_position_and_finish_or_expire_without_requeue() {
        for expire in [false, true] {
            let (_dir, s) = store();
            s.write_sync(|w| {
                register(w, reg("sha", "feature", &["test", "wait"]), 2, T0)?;
                let old = claim(w, "runner", 60, T0)?.unwrap();
                let queued = latest_attempts(w, "repo_1", "sha")?
                    .into_iter()
                    .find(|a| a.status == "queued")
                    .unwrap();
                let yielded = yield_checks(w, "repo_1", "sha", "author", T0)?;
                assert_eq!(yielded.len(), 1);
                let next = &yielded[0];
                assert_eq!(next.seq, old.attempt.seq);
                assert_eq!(next.number, old.attempt.number + 1);
                assert!(!next.after_interrupt);
                assert_eq!(next.network, old.attempt.network);
                assert_eq!(next.cache_scope, old.attempt.cache_scope);
                assert_eq!(next.cache_key, old.attempt.cache_key);
                assert_eq!(next.allow_reuse, old.attempt.allow_reuse);
                assert_eq!(get_attempt(w, &queued.id)?.unwrap().seq, queued.seq);
                assert!(yield_checks(w, "repo_1", "sha", "author", T0)?.is_empty());
                if expire {
                    super::expire(w, T2)?;
                } else {
                    finish(
                        w,
                        &old.attempt.id,
                        &old.lease,
                        "failure",
                        None,
                        ResultInfo::default(),
                        0,
                        T1,
                    )?;
                }
                assert_eq!(
                    get_attempt(w, &old.attempt.id)?
                        .unwrap()
                        .conclusion
                        .as_deref(),
                    Some("cancelled")
                );
                assert_eq!(attempts_of(w, "repo_1", "sha")?.len(), 3);
                assert_eq!(
                    latest_attempts(w, "repo_1", "sha")?
                        .into_iter()
                        .find(|a| a.job.name == next.job.name)
                        .unwrap()
                        .id,
                    next.id
                );
                assert_eq!(claim(w, "runner", 60, T2)?.unwrap().attempt.id, next.id);
                Ok(())
            })
            .unwrap();
        }
    }

    pub(super) fn reg(sha: &str, branch: &str, jobs: &[&str]) -> Registration {
        Registration {
            order: Vec::new(),
            inputs: Default::default(),
            selection: None,
            repo: "repo_1".into(),
            repo_path: "acme/demo".into(),
            sha: sha.into(),
            tree: Some(format!("tree-{sha}")),
            branch: branch.into(),
            protected: branch == "main",
            pipeline: PipelineState::Jobs {
                jobs: jobs.iter().map(|j| job(j)).collect(),
            },
            network: false,
        }
    }

    pub(super) fn store() -> (tempfile::TempDir, Store) {
        let dir = tempfile::tempdir().unwrap();
        let s = Store::open(&dir.path().join("t.redb"), 1 << 20, 1).unwrap();
        (dir, s)
    }

    pub(super) const T0: &str = "2026-10-14T10:00:00.000Z";
    const T1: &str = "2026-10-14T10:00:30.000Z";
    const T2: &str = "2026-10-14T10:02:00.000Z";
    const T3: &str = "2026-10-14T10:05:00.000Z";
    pub(super) const DIGEST: &str =
        "sha256:2222222222222222222222222222222222222222222222222222222222222222";

    fn latest_attempts(r: &impl Rd, repo: &str, sha: &str) -> AppResult<Vec<Attempt>> {
        let jobs: Vec<JobDef> = attempts_of(r, repo, sha)?
            .into_iter()
            .map(|a| a.job)
            .collect();
        current_attempts(r, repo, sha, &jobs)
    }

    fn latest(s: &Store, sha: &str) -> Vec<Attempt> {
        s.read_sync(|r| latest_attempts(r, "repo_1", sha)).unwrap()
    }

    fn same_tree(sha: &str) -> Registration {
        Registration {
            order: Vec::new(),
            selection: None,
            tree: Some("same-tree".into()),
            ..reg(sha, "main", &["test"])
        }
    }

    fn selected(sha: &str, skipped: &[&str]) -> Registration {
        let mut reg = reg(sha, "feature", &["web", "rust"]);
        if let PipelineState::Jobs { jobs } = &mut reg.pipeline {
            for job in jobs {
                job.paths = vec![format!("{}/**", job.name)];
            }
        }
        reg.selection = Some(JobSelection {
            bases: vec![SelectionBase {
                pull: 1,
                base: "main".into(),
                base_sha: "base".into(),
                merge_base: Some("merge-base".into()),
            }],
            skipped_jobs: skipped.iter().map(|s| s.to_string()).collect(),
        });
        reg
    }

    #[test]
    fn path_selection_is_idempotent_uses_only_real_slots_and_survives_waiting() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("busy", "other", &["busy"]), 1, T0))
            .unwrap();
        s.write_sync(|w| register(w, selected("s1", &["rust"]), 1, T0))
            .unwrap();
        let before = latest(&s, "s1");
        assert_eq!(before.len(), 1);
        assert_eq!(before[0].conclusion.as_deref(), Some("skipped"));
        assert!(before[0].path_skip.is_some());
        assert!(
            s.read_sync(|r| get_run(r, "repo_1", "s1"))
                .unwrap()
                .unwrap()
                .waiting
        );
        assert!(
            !s.write_sync(|w| register(w, selected("s1", &["rust"]), 1, T1))
                .unwrap()
        );
        assert_eq!(latest(&s, "s1"), before);
        s.write_sync(|w| {
            let a = latest_attempts(w, "repo_1", "busy")?;
            cancel(w, &a[0].id, "member1", T1)
        })
        .unwrap();
        s.write_sync(|w| admit_waiting(w, 1, &BTreeMap::new(), T1))
            .unwrap();
        let c = s
            .write_sync(|w| claim(w, "runner", 60, T1))
            .unwrap()
            .unwrap();
        assert_eq!(c.attempt.job.name, "web");
        assert!(
            s.write_sync(|w| claim(w, "runner", 60, T1))
                .unwrap()
                .is_none()
        );
        s.write_sync(|w| register(w, selected("s1", &[]), 1, T2))
            .unwrap();
        let c = s
            .write_sync(|w| claim(w, "runner", 60, T2))
            .unwrap()
            .unwrap();
        assert_eq!(c.attempt.job.name, "rust");
        assert!(c.attempt.path_skip.is_none());
        assert_eq!(c.attempt.number, 2);
        assert!(
            s.read_sync(crate::model::index_problems)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn protected_registration_and_retry_execute_selected_skips_and_cannot_reuse_them() {
        for protected in [false, true] {
            let (_d, s) = store();
            s.write_sync(|w| register(w, selected("s1", &["web", "rust"]), 1, T0))
                .unwrap();
            let a = latest(&s, "s1")[0].clone();
            if protected {
                let mut reg = selected("s1", &["web", "rust"]);
                reg.branch = "main".into();
                reg.protected = true;
                s.write_sync(|w| register(w, reg, 2, T1)).unwrap();
            } else {
                s.write_sync(|w| retry(w, &a.id, 1, T1)).unwrap();
            }
            let c = s
                .write_sync(|w| claim(w, "runner", 60, T1))
                .unwrap()
                .unwrap();
            assert!(c.attempt.path_skip.is_none());
            assert!(c.reuse.is_none());
            assert!(!s.read_sync(|r| can_reuse(r, &c.attempt, &a)).unwrap());
            let mut changed = a.clone();
            changed.job.paths.push("extra/**".into());
            assert_ne!(changed.job, a.job, "paths participate in job identity");
            if protected {
                s.write_sync(|w| register(w, selected("s1", &["web", "rust"]), 2, T2))
                    .unwrap();
                assert!(
                    s.read_sync(|r| get_run(r, "repo_1", "s1"))
                        .unwrap()
                        .unwrap()
                        .selection
                        .is_none()
                );
            }
        }
    }

    fn execute(s: &Store, sha: &str, now: &str) -> Attempt {
        s.write_sync(|w| {
            register(w, same_tree(sha), 10, now)?;
            let c = claim(w, "r", 60, now)?.unwrap();
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    ..Default::default()
                },
                10,
                now,
            )
        })
        .unwrap()
    }

    fn cached(sha: &str) -> Registration {
        let mut reg = same_tree(sha);
        if let PipelineState::Jobs { jobs } = &mut reg.pipeline {
            jobs[0].cache = Some(super::super::pipeline::CacheDef {
                paths: vec!["target".into()],
                key_files: vec![],
            });
        }
        reg
    }

    const CACHE_KEY: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    fn cached_source(s: &Store) -> Attempt {
        s.write_sync(|w| {
            register(w, cached("source"), 10, T0)?;
            let c = claim(w, "runner", 60, T0)?.unwrap();
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    cache_key: Some(CACHE_KEY.into()),
                    ..Default::default()
                },
                10,
                T1,
            )
        })
        .unwrap()
    }

    fn refresh_check(w: &Wtx, source: &Attempt, sha: &str, max: usize) -> AppResult<Attempt> {
        register(w, cached(sha), 10, T1)?;
        // Choose this check even while older refresh work waits.
        let a = latest_attempts(w, "repo_1", sha)?.pop().unwrap();
        let mut selected = a.clone();
        selected.seq = 0;
        put_attempt(w, Some(&a), &selected)?;
        let c = claim(w, "runner", 60, T1)?.unwrap();
        assert_eq!(c.attempt.id, a.id);
        finish(
            w,
            &c.attempt.id,
            &c.lease,
            "success",
            None,
            ResultInfo {
                image_digest: Some(DIGEST.into()),
                reused_from: Some(source.id.clone()),
                refresh_cache: true,
                ..Default::default()
            },
            max,
            T1,
        )
    }

    #[test]
    fn refresh_is_not_a_check_and_is_deduplicated_while_queued_and_running() {
        let (_d, s) = store();
        let source = cached_source(&s);
        s.write_sync(|w| {
            let first = refresh_check(w, &source, "s1", 10)?;
            let id = first.cache_refresh.clone().unwrap();
            let second = refresh_check(w, &source, "s2", 10)?;
            assert_eq!(second.cache_refresh.as_ref(), Some(&id));
            let refresh = claim(w, "runner", 60, T1)?.unwrap();
            assert_eq!(refresh.attempt.id, id);
            assert_eq!(refresh.attempt.purpose, Purpose::CacheRefresh);
            assert!(refresh.reuse.is_none());
            let third = refresh_check(w, &source, "s3", 10)?;
            assert_eq!(third.cache_refresh.as_ref(), Some(&id));
            branch_left(w, "repo_1", "s1", "main", "s2", T1)?;
            assert!(!get_attempt(w, &id)?.unwrap().cancel_requested);
            for sha in ["s1", "s2", "s3"] {
                let checks = latest_attempts(w, "repo_1", sha)?;
                assert_eq!(checks.len(), 1);
                assert_eq!(checks[0].conclusion.as_deref(), Some("success"));
            }
            finish(
                w,
                &id,
                &refresh.lease,
                "failure",
                None,
                ResultInfo::default(),
                10,
                T1,
            )?;
            assert_eq!(latest_attempts(w, "repo_1", "s1")?[0].id, first.id);
            assert!(
                w.prefix_raw(store::IX_CI_REFRESH, &[], None, 10)?
                    .is_empty()
            );
            let fourth = refresh_check(w, &source, "s4", 10)?;
            assert_ne!(fourth.cache_refresh, Some(id));
            assert!(crate::model::index_problems(w)?.is_empty());
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn refresh_queue_full_and_cancelled_check_do_not_block_success_or_enqueue() {
        let (_d, s) = store();
        let source = cached_source(&s);
        s.write_sync(|w| {
            let check = refresh_check(w, &source, "s1", 0)?;
            assert!(check.cache_refresh.is_none());
            assert_eq!(check.conclusion.as_deref(), Some("success"));
            assert!(!get_run(w, "repo_1", "s1")?.unwrap().waiting);
            register(w, cached("s2"), 10, T1)?;
            let c = claim(w, "runner", 60, T1)?.unwrap();
            branch_left(w, "repo_1", "s2", "main", "s3", T1)?;
            let cancelled = finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    reused_from: Some(source.id),
                    refresh_cache: true,
                    ..Default::default()
                },
                10,
                T1,
            )?;
            assert_eq!(cancelled.conclusion.as_deref(), Some("cancelled"));
            assert!(cancelled.cache_refresh.is_none());
            assert_eq!(pending_count(w, 10)?, 0);
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn refresh_survives_departure_and_interrupt_and_its_success_is_reusable() {
        for stop in [false, true] {
            let (_d, s) = store();
            let source = cached_source(&s);
            s.write_sync(|w| {
                let check = refresh_check(w, &source, "s1", 10)?;
                let id = check.cache_refresh.unwrap();
                branch_left(w, "repo_1", "s1", "main", "s2", T1)?;
                assert_eq!(get_attempt(w, &id)?.unwrap().status, "queued");
                let c = claim(w, "runner", 60, T1)?.unwrap();
                if stop {
                    interrupt_refreshes(w, T1)?;
                } else {
                    expire(w, T2)?;
                }
                assert!(
                    finish(
                        w,
                        &id,
                        &c.lease,
                        "success",
                        None,
                        ResultInfo::default(),
                        10,
                        T2
                    )
                    .is_err()
                );
                let next = claim(w, "runner", 600, T2)?.unwrap();
                assert_eq!(next.attempt.purpose, Purpose::CacheRefresh);
                assert_eq!(next.attempt.cache_key.as_deref(), Some(CACHE_KEY));
                assert_eq!(next.attempt.cache_scope, "protected");
                assert!(next.reuse.is_none());
                let success = finish(
                    w,
                    &next.attempt.id,
                    &next.lease,
                    "success",
                    None,
                    ResultInfo {
                        image_digest: Some(DIGEST.into()),
                        cache_key: Some(CACHE_KEY.into()),
                        ..Default::default()
                    },
                    10,
                    T3,
                )?;
                register(w, cached("s3"), 10, T3)?;
                assert_eq!(
                    claim(w, "runner", 60, T3)?
                        .unwrap()
                        .reuse
                        .unwrap()
                        .attempt_id,
                    success.id
                );
                assert_eq!(
                    latest_attempts(w, "repo_1", "s1")?[0].conclusion.as_deref(),
                    Some("success")
                );

                Ok(())
            })
            .unwrap();
        }
    }

    #[test]
    fn recovery_guidance_matches_the_purpose_and_refresh_can_be_requested_again() {
        for purpose in [Purpose::Check, Purpose::CacheRefresh] {
            for cause in ["lease", "stop", "no_runner"] {
                let (_d, s) = store();
                let source = cached_source(&s);
                s.write_sync(|w| {
                    let id = if purpose == Purpose::CacheRefresh {
                        refresh_check(w, &source, "s1", 10)?.cache_refresh.unwrap()
                    } else {
                        register(w, cached("s1"), 10, T1)?;
                        latest_attempts(w, "repo_1", "s1")?.pop().unwrap().id
                    };
                    let final_id = if cause == "no_runner" {
                        fail_queued(w, T2, "Start the runner", T2)?;
                        id
                    } else {
                        claim(w, "runner", 60, T1)?.unwrap();
                        if cause == "lease" {
                            expire(w, T2)?;
                        } else {
                            interrupt_running(w, None, "service stopped", T2)?;
                        }
                        let again = claim(w, "runner", 60, T2)?.unwrap();
                        if cause == "lease" {
                            expire(w, T3)?;
                        } else {
                            interrupt_running(w, None, "service stopped", T3)?;
                        }
                        again.attempt.id
                    };
                    let ended = get_attempt(w, &final_id)?.unwrap();
                    let details = ended.details.unwrap();
                    assert_eq!(ended.purpose, purpose);
                    assert_eq!(ended.status, "completed");
                    if purpose == Purpose::Check {
                        assert!(details.contains("retry the job"), "{cause}: {details}");
                        assert_eq!(retry(w, &final_id, 10, T3)?.purpose, Purpose::Check);
                    } else {
                        assert!(!details.contains("retry the job"), "{cause}: {details}");
                        assert!(
                            details.contains("no manual retry is needed"),
                            "{cause}: {details}"
                        );
                        assert!(details.contains("later check"), "{cause}: {details}");
                        assert!(
                            w.prefix_raw(store::IX_CI_REFRESH, &[], None, 10)?
                                .is_empty()
                        );
                        let fresh = refresh_check(w, &source, "s2", 10)?.cache_refresh.unwrap();
                        assert_ne!(fresh, final_id);
                        assert_eq!(
                            get_attempt(w, &fresh)?.unwrap().purpose,
                            Purpose::CacheRefresh
                        );
                    }
                    Ok(())
                })
                .unwrap();
            }
        }
    }

    #[test]
    fn refresh_retry_is_explicitly_refused_in_every_state() {
        for state in ["queued", "in_progress", "completed"] {
            let (_d, s) = store();
            let source = cached_source(&s);
            s.write_sync(|w| {
                let check = refresh_check(w, &source, "s1", 10)?;
                let id = check.cache_refresh.unwrap();
                if state != "queued" {
                    let c = claim(w, "runner", 60, T1)?.unwrap();
                    if state == "completed" {
                        finish(
                            w,
                            &id,
                            &c.lease,
                            "failure",
                            None,
                            ResultInfo::default(),
                            10,
                            T1,
                        )?;
                    }
                }
                let refused = retry(w, &id, 0, T1).unwrap_err();
                assert_eq!(refused.code, "conflict");
                assert_eq!(
                    refused.message,
                    "Cache refresh attempts cannot be manually retried."
                );
                if state == "completed" {
                    let next = retry(w, &check.id, 10, T1)?;
                    assert_eq!(next.purpose, Purpose::Check);
                    assert_eq!(
                        next.number, 3,
                        "check and refresh share the job's numbering"
                    );
                }
                Ok(())
            })
            .unwrap();
        }
    }

    #[test]
    fn reuse_requires_the_same_repo_tree_definition_network_and_an_actual_success() {
        for mismatch in [
            "none",
            "repo",
            "tree",
            "definition",
            "network",
            "failure",
            "unfinished",
            "reused",
            "missing_tree",
            "missing_digest",
            "pruned",
        ] {
            let (_d, s) = store();
            let source = execute(&s, "s1", T0);
            s.write_sync(|w| {
                let mut changed = source.clone();
                match mismatch {
                    "network" => changed.network = true,
                    "failure" => changed.conclusion = Some("failure".into()),
                    "unfinished" => {
                        changed.status = "in_progress".into();
                        changed.conclusion = None;
                    }
                    "reused" => changed.reused_from = Some("another".into()),
                    "missing_digest" => changed.image_digest = None,
                    "pruned" => changed.files_pruned = true,
                    _ => {}
                }
                put_attempt(w, Some(&source), &changed)?;
                let mut next = same_tree("s2");
                match mismatch {
                    "repo" => {
                        next.repo = "repo_2".into();
                        next.repo_path = "acme/other".into();
                    }
                    "tree" => next.tree = Some("different-tree".into()),
                    "missing_tree" => next.tree = None,
                    "definition" => {
                        if let PipelineState::Jobs { jobs } = &mut next.pipeline {
                            jobs[0].commands.push("false".into());
                        }
                    }
                    _ => {}
                }
                register(w, next, 10, T1)?;
                let c = claim(w, "r", 60, T1)?.unwrap();
                assert_eq!(
                    c.reuse.as_ref().map(|x| x.attempt_id.as_str()),
                    if mismatch == "none" {
                        Some(source.id.as_str())
                    } else {
                        None
                    },
                    "{mismatch}"
                );
                Ok(())
            })
            .unwrap();
        }
    }

    /// `same_tree` with the input digest `d1` of its job.
    fn with_inputs(sha: &str) -> Registration {
        Registration {
            order: Vec::new(),
            inputs: BTreeMap::from([("test".to_string(), "d1".to_string())]),
            ..same_tree(sha)
        }
    }

    /// The attempts indexed by `test`'s input digest `d1`.
    fn by_inputs(r: &impl Rd) -> AppResult<Vec<String>> {
        let prefix = Key::new().s("repo_1").s("test").s("d1").bytes();
        Ok(
            r.prefix_raw(store::IX_CI_INPUTS, &prefix, None, usize::MAX)?
                .into_iter()
                .map(|(_, id)| String::from_utf8(id).unwrap())
                .collect(),
        )
    }

    #[test]
    fn claims_carry_the_runs_input_digest_of_their_job_for_every_purpose() {
        let (_d, s) = store();
        s.write_sync(|w| {
            let mut reg = reg("s1", "main", &["test", "plain"]);
            reg.inputs = BTreeMap::from([("test".to_string(), "d1".to_string())]);
            register(w, reg, 10, T0)?;
            let mut claimed = BTreeMap::new();
            while let Some(c) = claim(w, "r", 60, T0)? {
                claimed.insert(c.attempt.job.name.clone(), c.attempt.inputs);
            }
            assert_eq!(
                claimed,
                BTreeMap::from([
                    ("plain".to_string(), None),
                    ("test".to_string(), Some("d1".to_string())),
                ])
            );
            Ok(())
        })
        .unwrap();
        let source = cached_source(&s);
        s.write_sync(|w| {
            let check = refresh_check(w, &source, "s2", 10)?;
            register(
                w,
                Registration {
                    order: Vec::new(),
                    inputs: BTreeMap::from([("test".to_string(), "d2".to_string())]),
                    ..cached("s2")
                },
                10,
                T1,
            )?;
            let refresh = claim(w, "runner", 60, T1)?.unwrap().attempt;
            assert_eq!(Some(refresh.id), check.cache_refresh);
            assert_eq!(refresh.purpose, Purpose::CacheRefresh);
            assert_eq!(refresh.inputs.as_deref(), Some("d2"));
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn only_actual_kept_successes_with_inputs_are_found_by_them() {
        for case in ["success", "failure", "no_inputs", "pruned"] {
            let (_d, s) = store();
            s.write_sync(|w| {
                let reg = match case {
                    "no_inputs" => same_tree("s1"),
                    _ => with_inputs("s1"),
                };
                register(w, reg, 10, T0)?;
                let c = claim(w, "r", 60, T0)?.unwrap();
                assert!(by_inputs(w)?.is_empty(), "unfinished");
                let conclusion = if case == "failure" {
                    "failure"
                } else {
                    "success"
                };
                let info = ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    ..Default::default()
                };
                let done = finish(w, &c.attempt.id, &c.lease, conclusion, None, info, 10, T0)?;
                if case == "pruned" {
                    mark_pruned(w, &done.id)?;
                }
                let found = by_inputs(w)?;
                assert_eq!(
                    found,
                    if case == "success" {
                        vec![done.id.clone()]
                    } else {
                        vec![]
                    },
                    "{case}"
                );
                if case == "success" {
                    // A reused success is not an execution of its inputs.
                    register(w, with_inputs("s2"), 10, T1)?;
                    let c = claim(w, "r", 60, T1)?.unwrap();
                    assert_eq!(c.reuse.unwrap().attempt_id, done.id);
                    let info = ResultInfo {
                        image_digest: Some(DIGEST.into()),
                        reused_from: Some(done.id.clone()),
                        ..Default::default()
                    };
                    finish(w, &c.attempt.id, &c.lease, "success", None, info, 10, T1)?;
                    assert_eq!(by_inputs(w)?, found);
                }
                Ok(())
            })
            .unwrap();
        }
    }

    #[test]
    fn reuse_selects_the_latest_finished_execution_and_never_chains() {
        let (_d, s) = store();
        let first = execute(&s, "s1", T0);
        let second = execute(&s, "s2", T1); // runner declined reuse and actually executed
        assert!(second.reused_from.is_none());
        s.write_sync(|w| {
            register(w, same_tree("s3"), 10, T2)?;
            let c = claim(w, "r", 60, T2)?.unwrap();
            assert_eq!(c.reuse.as_ref().unwrap().attempt_id, second.id);
            let info = ResultInfo {
                image_digest: Some(DIGEST.into()),
                reused_from: Some(second.id.clone()),
                cache_key: None,
                refresh_cache: false,
            };
            let mut invalid = info.clone();
            invalid.image_digest = None;
            assert!(finish(w, &c.attempt.id, &c.lease, "success", None, invalid, 10, T2).is_err());
            assert!(
                finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "failure",
                    None,
                    info.clone(),
                    10,
                    T2
                )
                .is_err()
            );
            let done = finish(w, &c.attempt.id, &c.lease, "success", None, info, 10, T2)?;
            assert_eq!(done.reused_from.as_deref(), Some(second.id.as_str()));
            register(w, same_tree("s4"), 10, T3)?;
            let next = claim(w, "r", 60, T3)?.unwrap();
            assert_eq!(next.reuse.unwrap().attempt_id, second.id);
            // A newer failed retry does not erase an earlier successful execution.
            let mut failed = second.clone();
            failed.conclusion = Some("failure".into());
            put_attempt(w, Some(&second), &failed)?;
            assert_eq!(
                reuse_candidate(&next.attempt, &reuse_sources(w, &next.attempt)?)
                    .unwrap()
                    .attempt_id,
                first.id
            );
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn a_source_pruned_after_claim_still_finishes_but_is_never_offered_again() {
        let (_d, s) = store();
        let source = execute(&s, "s1", T0);
        s.write_sync(|w| {
            register(w, same_tree("s2"), 10, T1)?;
            let c = claim(w, "r", 60, T1)?.unwrap();
            assert_eq!(c.reuse.unwrap().attempt_id, source.id);
            let mut pruned = source.clone();
            pruned.files_pruned = true;
            put_attempt(w, Some(&source), &pruned)?;
            let done = finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    reused_from: Some(source.id.clone()),
                    cache_key: None,
                    refresh_cache: false,
                },
                10,
                T1,
            )?;
            assert_eq!(done.reused_from.as_deref(), Some(source.id.as_str()));
            register(w, same_tree("s3"), 10, T2)?;
            assert!(claim(w, "r", 60, T2)?.unwrap().reuse.is_none());
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn reuse_is_decided_when_waiting_and_interrupted_attempts_are_claimed() {
        let (_d, s) = store();
        s.write_sync(|w| {
            register(w, same_tree("s1"), 1, T0)?;
            register(w, same_tree("s2"), 1, T0)?;
            assert!(get_run(w, "repo_1", "s2")?.unwrap().waiting);
            let first = claim(w, "r", 60, T0)?.unwrap();
            finish(
                w,
                &first.attempt.id,
                &first.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    ..Default::default()
                },
                10,
                T1,
            )?;
            admit_waiting(w, 1, &BTreeMap::new(), T1)?;
            let next = claim(w, "r", 60, T1)?.unwrap();
            assert_eq!(next.reuse.unwrap().attempt_id, first.attempt.id);
            expire(w, T2)?;
            let requeued = claim(w, "r", 60, T2)?.unwrap();
            assert!(requeued.attempt.after_interrupt);
            assert_eq!(requeued.reuse.unwrap().attempt_id, first.attempt.id);
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn manual_retries_and_their_interruption_requeues_always_execute() {
        let (_d, s) = store();
        let source = execute(&s, "s1", T0);
        s.write_sync(|w| {
            let retry = retry(w, &source.id, 10, T1)?;
            assert!(!retry.allow_reuse);
            let c = claim(w, "r", 60, T1)?.unwrap();
            assert!(c.reuse.is_none());
            let info = ResultInfo {
                image_digest: Some(DIGEST.into()),
                reused_from: Some(source.id.clone()),
                cache_key: None,
                refresh_cache: false,
            };
            assert!(finish(w, &c.attempt.id, &c.lease, "success", None, info, 10, T1).is_err());
            expire(w, T2)?;
            let next = claim(w, "r", 60, T2)?.unwrap();
            assert!(!next.attempt.allow_reuse && next.reuse.is_none());
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn a_retry_survives_departure_only_when_it_is_queued_after_the_departure() {
        for branch in ["gate/3", "feature"] {
            for departure_first in [false, true] {
                let (_dir, store) = store();
                store
                    .write_sync(|w| {
                        register(w, reg("s1", branch, &["test"]), 10, T0)?;
                        let original = claim(w, "runner", 60, T0)?.unwrap();
                        finish(
                            w,
                            &original.attempt.id,
                            &original.lease,
                            "failure",
                            None,
                            ResultInfo::default(),
                            10,
                            T0,
                        )?;
                        if departure_first {
                            branch_left(w, "repo_1", "s1", branch, "deleted", T1)?;
                        }
                        let retry = retry(w, &original.attempt.id, 10, T1)?;
                        assert_eq!(retry.number, 2);
                        if !departure_first {
                            branch_left(w, "repo_1", "s1", branch, "deleted", T1)?;
                        }
                        let run = get_run(w, "repo_1", "s1")?.unwrap();
                        assert!(run.branches.is_empty());
                        assert_eq!(run.superseded_by.as_deref(), Some("deleted"));
                        let attempt = get_attempt(w, &retry.id)?.unwrap();
                        if departure_first {
                            assert_eq!(attempt.status, "queued");
                            assert!(!attempt.superseded);
                            assert_eq!(pending_count(w, 10)?, 1);
                            let claimed = claim(w, "runner", 60, T1)?.unwrap();
                            assert_eq!(claimed.attempt.id, retry.id);
                        } else {
                            assert_eq!(attempt.status, "completed");
                            assert_eq!(attempt.conclusion.as_deref(), Some("cancelled"));
                            assert!(attempt.superseded);
                            assert!(attempt.details.unwrap().starts_with("Superseded:"));
                            assert_eq!(pending_count(w, 10)?, 0);
                            assert!(claim(w, "runner", 60, T1)?.is_none());
                        }
                        Ok(())
                    })
                    .unwrap();
            }
        }
    }

    /// A fast-forward merge: main comes to want a commit whose jobs ran for
    /// the pull request. The jobs with a cache run again, once, as main's.
    #[test]
    fn a_protected_branch_runs_the_cached_jobs_of_a_pull_requests_commit_again() {
        let (_d, s) = store();
        let mut pr = reg("s1", "feature", &["build", "lint"]);
        let PipelineState::Jobs { jobs } = &mut pr.pipeline else {
            unreachable!()
        };
        jobs[0].cache = Some(crate::ci::pipeline::CacheDef {
            paths: vec!["target".into()],
            key_files: vec![],
        });
        let pipeline = pr.pipeline.clone();
        s.write_sync(|w| register(w, pr, 10, T0)).unwrap();
        for _ in 0..2 {
            let c = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
            assert_eq!(c.attempt.cache_scope, "branch:feature");
            s.write_sync(|w| {
                finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "success",
                    None,
                    ResultInfo {
                        image_digest: Some(DIGEST.into()),
                        cache_key: c.attempt.job.cache.as_ref().map(|_| CACHE_KEY.into()),
                        ..Default::default()
                    },
                    10,
                    T1,
                )
            })
            .unwrap();
        }
        let merged = || Registration {
            order: Vec::new(),
            selection: None,
            pipeline: pipeline.clone(),
            ..reg("s1", "main", &[])
        };
        assert!(s.write_sync(|w| register(w, merged(), 10, T2)).unwrap());
        let c = s.write_sync(|w| claim(w, "r", 60, T2)).unwrap().unwrap();
        assert_eq!(
            (c.attempt.job.name.as_str(), c.attempt.number),
            ("build", 2)
        );
        assert_eq!(c.attempt.cache_scope, "protected");
        assert!(
            c.reuse.is_some(),
            "a fast-forward merge may reuse the same commit's completed PR execution"
        );
        assert!(
            s.write_sync(|w| claim(w, "r", 60, T2)).unwrap().is_none(),
            "lint has no cache"
        );
        // Registered again (the reconciler), nothing more runs.
        s.write_sync(|w| register(w, merged(), 10, T3)).unwrap();
        assert!(s.write_sync(|w| claim(w, "r", 60, T3)).unwrap().is_none());
    }

    #[test]
    fn an_attempt_takes_the_scope_its_run_has_when_claimed() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "feature", &["test"]), 10, T0))
            .unwrap();
        // Merged before a runner claimed it: main now wants the commit.
        s.write_sync(|w| register(w, reg("s1", "main", &["test"]), 10, T0))
            .unwrap();
        let c = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
        assert_eq!(c.attempt.cache_scope, "protected");
        s.write_sync(|w| register(w, reg("s2", "feature", &["test"]), 10, T0))
            .unwrap();
        let c = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
        assert_eq!(c.attempt.cache_scope, "branch:feature");
    }

    #[test]
    fn a_claimed_attempt_finishes_only_with_its_current_lease() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "feature", &["test"]), 10, T0))
            .unwrap();
        let c = s
            .write_sync(|w| claim(w, "runner1", 60, T0))
            .unwrap()
            .unwrap();
        assert_eq!(c.attempt.status, "in_progress");
        assert!(
            s.write_sync(|w| claim(w, "runner1", 60, T0))
                .unwrap()
                .is_none()
        );
        let wrong = s.write_sync(|w| {
            finish(
                w,
                &c.attempt.id,
                "bvl_other",
                "success",
                None,
                ResultInfo::default(),
                10,
                T1,
            )
        });
        assert_eq!(wrong.unwrap_err().code, "conflict");
        let junk = s.write_sync(|w| {
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some("sha256:x".into()),
                    ..Default::default()
                },
                10,
                T1,
            )
        });
        assert_eq!(junk.unwrap_err().code, "invalid");
        let done = s
            .write_sync(|w| {
                finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "failure",
                    Some("exit 1".into()),
                    ResultInfo {
                        image_digest: Some(DIGEST.into()),
                        ..Default::default()
                    },
                    10,
                    T1,
                )
            })
            .unwrap();
        assert_eq!(done.conclusion.as_deref(), Some("failure"));
        assert_eq!(done.image_digest.as_deref(), Some(DIGEST));
        // The lease is gone with the conclusion: a late repeat changes nothing.
        let again = s.write_sync(|w| {
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo::default(),
                10,
                T1,
            )
        });
        assert_eq!(again.unwrap_err().code, "conflict");
        assert_eq!(latest(&s, "s1")[0].conclusion.as_deref(), Some("failure"));
        assert!(
            s.read_sync(crate::model::index_problems)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn an_expired_lease_is_interrupted_and_requeued_once() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "feature", &["test"]), 10, T0))
            .unwrap();
        let c1 = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
        // Heartbeat after expiry is refused; the sweeper interrupts it.
        assert!(
            s.write_sync(|w| heartbeat(w, &c1.attempt.id, &c1.lease, 60, T2))
                .is_err()
        );
        assert_eq!(s.write_sync(|w| expire(w, T2)).unwrap().len(), 1);
        let l = latest(&s, "s1");
        assert_eq!((l[0].number, l[0].status.as_str()), (2, "queued"));
        assert!(l[0].after_interrupt);
        // The first runner's late result is refused.
        let late = s.write_sync(|w| {
            finish(
                w,
                &c1.attempt.id,
                &c1.lease,
                "success",
                None,
                ResultInfo::default(),
                10,
                T2,
            )
        });
        assert_eq!(late.unwrap_err().code, "conflict");
        // The second interruption is final.
        let c2 = s.write_sync(|w| claim(w, "r", 60, T2)).unwrap().unwrap();
        assert_eq!(c2.attempt.number, 2);
        s.write_sync(|w| expire(w, T3)).unwrap();
        let l = latest(&s, "s1");
        assert_eq!(l[0].number, 2);
        assert_eq!(l[0].conclusion.as_deref(), Some("cancelled"));
        assert!(l[0].details.as_deref().unwrap().contains("twice"));
        assert!(
            s.read_sync(crate::model::index_problems)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn an_interrupted_job_is_queued_again_ahead_of_what_was_queued_after_it() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "feature", &["test"]), 10, T0))
            .unwrap();
        let c1 = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
        s.write_sync(|w| register(w, reg("s2", "other", &["test"]), 10, T0))
            .unwrap();
        s.write_sync(|w| expire(w, T2)).unwrap();
        let again = s.write_sync(|w| claim(w, "r", 60, T2)).unwrap().unwrap();
        assert_eq!(
            (again.attempt.sha.as_str(), again.attempt.number),
            ("s1", 2)
        );
        // Stopped by the service, it keeps its place too.
        // Only the given runner's.
        let changed = s.write_sync(|w| interrupt_running(w, Some("other"), "-", T2));
        assert!(changed.unwrap().is_empty());
        s.write_sync(|w| interrupt_running(w, Some("R"), "Branvil stopped", T2))
            .unwrap();
        let old = s
            .read_sync(|r| get_attempt(r, &again.attempt.id))
            .unwrap()
            .unwrap();
        assert!(old.details.unwrap().contains("Branvil stopped; retry"));
        assert_eq!(
            s.write_sync(|w| claim(w, "r", 60, T2))
                .unwrap()
                .unwrap()
                .attempt
                .sha,
            "s2"
        );
        let first = s
            .read_sync(|r| get_attempt(r, &c1.attempt.id))
            .unwrap()
            .unwrap();
        assert!(first.details.unwrap().contains("queued again in its place"));
        assert!(
            s.read_sync(crate::model::index_problems)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn a_new_tip_supersedes_the_old_commit() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "feature", &["build", "test"]), 10, T0))
            .unwrap();
        let running = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
        s.write_sync(|w| branch_left(w, "repo_1", "s1", "feature", "s2", T1))
            .unwrap();
        s.write_sync(|w| register(w, reg("s2", "feature", &["build", "test"]), 10, T1))
            .unwrap();
        let old = latest(&s, "s1");
        let build = old.iter().find(|a| a.job.name == "build").unwrap();
        let test = old.iter().find(|a| a.job.name == "test").unwrap();
        assert!(build.cancel_requested, "the running job is asked to stop");
        assert_eq!(
            test.conclusion.as_deref(),
            Some("cancelled"),
            "the queued one is dropped"
        );
        // The cancelled runner's success never counts, even for its own commit.
        let fin = s
            .write_sync(|w| {
                finish(
                    w,
                    &running.attempt.id,
                    &running.lease,
                    "success",
                    None,
                    ResultInfo::default(),
                    10,
                    T1,
                )
            })
            .unwrap();
        assert_eq!(fin.conclusion.as_deref(), Some("cancelled"));
        // Only the new commit's attempts are queued.
        let next = s.write_sync(|w| claim(w, "r", 60, T1)).unwrap().unwrap();
        assert_eq!(next.attempt.sha, "s2");
        assert!(
            s.read_sync(crate::model::index_problems)
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn a_commit_still_wanted_by_another_branch_is_not_superseded() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "a", &["test"]), 10, T0))
            .unwrap();
        s.write_sync(|w| register(w, reg("s1", "b", &["test"]), 10, T0))
            .unwrap();
        s.write_sync(|w| branch_left(w, "repo_1", "s1", "a", "s2", T1))
            .unwrap();
        assert_eq!(latest(&s, "s1")[0].status, "queued");
        assert_eq!(latest(&s, "s1").len(), 1, "one attempt for both branches");
    }

    #[test]
    fn admission_uses_named_order_then_pipeline_and_never_reorders_existing_attempts() {
        let (_d, s) = store();
        s.write_sync(|w| {
            let mut r = reg("sha", "feature", &["a", "b", "c", "d"]);
            r.order = vec!["removed".into(), "c".into(), "a".into()];
            register(w, r, 10, T0)?;
            let mut before = attempts_of(w, "repo_1", "sha")?;
            before.sort_by_key(|a| a.seq);
            assert_eq!(
                before
                    .iter()
                    .map(|a| a.job.name.as_str())
                    .collect::<Vec<_>>(),
                ["c", "a", "b", "d"]
            );
            register(w, reg("sha", "feature", &["a", "b", "c", "d"]), 10, T1)?;
            let mut after = attempts_of(w, "repo_1", "sha")?;
            after.sort_by_key(|a| a.seq);
            assert_eq!(after, before);
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn a_full_queue_makes_runs_wait_and_admits_them_in_order() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "a", &["x", "y"]), 2, T0))
            .unwrap();
        s.write_sync(|w| register(w, reg("s2", "b", &["x"]), 2, T0))
            .unwrap();
        let run2 = s
            .read_sync(|r| get_run(r, "repo_1", "s2"))
            .unwrap()
            .unwrap();
        assert!(run2.waiting);
        assert!(latest(&s, "s2").is_empty());
        let cancelled = s.write_sync(|w| {
            let a = latest_attempts(w, "repo_1", "s1")?;
            cancel(w, &a[0].id, "ken", T0)?;
            Ok(a[0].id.clone())
        });
        let id = cancelled.unwrap();
        // One slot free: s2 fits now.
        assert_eq!(
            s.write_sync(|w| admit_waiting(w, 2, &BTreeMap::new(), T1))
                .unwrap()
                .len(),
            1
        );
        assert_eq!(latest(&s, "s2")[0].status, "queued");
        // The queue is full again: a retry is refused explicitly.
        let full = s.write_sync(|w| retry(w, &id, 2, T1)).unwrap_err();
        assert_eq!(full.code, "unavailable");
        assert!(
            s.read_sync(crate::model::index_problems)
                .unwrap()
                .is_empty()
        );
    }

    fn with(sha: &str, jobs: Vec<JobDef>) -> Registration {
        let mut r = reg(sha, "feature", &[]);
        r.pipeline = PipelineState::Jobs { jobs };
        r
    }

    fn changed(name: &str) -> JobDef {
        let mut j = job(name);
        j.commands = vec!["echo changed".into()];
        j
    }

    #[test]
    fn a_taken_pipeline_stops_the_old_definition_and_retries_the_current_one() {
        let (_d, s) = store();
        // The old definition of test runs when the run takes the new one: it
        // is asked to stop, and the new definition is queued.
        s.write_sync(|w| register(w, reg("s1", "feature", &["test"]), 10, T0))
            .unwrap();
        let old = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
        s.write_sync(|w| register(w, with("s1", vec![changed("test")]), 10, T0))
            .unwrap();
        let a1 = s
            .read_sync(|r| get_attempt(r, &old.attempt.id))
            .unwrap()
            .unwrap();
        assert!(a1.cancel_requested);
        // Its lease runs out: it is not queued again.
        s.write_sync(|w| expire(w, T2)).unwrap();
        let all = s.read_sync(|r| attempts_of(r, "repo_1", "s1")).unwrap();
        let shape: Vec<(u64, bool, &str)> = all
            .iter()
            .map(|a| (a.number, a.job == changed("test"), a.status.as_str()))
            .collect();
        assert_eq!(shape, vec![(1, false, "completed"), (2, true, "queued")]);
        // The current attempt fails and is retried; the old one is history.
        let c = s.write_sync(|w| claim(w, "r", 60, T2)).unwrap().unwrap();
        assert_eq!(c.attempt.number, 2);
        s.write_sync(|w| {
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "failure",
                None,
                ResultInfo::default(),
                10,
                T2,
            )
        })
        .unwrap();
        let again = s.write_sync(|w| retry(w, &c.attempt.id, 10, T2)).unwrap();
        assert_eq!((again.number, again.job == changed("test")), (3, true));
        let refused = s.write_sync(|w| retry(w, &old.attempt.id, 10, T2));
        assert_eq!(refused.unwrap_err().code, "conflict");
        // Leaving the commit ends every unfinished attempt.
        s.write_sync(|w| branch_left(w, "repo_1", "s1", "feature", "s2", T2))
            .unwrap();
        let all = s.read_sync(|r| attempts_of(r, "repo_1", "s1")).unwrap();
        assert!(all.iter().all(|a| a.status == "completed"), "{all:?}");
    }

    #[test]
    fn taking_the_same_pipeline_stops_only_unfinished_attempts_of_another_definition() {
        let (_d, s) = store();
        // As an upgrade can leave it: an old definition still running beside
        // the run's pipeline.
        s.write_sync(|w| register(w, reg("s1", "feature", &["test"]), 10, T0))
            .unwrap();
        let old = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
        let pipeline = PipelineState::Jobs {
            jobs: vec![changed("test")],
        };
        let stopped = s
            .write_sync(|w| {
                let old = get_run(w, "repo_1", "s1")?.unwrap();
                let mut run = old.clone();
                run.pipeline = pipeline.clone();
                put_run(w, &run)?;
                // The run already has this pipeline; the old attempt stops.
                take_pipeline(w, &mut run, pipeline.clone(), "stop", T0)
            })
            .unwrap();
        assert_eq!(stopped, 1);
        let a1 = s
            .read_sync(|r| get_attempt(r, &old.attempt.id))
            .unwrap()
            .unwrap();
        assert!(a1.cancel_requested);
        s.write_sync(|w| expire(w, T2)).unwrap();
        assert_eq!(
            s.read_sync(|r| attempts_of(r, "repo_1", "s1"))
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn an_attempt_of_another_definition_is_not_retried() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "feature", &["test"]), 10, T0))
            .unwrap();
        let c = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
        s.write_sync(|w| {
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo::default(),
                10,
                T0,
            )
        })
        .unwrap();
        // The run takes another definition (as the upgrade does for a run no
        // branch wants now): the old attempt is the newest, and only history.
        s.write_sync(|w| {
            let old = get_run(w, "repo_1", "s1")?.unwrap();
            let mut run = old.clone();
            let jobs = vec![changed("test")];
            take_pipeline(w, &mut run, PipelineState::Jobs { jobs }, "stop", T0)?;
            put_run(w, &run)
        })
        .unwrap();
        let refused = s.write_sync(|w| retry(w, &c.attempt.id, 10, T1));
        assert_eq!(refused.unwrap_err().code, "conflict");
        // A registration queues the current definition, and says so.
        assert!(
            s.write_sync(|w| register(w, with("s1", vec![changed("test")]), 10, T1))
                .unwrap()
        );
        let current = s
            .read_sync(|r| current_attempts(r, "repo_1", "s1", &[changed("test")]))
            .unwrap();
        assert_eq!(
            (current[0].number, current[0].status.as_str()),
            (2, "queued")
        );
    }

    #[test]
    fn an_admitted_run_runs_only_jobs_without_a_current_attempt() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "feature", &["a", "b"]), 10, T0))
            .unwrap();
        for _ in 0..2 {
            let c = s.write_sync(|w| claim(w, "r", 60, T0)).unwrap().unwrap();
            s.write_sync(|w| {
                finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "success",
                    None,
                    ResultInfo::default(),
                    10,
                    T0,
                )
            })
            .unwrap();
        }
        // The queue is full when the run takes a pipeline with a new b.
        s.write_sync(|w| register(w, reg("s2", "other", &["x"]), 1, T0))
            .unwrap();
        s.write_sync(|w| register(w, with("s1", vec![job("a"), changed("b")]), 1, T0))
            .unwrap();
        let run = s
            .read_sync(|r| get_run(r, "repo_1", "s1"))
            .unwrap()
            .unwrap();
        assert!(run.waiting);
        s.write_sync(|w| {
            let x = attempts_of(w, "repo_1", "s2")?;
            cancel(w, &x[0].id, "ken", T1)
        })
        .unwrap();
        assert_eq!(
            s.write_sync(|w| admit_waiting(w, 1, &BTreeMap::new(), T1))
                .unwrap()
                .len(),
            1
        );
        let all = s.read_sync(|r| attempts_of(r, "repo_1", "s1")).unwrap();
        let shape: Vec<(&str, u64, &str)> = all
            .iter()
            .map(|a| (a.job.name.as_str(), a.number, a.status.as_str()))
            .collect();
        assert_eq!(
            shape,
            vec![
                ("a", 1, "completed"),
                ("b", 1, "completed"),
                ("b", 2, "queued")
            ]
        );
    }

    #[test]
    fn queued_attempts_fail_to_start_without_a_runner() {
        let (_d, s) = store();
        s.write_sync(|w| register(w, reg("s1", "a", &["x"]), 10, T0))
            .unwrap();
        assert!(
            s.write_sync(|w| fail_queued(w, T0, "no runner", T1))
                .unwrap()
                .is_empty()
        );
        assert_eq!(
            s.write_sync(|w| fail_queued(w, T1, "no runner", T1))
                .unwrap()
                .len(),
            1
        );
        let l = latest(&s, "s1");
        assert_eq!(l[0].conclusion.as_deref(), Some("startup_failure"));
        assert_eq!(l[0].details.as_deref(), Some("no runner; retry the job."));
        // Retry makes attempt 2.
        let a2 = s.write_sync(|w| retry(w, &l[0].id, 10, T1)).unwrap();
        assert_eq!(a2.number, 2);
        assert_eq!(latest(&s, "s1")[0].id, a2.id);
    }
}

#[cfg(test)]
#[path = "priority_tests.rs"]
mod priority_tests;

#[cfg(test)]
#[path = "reuse_tests.rs"]
mod reuse_tests;
