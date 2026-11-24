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

pub use gb_runner::ci::queue::{Basis, ResultInfo, Reuse};
use std::collections::{BTreeMap, BTreeSet};

use gb_runner::ci::{api::Machine, config::JobLimits, pipeline::CacheOwner};
use serde::{Deserialize, Serialize};

use super::pipeline::JobDef;
use super::priority::Class;
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
const RUNNER_RETRY_GRACE_SECS: u64 = 30;

const MAX_DETAILS: usize = 4 << 10;

/// What the run's pipeline turned out to be.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum PipelineState {
    /// The commit has no `.guildbotics-ci.toml`.
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
    pub post_merge_jobs: Vec<String>,
    pub bases: Vec<SelectionBase>,
    pub skipped_jobs: Vec<String>,
}

/// A service decision, never a conclusion supplied by a runner.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct PathSkip {
    pub reason: SkipReason,
    pub paths: Vec<String>,
    pub bases: Vec<SelectionBase>,
}

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SkipReason {
    Paths,
    PostMerge,
}

impl PathSkip {
    pub fn reason(&self) -> String {
        if self.reason == SkipReason::PostMerge {
            return "post-merge only (repository setting)".into();
        }
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
    /// The time this exact head was incorporated into a protected branch.
    pub cache_landed_at: Option<String>,
    /// The input digest of every job of the pipeline with paths, by job
    /// name, as of the latest registration (`Ci::job_inputs`).
    pub inputs: BTreeMap<String, String>,
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
    Probe,
    Historical,
}

/// An automatic retry belongs to the first failed attempt's retry budget.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct AutoRetry {
    pub root_attempt: String,
    pub number: u64,
}

#[derive(Serialize, Deserialize, Clone, Debug, Default, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RunnerSelection {
    pub runner: Option<String>,
    #[serde(default)]
    pub exclude_runners: Vec<String>,
}

impl RunnerSelection {
    pub fn is_empty(&self) -> bool {
        self.runner.is_none() && self.exclude_runners.is_empty()
    }

    fn allows(&self, runner: &str) -> bool {
        let key = util::fold(runner);
        self.runner
            .as_ref()
            .is_none_or(|name| util::fold(name) == key)
            && !self
                .exclude_runners
                .iter()
                .any(|name| util::fold(name) == key)
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct RetryDecision {
    pub source_attempt: String,
    /// None is a scheduling decision by Branvil.
    pub requested_by: Option<String>,
    pub selection: RunnerSelection,
    pub after_failure: bool,
    pub failed_runner: Option<String>,
    pub prefer_other_until: Option<String>,
}

impl RetryDecision {
    fn of(old: &Attempt, requested_by: Option<String>, selection: RunnerSelection) -> Self {
        Self {
            source_attempt: old.id.clone(),
            requested_by,
            selection,
            after_failure: old.failed(),
            failed_runner: old.runner.clone().filter(|_| old.failed()),
            prefer_other_until: None,
        }
    }

    fn is_failed_runner(&self, runner: &str) -> bool {
        self.failed_runner
            .as_ref()
            .is_some_and(|name| util::fold(name) == util::fold(runner))
    }

    fn after_interruption(&self) -> Self {
        Self {
            prefer_other_until: None,
            ..self.clone()
        }
    }
}

#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct RunnerRecovery {
    pub failed_attempt: String,
    pub failed_runner: String,
    pub successful_runner: String,
}

/// One try of one job.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Attempt {
    pub verified_sha: Option<String>,
    pub path_skip: Option<PathSkip>,
    pub id: String,
    pub purpose: Purpose,
    pub auto_retry: Option<AutoRetry>,
    pub retry_decision: Option<RetryDecision>,
    pub runner_deferrals: BTreeMap<String, ResourceDeferral>,
    /// An absence retry stays queued until a runner claims it.
    pub wait_for_runner: bool,
    pub repo: String,
    pub repo_path: String,
    pub sha: String,
    pub job: JobDef,
    /// The configuration digest of the image the job ran on, as its runner
    /// reported it (none when it did not get that far).
    pub image_digest: Option<String>,
    pub allocation: Option<Allocation>,
    pub peak_memory_mib: Option<u64>,
    /// The actual execution whose result this attempt reused, reported at finish.
    pub reused_from: Option<String>,
    /// The computed dependency-cache key of an execution.
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

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq)]
pub struct Allocation {
    pub cpus: u32,
    pub vm_memory_mib: u64,
    pub reserved_memory_mib: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct ResourceWait {
    pub attempt_id: String,
    pub repo: String,
    pub job: String,
    pub cpu_shortfall: u64,
    pub memory_shortfall_mib: u64,
    pub commit_shortfall_mib: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct ResourceDeferral {
    pub at: String,
    #[serde(flatten)]
    pub wait: ResourceWait,
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
struct Resources {
    cpus: u64,
    memory: u64,
}

impl Resources {
    fn minus(self, other: Self) -> Self {
        Self {
            cpus: self.cpus.saturating_sub(other.cpus),
            memory: self.memory.saturating_sub(other.memory),
        }
    }

    fn plus(self, other: Self) -> Self {
        Self {
            cpus: self.cpus.saturating_add(other.cpus),
            memory: self.memory.saturating_add(other.memory),
        }
    }
}

impl From<Allocation> for Resources {
    fn from(a: Allocation) -> Self {
        Self {
            cpus: a.cpus.into(),
            memory: a.reserved_memory_mib,
        }
    }
}

impl Attempt {
    pub fn failed(&self) -> bool {
        matches!(
            self.conclusion.as_deref(),
            Some("failure" | "timed_out" | "startup_failure")
        )
    }

    pub fn runner_recovery(&self, history: &[Attempt]) -> Option<RunnerRecovery> {
        let runner = self.runner.as_ref()?;
        if self.conclusion.as_deref() != Some("success") || self.reused_from.is_some() {
            return None;
        }
        let failed = history
            .iter()
            .rev()
            .find(|a| {
                a.purpose == Purpose::Check
                    && a.job == self.job
                    && a.number < self.number
                    && (a.failed() || a.conclusion.as_deref() == Some("success"))
                    && a.runner.is_some()
                    && a.reused_from.is_none()
            })
            .filter(|a| a.failed())?;
        let failed_runner = failed.runner.as_ref()?;
        (util::fold(failed_runner) != util::fold(runner)).then(|| RunnerRecovery {
            failed_attempt: failed.id.clone(),
            failed_runner: failed_runner.clone(),
            successful_runner: runner.clone(),
        })
    }

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
            Purpose::Probe => "the narrowing schedules the next probe",
            Purpose::Historical => "the attempt is historical",
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
    if a.conclusion.as_deref() == Some("success")
        && a.reused_from.is_none()
        && let (Some(allocation), Some(peak), Some(finished)) =
            (a.allocation, a.peak_memory_mib, a.finished_at.as_deref())
        && let Ok(time) = chrono::DateTime::parse_from_rfc3339(finished)
        && let Ok(millis) = u64::try_from(time.timestamp_millis())
    {
        out.push(IxEntry {
            table: store::IX_CI_MEMORY,
            key: memory_key(a, allocation.cpus, allocation.vm_memory_mib)
                .u(u64::MAX - millis)
                .s(&a.id)
                .bytes(),
            value: peak.to_be_bytes().to_vec(),
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

fn memory_key(a: &Attempt, cpus: u32, memory_mib: u64) -> Key {
    Key::new()
        .s(&a.repo)
        .s(&a.job.name)
        .u(cpus.into())
        .u(memory_mib)
}

/// Every CI index entry the records imply (for `server hub-data check`).
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

pub fn put_run(w: &Wtx, new: &Run) -> AppResult<()> {
    let old = get_run(w, &new.repo, &new.sha)?;
    if old.as_ref() == Some(new) {
        return Ok(());
    }
    put_indexed(
        w,
        store::CI_RUNS,
        &run_key(&new.repo, &new.sha),
        old.as_ref().map(run_index),
        new,
        run_index(new),
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

pub(crate) fn keeps_cache(r: &impl Rd, a: &Attempt) -> AppResult<bool> {
    if a.job.cache.is_none() || a.purpose != Purpose::Check {
        return Ok(false);
    }
    if a.cache_scope == "protected" {
        return Ok(true);
    }
    let Some(run) = get_run(r, &a.repo, &a.sha)? else {
        return Ok(false);
    };
    Ok(run.cache_landed_at.is_some() || cache_head(r, &run)?)
}

fn cache_head(r: &impl Rd, run: &Run) -> AppResult<bool> {
    let Some(repo) =
        r.get::<crate::model::Repo>(store::REPOS, &crate::model::Repo::key(&run.repo))?
    else {
        return Ok(false);
    };
    for branch in &run.branches {
        let reference = format!("refs/heads/{branch}");
        let tip: Option<String> =
            r.get(store::REFS, &Key::new().s(&run.repo).s(&reference).bytes())?;
        if tip.as_deref() != Some(&run.sha) {
            continue;
        }
        for (_, value) in r.prefix_raw(
            store::IX_OPEN_PULLS,
            &Key::new().s(&run.repo).s(branch).bytes(),
            None,
            usize::MAX,
        )? {
            let number = String::from_utf8_lossy(&value)
                .parse()
                .map_err(AppError::internal)?;
            let key = crate::model::Pull::key(&run.repo, number);
            if let Some(pull) = r.get::<crate::model::Pull>(store::PULLS, &key)?
                && repo.is_protected(&pull.base)
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

pub(crate) fn cache_decision(
    r: &impl Rd,
    runner: &str,
    id: &str,
) -> AppResult<gb_runner::ci::api::CacheDecision> {
    use gb_runner::ci::api::CacheDecision::{Discard, Keep, Publish};
    let Some(a) = get_attempt(r, id)? else {
        return Ok(Discard);
    };
    if a.runner
        .as_deref()
        .is_none_or(|name| util::fold(name) != util::fold(runner))
        || a.purpose != Purpose::Check
        || a.job.cache.is_none()
    {
        return Ok(Discard);
    }
    if a.status == "in_progress" {
        return Ok(Keep);
    }
    if a.conclusion.as_deref() != Some("success")
        || a.reused_from.is_some()
        || a.cache_key.is_none()
    {
        return Ok(Discard);
    }
    let Some(run) = get_run(r, &a.repo, &a.sha)? else {
        return Ok(Discard);
    };
    let at = if a.cache_scope == "protected" {
        a.finished_at.as_deref()
    } else {
        run.cache_landed_at.as_deref()
    };
    if let Some(at) = at {
        let at = chrono::DateTime::parse_from_rfc3339(at).map_err(AppError::internal)?;
        return Ok(Publish(at.to_utc()));
    }
    Ok(if cache_head(r, &run)? { Keep } else { Discard })
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
/// protected branch wants the commit (a fast-forward merge), the jobs whose
/// current attempt reused another tree's result by the contents of paths.
pub(super) fn jobs_to_run(r: &impl Rd, run: &Run) -> AppResult<Vec<JobDef>> {
    let PipelineState::Jobs { jobs } = &run.pipeline else {
        return Ok(Vec::new());
    };
    let current = current_attempts(r, &run.repo, &run.sha, jobs)?;
    let protected = run.cache_scope == "protected";
    let repair = crate::repair::is_run(r, run)?;
    let mut out = Vec::new();
    for j in jobs.iter().filter(|j| !run.skips(&j.name)) {
        let again = match current.iter().find(|a| a.job.name == j.name) {
            None => true,
            Some(a) => {
                (protected
                    && a.status == "completed"
                    && a.conclusion.as_deref() != Some("success")
                    && a.cache_scope != "protected")
                    || a.superseded
                    || a.path_skip.is_some()
                    || ((protected || repair) && source_of(r, a)?.basis == Some(Basis::Paths))
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
#[cfg(test)]
pub fn pending_count(r: &impl Rd, limit: usize) -> AppResult<usize> {
    Ok(r.prefix_raw(store::IX_CI_PENDING, &[], None, limit)?.len())
}

/// Capacity for the incoming class, with a hypothetical manual retry applied.
pub(super) fn admission_count(
    r: &impl Rd,
    class: super::priority::Class,
    limit: usize,
    retry_id: Option<String>,
) -> AppResult<usize> {
    let mut snapshot = retry_id
        .as_deref()
        .map(super::priority::Snapshot::retrying)
        .unwrap_or_default();
    let mut count = 0;
    for (_, id) in r.prefix_raw(store::IX_CI_PENDING, &[], None, usize::MAX)? {
        let a = get_attempt(r, &String::from_utf8_lossy(&id))?
            .ok_or_else(|| AppError::internal("pending attempt is missing"))?;
        let pending_class = snapshot.class_of(r, &a)?.class;
        if pending_class != super::priority::Class::Verify
            && (class >= super::priority::Class::Deferred
                || pending_class < super::priority::Class::Deferred)
        {
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
    new_job(
        w,
        (&run.repo, &run.repo_path, &run.sha, &run.cache_scope),
        job,
        network,
        now,
    )
}

fn new_job(
    w: &Wtx,
    (repo, path, sha, scope): (&str, &str, &str, &str),
    job: &JobDef,
    network: bool,
    now: &str,
) -> AppResult<Attempt> {
    let number = next_number(w, repo, sha, &job.name)?;
    Ok(Attempt {
        verified_sha: None,
        path_skip: None,
        id: util::new_id("att"),
        purpose: Purpose::Check,
        auto_retry: None,
        retry_decision: None,
        runner_deferrals: BTreeMap::new(),
        wait_for_runner: false,
        repo: repo.into(),
        repo_path: path.into(),
        sha: sha.into(),
        job: job.clone(),
        image_digest: None,
        allocation: None,
        peak_memory_mib: None,
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
        cache_scope: scope.into(),
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
                run.cache_landed_at.get_or_insert_with(|| now.to_owned());
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
            cache_landed_at: reg.protected.then(|| now.to_owned()),
            branches: vec![reg.branch],
            pipeline: reg.pipeline,
            cache_scope: scope,
            waiting: false,
            superseded_by: None,
            inputs: BTreeMap::new(),
        },
    };
    run.inputs = reg.inputs;
    run.selection = if run.cache_scope == "protected" {
        None
    } else {
        reg.selection
    };
    if run.cache_scope == "protected"
        && old
            .as_ref()
            .is_some_and(|old| old.cache_scope != "protected")
    {
        for a in attempts_of(w, &run.repo, &run.sha)? {
            if a.retry_decision
                .as_ref()
                .is_some_and(|retry| !retry.selection.is_empty())
            {
                stop(
                    w,
                    &a,
                    "Stopped: protected-branch checks select their runner automatically.",
                    true,
                    now,
                )?;
            }
        }
    }
    // Skipped attempts consume no queue capacity, including while waiting.
    let mut skipped = false;
    if let PipelineState::Jobs { jobs } = &run.pipeline {
        let current = current_attempts(w, &run.repo, &run.sha, jobs)?;
        for job in jobs.iter().filter(|j| run.skips(&j.name)) {
            let skip = PathSkip {
                reason: if run
                    .selection
                    .as_ref()
                    .unwrap()
                    .post_merge_jobs
                    .contains(&job.name)
                {
                    SkipReason::PostMerge
                } else {
                    SkipReason::Paths
                },
                paths: job.paths.clone(),
                bases: run.selection.as_ref().unwrap().bases.clone(),
            };
            let old = current.iter().find(|a| a.job.name == job.name);
            // Keep real executions and manual retries. Refresh only decisions.
            if old.is_some_and(|a| {
                !a.superseded && a.path_skip.is_none() && (a.status != "queued" || !a.allow_reuse)
            }) || old.is_some_and(|a| a.path_skip.as_ref() == Some(&skip))
            {
                continue;
            }
            let mut a = match old.filter(|a| a.status == "queued" && !a.superseded) {
                Some(a) => a.clone(),
                None => new_attempt(w, &run, job, reg.network, now)?,
            };
            conclude(&mut a, "skipped", Some(skip.reason()), now);
            a.path_skip = Some(skip);
            put_attempt(w, old.filter(|old| old.id == a.id), &a)?;
            skipped = true;
        }
    }
    let queued = !run.waiting && {
        let todo = jobs_to_run(w, &run)?;
        admit_jobs(w, &mut run, &todo, &reg.order, reg.network, queue_max, now)?
    };
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
    if !super::verify::running(w, &run.repo, &run.sha)?
        && admission_count(
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
    if super::verify::running(w, repo, sha)? {
        return Ok(false);
    }
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

/// The basis on which `a` (with the scope and inputs copied at its claim)
/// may reuse a result. Protected and gate runs check every job for real, so
/// they reuse only the same tree; so do jobs without paths and attempts
/// whose run has no digest of the job.
fn basis(a: &Attempt) -> Basis {
    if a.cache_scope == "protected" || a.job.paths.is_empty() || a.inputs.is_none() {
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

fn reusable_shape(a: &Attempt, source: &Attempt) -> bool {
    a.purpose == Purpose::Check
        && a.allow_reuse
        && a.id != source.id
        && a.repo == source.repo
        && a.job == source.job
        // Distributed binaries can embed the commit even when the tree is identical.
        && (a.job.artifacts.is_empty() || a.sha == source.sha)
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
    if a.job.verified_sha
        && (a.verified_sha != source.verified_sha
            || tree_of(r, a)?.is_none()
            || tree_of(r, a)? != tree_of(r, source)?)
    {
        return Ok(false);
    }
    let repair = get_run(r, &a.repo, &a.sha)?
        .as_ref()
        .map(|run| crate::repair::is_run(r, run))
        .transpose()?
        .unwrap_or(false);
    Ok(match if repair { Basis::Tree } else { basis(a) } {
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
    Ok(scheduled_order(r, first_only)?
        .into_iter()
        .map(|(a, reuse, _)| (a, reuse))
        .collect())
}

type Scheduled = (Attempt, Option<Reuse>, Class);

fn scheduled_order(r: &impl Rd, first_only: bool) -> AppResult<Vec<Scheduled>> {
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
        if a.purpose == Purpose::Check
            && let Some(run) = &run
        {
            a.cache_scope = run.cache_scope.clone();
        }
        if a.job.verified_sha {
            a.verified_sha = super::verify::latest_verified(r, &a.repo)?.map(|v| v.sha);
        }
        let repair = run
            .as_ref()
            .map(|run| crate::repair::is_run(r, run))
            .transpose()?
            .unwrap_or(false);
        a.inputs = if repair {
            None
        } else {
            run.and_then(|run| run.inputs.get(&a.job.name).cloned())
        };
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
            let eligible = sources
                .iter()
                .map(|source| Ok((source, can_reuse(r, &a, source)?)))
                .collect::<AppResult<Vec<_>>>()?;
            reuse_candidate(
                &a,
                &eligible
                    .into_iter()
                    .filter(|(_, ok)| *ok)
                    .map(|(s, _)| s.clone())
                    .collect::<Vec<_>>(),
            )
        } else {
            None
        };
        if first_only && reuse.is_some() {
            return Ok(vec![(a, reuse, class)]);
        }
        ordered.push((a, reuse, class));
    }
    ordered.sort_by_key(|(a, reuse, class)| (*class, reuse.is_none(), a.seq));
    Ok(ordered)
}

/// Hands the highest-priority queued attempt to `runner` under a new lease.
#[cfg(test)]
pub fn claim(w: &Wtx, runner: &str, lease_secs: u64, now: &str) -> AppResult<Option<Claimed>> {
    let Some((a, reuse)) = scheduled(w, true)?.into_iter().next() else {
        return Ok(None);
    };
    claim_selected(w, runner, lease_secs, now, a, reuse, None).map(Some)
}

#[cfg_attr(test, derive(Clone))]
pub struct ClaimRunner {
    pub name: String,
    pub images: Vec<super::images::Image>,
    pub limits: JobLimits,
    pub machine: Option<Machine>,
    pub ram_fresh: bool,
    pub cache_groups: Option<BTreeSet<String>>,
    pub ram_unsupported_images: BTreeSet<String>,
}

impl ClaimRunner {
    fn ram_memory(&self, budget: &Budget, reserve: Resources) -> Option<Machine> {
        let mut machine = self.machine.clone().filter(|_| self.ram_fresh)?;
        if let Some((_, free)) = budget.physical {
            let host_reserve = self.limits.memory_reserve_mib(machine.total_memory_mib);
            machine.available_memory_mib = machine.available_memory_mib.min(
                free.memory
                    .saturating_sub(reserve.memory)
                    .saturating_add(host_reserve),
            );
        }
        if let Some(commit) = machine.commit.as_mut() {
            commit.available_mib = budget.commit.unwrap_or_default();
        }
        Some(machine)
    }

    fn ram_allows(&self, a: &Attempt, machine: Option<&Machine>) -> bool {
        let (Some(ram), Some(cache), Some(machine)) = (&a.job.ram_workspace, &a.job.cache, machine)
        else {
            return false;
        };
        ram.memory_allows(
            a.job.tmpfs_mib,
            self.limits.max_memory_mib,
            self.limits,
            machine,
        ) && self.cache_groups.as_ref().is_none_or(|groups| {
            groups.contains(&CacheOwner::for_job(&a.repo_path, &a.job, cache).fingerprint())
        })
    }

    fn budget(&self, active: &[Attempt]) -> Option<Budget> {
        let Some(machine) = &self.machine else {
            return Some(Budget::default());
        };
        let key = util::fold(&self.name);
        let running = active
            .iter()
            .filter(|a| a.runner.as_deref().map(util::fold).as_ref() == Some(&key))
            .collect::<Vec<_>>();
        if running.len() >= self.limits.max_jobs as usize {
            return None;
        }
        let commit = machine.commit.as_ref().map(|commit| {
            running
                .iter()
                .filter(|a| !commit.started_attempts.contains(&a.id))
                .filter_map(|a| a.allocation)
                .fold(commit.available_mib, |free, a| {
                    free.saturating_sub(a.vm_memory_mib)
                })
        });
        Some(Budget {
            physical: Some(capacity(machine, self.limits, &running)),
            commit,
        })
    }
}

#[derive(Default)]
struct Budget {
    physical: Option<(Resources, Resources)>,
    commit: Option<u64>,
}

impl Budget {
    fn shortfall(&self, allocation: Allocation, reserve: Resources) -> (Resources, u64) {
        let physical = self.physical.map_or_else(Resources::default, |(_, free)| {
            Resources::from(allocation).minus(free.minus(reserve))
        });
        let commit = self
            .commit
            .map_or(0, |free| allocation.vm_memory_mib.saturating_sub(free));
        (physical, commit)
    }
}

fn allocation(r: &impl Rd, a: &Attempt, limits: JobLimits) -> AppResult<Allocation> {
    Ok(Allocation {
        cpus: limits.max_cpus,
        vm_memory_mib: limits.max_memory_mib,
        reserved_memory_mib: estimate_memory(r, a, limits)?,
    })
}

/// Choose a compatible attempt and reserve its capacity in the lease transaction.
pub fn claim_images(
    w: &Wtx,
    runner: ClaimRunner,
    lease_secs: u64,
    now: &str,
    alternatives: impl FnOnce() -> AppResult<Vec<ClaimRunner>>,
) -> AppResult<(Option<Claimed>, Option<ResourceWait>)> {
    let active = active(w)?;
    let ordered = scheduled_order(w, false)?;
    let ram = |a: &Attempt| {
        a.purpose == Purpose::Check && a.job.ram_workspace.is_some() && a.job.cache.is_some()
    };
    let peers = if ordered.iter().any(|(a, _, _)| {
        ram(a)
            || a.retry_decision.as_ref().is_some_and(|retry| {
                retry
                    .prefer_other_until
                    .as_deref()
                    .is_some_and(|until| now >= until)
            })
    }) {
        alternatives()?
    } else {
        Vec::new()
    };
    let scan = ClaimScan {
        ordered: &ordered,
        active: &active,
        now,
        runners: std::iter::once(&runner).chain(&peers).collect(),
    };
    let (choices, waits) = scan.choices(w, &runner)?;
    for wait in &waits {
        let a = &ordered[wait.index].0;
        if (wait.reason.commit_shortfall_mib > 0
            || a.retry_decision
                .as_ref()
                .is_some_and(|retry| retry.selection.runner.is_some()))
            && a.runner_deferrals
                .get(&runner.name)
                .is_none_or(|previous| previous.wait != wait.reason)
        {
            // Do not persist the run-derived fields of the scheduling copy.
            let mut stored = get_attempt(w, &a.id)?
                .ok_or_else(|| AppError::internal("pending attempt is missing"))?;
            stored.runner_deferrals.insert(
                runner.name.clone(),
                ResourceDeferral {
                    at: now.into(),
                    wait: wait.reason.clone(),
                },
            );
            w.put(store::CI_ATTEMPTS, &Attempt::key(&a.id), &stored)?;
        }
    }
    let waiting = waits.into_iter().next().map(|wait| wait.reason);
    let Some(&first) = choices.first() else {
        return Ok((None, waiting));
    };
    let chosen = if first.allocation.is_some() && choices.len() > 1 && !first.ram {
        scan.prefer_ram(w, &choices, &peers)?
    } else {
        first
    };
    let (a, reuse, _) = &ordered[chosen.index];
    let claim = claim_selected(
        w,
        &runner.name,
        lease_secs,
        now,
        a.clone(),
        reuse.clone(),
        chosen.allocation,
    )?;
    Ok((Some(claim), waiting))
}

#[derive(Clone, Copy)]
struct ClaimChoice {
    index: usize,
    allocation: Option<Allocation>,
    ram: bool,
}

struct ClaimWait {
    index: usize,
    reason: ResourceWait,
}

struct ClaimScan<'a> {
    ordered: &'a [Scheduled],
    active: &'a [Attempt],
    now: &'a str,
    runners: Vec<&'a ClaimRunner>,
}

impl ClaimScan<'_> {
    fn choices(
        &self,
        r: &impl Rd,
        runner: &ClaimRunner,
    ) -> AppResult<(Vec<ClaimChoice>, Vec<ClaimWait>)> {
        let mut choices: Vec<ClaimChoice> = Vec::new();
        let mut waiting = Vec::new();
        let Some(budget) = runner.budget(self.active) else {
            return Ok((choices, waiting));
        };
        let mut reserve = Resources::default();
        let mut ram_memory = runner.ram_memory(&budget, reserve);
        let mut estimates = BTreeMap::new();
        for (index, (a, reuse, class)) in self.ordered.iter().enumerate() {
            if let Some(first) = choices.first() {
                let (head, head_reuse, head_class) = &self.ordered[first.index];
                // RAM can change the job within this part of the queue,
                // without moving it across a different group or reuse tier.
                if *class != *head_class
                    || reuse.is_some() != head_reuse.is_some()
                    || a.purpose != Purpose::Check
                    || head.purpose != Purpose::Check
                    || a.repo != head.repo
                    || a.sha != head.sha
                {
                    break;
                }
            }
            let Some(image) = runner.images.iter().find(|i| i.supports(&a.job.image)) else {
                continue;
            };
            if let Some(retry) = &a.retry_decision {
                if !retry.selection.allows(&runner.name) {
                    continue;
                }
                if let Some(until) = retry.prefer_other_until.as_deref()
                    && retry.is_failed_runner(&runner.name)
                    && (self.now < until
                        || alternative_can_run(r, a, &self.runners, runner, self.active)?)
                {
                    continue;
                }
            }
            if reuse
                .as_ref()
                .is_some_and(|source| source.image_digest == image.digest)
            {
                if choices.is_empty() {
                    choices.push(ClaimChoice {
                        index,
                        allocation: None,
                        ram: false,
                    });
                }
                break;
            }
            let key = (&a.repo, &a.job.name);
            let allocation = match estimates.get(&key) {
                Some(&allocation) => allocation,
                None => {
                    let allocation = allocation(r, a, runner.limits)?;
                    estimates.insert(key, allocation);
                    allocation
                }
            };
            let (short, commit_shortfall_mib) = budget.shortfall(allocation, reserve);
            if short == Resources::default() && commit_shortfall_mib == 0 {
                choices.push(ClaimChoice {
                    index,
                    allocation: Some(allocation),
                    ram: a.purpose == Purpose::Check
                        && runner.ram_allows(a, ram_memory.as_ref())
                        && !runner.ram_unsupported_images.contains(&image.digest),
                });
            } else if choices.is_empty() {
                waiting.push(ClaimWait {
                    index,
                    reason: ResourceWait {
                        attempt_id: a.id.clone(),
                        repo: a.repo_path.clone(),
                        job: a.job.name.clone(),
                        cpu_shortfall: short.cpus,
                        memory_shortfall_mib: short.memory,
                        commit_shortfall_mib,
                    },
                });
                let need = Resources::from(allocation);
                if reserve == Resources::default()
                    && let Some((total, _)) = budget.physical
                    && need.minus(total) == Resources::default()
                {
                    reserve = need;
                    ram_memory = runner.ram_memory(&budget, reserve);
                }
            }
        }
        Ok((choices, waiting))
    }

    fn prefer_ram(
        &self,
        r: &impl Rd,
        choices: &[ClaimChoice],
        peers: &[ClaimRunner],
    ) -> AppResult<ClaimChoice> {
        let first = choices[0];
        if let Some(&ram) = choices.iter().find(|choice| choice.ram) {
            // If the peer would otherwise execute this job and cannot start
            // ours, keep both slots working.
            for peer in peers {
                let (other, _) = self.choices(r, peer)?;
                if other
                    .first()
                    .is_some_and(|choice| choice.index == ram.index)
                    && !other.iter().any(|choice| choice.index == first.index)
                {
                    return Ok(first);
                }
            }
            return Ok(ram);
        }
        if self.ordered[first.index].0.job.ram_workspace.is_some() {
            // A disk runner that polls first can take another job immediately
            // when a peer can take its first job in RAM in this same group.
            for peer in peers
                .iter()
                .filter(|peer| peer.ram_fresh && peer.machine.is_some())
            {
                let (other, _) = self.choices(r, peer)?;
                if other
                    .iter()
                    .find(|choice| choice.ram)
                    .is_some_and(|choice| choice.index == first.index)
                {
                    return Ok(choices[1..]
                        .iter()
                        .copied()
                        .find(|candidate| {
                            !other
                                .iter()
                                .any(|peer| peer.index == candidate.index && peer.ram)
                        })
                        .unwrap_or(choices[1]));
                }
            }
        }
        Ok(first)
    }
}

fn alternative_can_run(
    r: &impl Rd,
    attempt: &Attempt,
    alternatives: &[&ClaimRunner],
    requester: &ClaimRunner,
    active: &[Attempt],
) -> AppResult<bool> {
    for runner in alternatives {
        if util::fold(&runner.name) == util::fold(&requester.name)
            || attempt
                .retry_decision
                .as_ref()
                .is_some_and(|retry| !retry.selection.allows(&runner.name))
        {
            continue;
        }
        let jobs = active
            .iter()
            .filter(|a| {
                a.runner
                    .as_deref()
                    .is_some_and(|name| util::fold(name) == util::fold(&runner.name))
            })
            .count();
        if jobs < runner.limits.max_jobs as usize
            && runner.images.iter().any(|i| i.supports(&attempt.job.image))
            && let Some(budget) = runner.budget(active)
            && budget.shortfall(allocation(r, attempt, runner.limits)?, Resources::default())
                == (Resources::default(), 0)
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn capacity(machine: &Machine, limits: JobLimits, running: &[&Attempt]) -> (Resources, Resources) {
    let logical_cpus = u64::from(machine.logical_cpus);
    let cpus = match limits.cpu_multiplier_percent {
        0 => logical_cpus.max(u64::from(limits.max_cpus) * u64::from(limits.max_jobs)),
        percent => logical_cpus * u64::from(percent) / 100,
    };
    let reserve = limits.memory_reserve_mib(machine.total_memory_mib);
    let total = Resources {
        cpus,
        memory: machine.total_memory_mib.saturating_sub(reserve),
    };
    let used = running
        .iter()
        .filter_map(|a| a.allocation)
        .fold(Resources::default(), |used, a| used.plus(a.into()));
    let free = total.minus(used);
    let available = machine.available_memory_mib.saturating_sub(reserve);
    (
        total,
        Resources {
            memory: free.memory.min(available),
            ..free
        },
    )
}

fn estimate_memory(r: &impl Rd, a: &Attempt, limits: JobLimits) -> AppResult<u64> {
    let prefix = memory_key(a, limits.max_cpus, limits.max_memory_mib).bytes();
    let mut samples = r
        .prefix_raw(store::IX_CI_MEMORY, &prefix, None, 20)?
        .into_iter()
        .map(|(_, bytes)| {
            bytes
                .try_into()
                .map(u64::from_be_bytes)
                .map_err(|_| AppError::internal("invalid CI memory sample"))
        })
        .collect::<AppResult<Vec<_>>>()?;
    if samples.is_empty() {
        return Ok(limits.max_memory_mib);
    }
    samples.sort_unstable();
    Ok(samples[(samples.len() * 9).div_ceil(10) - 1])
}

#[cfg(any(test, feature = "test-support"))]
pub(crate) fn claim_fixture(w: &Wtx, target: (&str, &str), now: &str) -> AppResult<Claimed> {
    let (a, reuse) = scheduled(w, false)?
        .into_iter()
        .find(|(a, _)| a.repo_path == target.0 && a.sha == target.1)
        .ok_or_else(|| AppError::internal("fixture head has no queued check"))?;
    claim_selected(w, "fixture", 300, now, a, reuse, None)
}

fn claim_selected(
    w: &Wtx,
    runner: &str,
    lease_secs: u64,
    now: &str,
    mut a: Attempt,
    reuse: Option<Reuse>,
    allocation: Option<Allocation>,
) -> AppResult<Claimed> {
    let old = get_attempt(w, &a.id)?.unwrap();
    let lease = format!("bvl_{}", util::random_secret::<24>());
    a.status = "in_progress".into();
    a.wait_for_runner = false;
    a.lease_hash = Some(lease_hash(&lease));
    a.lease_expires_at = Some(later(now, lease_secs));
    a.runner = Some(runner.to_string());
    a.allocation = allocation;
    a.started_at = Some(now.to_string());
    put_attempt(w, Some(&old), &a)?;
    Ok(Claimed {
        attempt: a,
        lease,
        reuse,
    })
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
pub fn finish(
    w: &Wtx,
    id: &str,
    lease: &str,
    conclusion: &str,
    details: Option<String>,
    info: ResultInfo,
    now: &str,
) -> AppResult<Attempt> {
    if !CONCLUSIONS.contains(&conclusion) && conclusion != "deferred" {
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
    if conclusion == "deferred" && !a.cancel_requested {
        if a.allocation.is_some() {
            return Err(AppError::invalid("An execution cannot defer a held lease."));
        }
        // A reuse claim can lose its image between the inventory and lookup.
        // Return its position; a new claim must admit any actual execution.
        a.status = "queued".into();
        a.lease_hash = None;
        a.lease_expires_at = None;
        a.runner = None;
        a.started_at = None;
        put_attempt(w, Some(&old), &a)?;
        return Ok(a);
    }
    if let Some(key) = &info.cache_key
        && (key.len() != 64 || !key.bytes().all(|b| b.is_ascii_hexdigit()) || a.job.cache.is_none())
    {
        return Err(AppError::invalid(
            "cache_key must be a SHA-256 key of a job with a cache.",
        ));
    }
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
    a.peak_memory_mib = info.peak_memory_mib.filter(|peak| *peak > 0);
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
    }
    let next = if a.failed() {
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
    if w.get::<crate::model::Repo>(store::REPOS, &crate::model::Repo::key(&old.repo))?
        .is_none()
    {
        return Ok(None);
    }
    let PipelineState::Jobs { jobs } = &run.pipeline else {
        return Ok(None);
    };
    if !current_attempts(w, &old.repo, &old.sha, jobs)?
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
    let mut decision = RetryDecision::of(old, None, RunnerSelection::default());
    decision.after_failure |= old
        .retry_decision
        .as_ref()
        .is_some_and(|retry| retry.after_failure);
    if decision.failed_runner.is_some() {
        decision.prefer_other_until = Some(later(now, RUNNER_RETRY_GRACE_SECS));
    }
    next.retry_decision = Some(decision);
    next.allow_reuse = false;
    next.wait_for_runner = wait_for_runner;
    Ok(Some(next))
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
#[cfg(any(test, feature = "test-support"))]
pub fn retry(w: &Wtx, id: &str, queue_max: usize, now: &str) -> AppResult<Attempt> {
    retry_selected(w, id, queue_max, now, "fixture", RunnerSelection::default())
}

pub fn runner_selection_supported(r: &impl Rd, run: &Run) -> AppResult<bool> {
    if run.cache_scope == "protected" {
        return Ok(false);
    }
    for branch in &run.branches {
        let prefix = Key::new().s(&run.repo).s(branch).bytes();
        if !r
            .prefix_raw(store::IX_OPEN_PULLS, &prefix, None, 1)?
            .is_empty()
        {
            return Ok(true);
        }
    }
    Ok(false)
}

pub fn retry_selected(
    w: &Wtx,
    id: &str,
    queue_max: usize,
    now: &str,
    who: &str,
    selection: RunnerSelection,
) -> AppResult<Attempt> {
    let old =
        get_attempt(w, id)?.ok_or_else(|| AppError::not_found(format!("No CI attempt {id}.")))?;
    if old.purpose != Purpose::Check {
        return Err(AppError::conflict(
            "Only check attempts can be manually retried.",
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
    if !selection.is_empty() && !runner_selection_supported(w, &run)? {
        return Err(AppError::conflict_code(
            "retry_pr_head_required",
            "Runner selection requires a current, unprotected pull request head.",
        ));
    }
    let decision = RetryDecision::of(&old, Some(who.into()), selection);
    let mut snapshot = super::priority::Snapshot::retrying(&old.id);
    let mut replacement = old.clone();
    replacement.retry_decision = Some(decision.clone());
    let class = snapshot.class_of(w, &replacement)?.class;
    let reopen = super::verify::get(w, &old.repo)?.filter(|v| {
        v.state == "failed" && v.sha == old.sha && old.conclusion.as_deref() != Some("success")
    });
    let reopen = if let Some(v) = reopen {
        let repo =
            w.get::<crate::model::Repo>(store::REPOS, &crate::model::Repo::key(&old.repo))?;
        let tip: Option<String> = match repo {
            Some(repo) => w.get(
                store::REFS,
                &Key::new()
                    .s(&old.repo)
                    .s(&format!("refs/heads/{}", repo.default_branch))
                    .bytes(),
            )?,
            None => None,
        };
        (tip.as_deref() == Some(&old.sha)).then_some(v)
    } else {
        None
    };
    if reopen.is_none()
        && admission_count(w, class, queue_max + 1, Some(old.id.clone()))? >= queue_max
    {
        return Err(AppError::unavailable(format!(
            "The CI queue is full ({queue_max} queued attempts); retry later."
        )));
    }
    if let Some(mut v) = reopen {
        v.state = "running".into();
        v.failed_jobs.clear();
        v.finished_at = None;
        w.put(
            store::CI_VERIFICATIONS,
            &Key::new().s(&old.repo).bytes(),
            &v,
        )?;
    }
    let network = network_of(w, &old.repo)?;
    let mut a = new_attempt(w, &run, &old.job, network, now)?;
    a.allow_reuse = false;
    a.retry_decision = Some(decision);
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

pub fn running_by_runner(r: &impl Rd) -> AppResult<BTreeMap<String, usize>> {
    let mut counts = BTreeMap::new();
    for attempt in active(r)? {
        if let Some(name) = attempt.runner {
            *counts.entry(crate::util::fold(&name)).or_default() += 1;
        }
    }
    Ok(counts)
}

/// Requeue the same work at its original position without carrying runner state.
fn replacement(w: &Wtx, run: &Run, old: &Attempt, now: &str) -> AppResult<Attempt> {
    let mut next = new_attempt(w, run, &old.job, old.network, now)?;
    next.seq = old.seq;
    next.allow_reuse = old.allow_reuse;
    next.auto_retry = old.auto_retry.clone();
    next.retry_decision = old
        .retry_decision
        .as_ref()
        .map(RetryDecision::after_interruption);
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
/// the automatic retry budget of a protected check.
fn interrupt(w: &Wtx, old: &Attempt, why: &str, now: &str) -> AppResult<()> {
    let mut a = old.clone();
    let requeue = !old.cancel_requested && !old.superseded && !old.after_interrupt;
    // Its definition is the run's: taking another pipeline stops unfinished
    // attempts of definitions it does not have.
    let next = if requeue {
        let mut next = new_job(
            w,
            (&old.repo, &old.repo_path, &old.sha, &old.cache_scope),
            &old.job,
            old.network,
            now,
        )?;
        next.seq = old.seq;
        next.allow_reuse = old.allow_reuse;
        next.auto_retry = old.auto_retry.clone();
        next.retry_decision = old
            .retry_decision
            .as_ref()
            .map(RetryDecision::after_interruption);
        next.purpose = old.purpose;
        next.cache_key = old.cache_key.clone();
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
#[cfg(test)]
pub fn fail_queued(
    w: &Wtx,
    before: &str,
    details: &str,
    now: &str,
) -> AppResult<Vec<(String, String)>> {
    fail_unavailable(w, before, now, |_| Some(details.to_owned()))
}

pub fn fail_unavailable(
    w: &Wtx,
    before: &str,
    now: &str,
    reason: impl Fn(&Attempt) -> Option<String>,
) -> AppResult<Vec<(String, String)>> {
    let mut changed = Vec::new();
    for (_, id) in w.prefix_raw(store::IX_CI_PENDING, &[], None, usize::MAX)? {
        let id = String::from_utf8_lossy(&id).into_owned();
        let Some(old) = get_attempt(w, &id)? else {
            continue;
        };
        // Automatic retries wait for a runner instead of exhausting their
        // entire budget while no runner is available.
        if old.wait_for_runner
            || old
                .retry_decision
                .as_ref()
                .is_some_and(|retry| retry.selection.runner.is_some())
            || old.created_at.as_str() >= before
        {
            continue;
        }
        let Some(details) = reason(&old) else {
            continue;
        };
        let mut a = old.clone();
        conclude(&mut a, "startup_failure", None, now);
        let next = prepare_auto_retry(w, &a, true, now)?;
        a.details = Some(cut(&format!(
            "{details}; {}.",
            old.recovery_hint(next.as_ref())
        )));
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

    fn claim_images(
        w: &Wtx,
        name: &str,
        lease_secs: u64,
        now: &str,
        images: &[super::super::images::Image],
        limits: JobLimits,
        machine: Option<Machine>,
    ) -> AppResult<(Option<Claimed>, Option<ResourceWait>)> {
        super::claim_images(
            w,
            ClaimRunner {
                name: name.into(),
                images: images.to_vec(),
                limits,
                machine,
                ram_fresh: false,
                cache_groups: None,
                ram_unsupported_images: BTreeSet::new(),
            },
            lease_secs,
            now,
            || Ok(Vec::new()),
        )
    }

    fn limits() -> JobLimits {
        JobLimits::from(&gb_runner::ci::config::ExecutorConfig::default())
    }

    fn held_image() -> super::super::images::Image {
        super::super::images::Image {
            reference: job("job").image,
            architecture: "amd64".into(),
            digest: format!("sha256:{}", "2".repeat(64)),
        }
    }

    fn runner(name: &str, vm_mib: u64) -> ClaimRunner {
        ClaimRunner {
            name: name.into(),
            images: vec![held_image()],
            limits: JobLimits {
                max_memory_mib: vm_mib,
                ..limits()
            },
            machine: Some(Machine {
                logical_cpus: 8,
                total_memory_mib: 20_000,
                available_memory_mib: 18_000,
                commit: None,
            }),
            ram_fresh: true,
            cache_groups: None,
            ram_unsupported_images: BTreeSet::new(),
        }
    }

    fn ram_registration(sha: &str, branch: &str, names: &[&str]) -> Registration {
        use gb_runner::ci::pipeline::{CacheDef, RamWorkspace};
        let mut registration = reg(sha, branch, names);
        let PipelineState::Jobs { jobs } = &mut registration.pipeline else {
            unreachable!();
        };
        let ram = jobs.iter_mut().find(|job| job.name == "rust-test").unwrap();
        ram.tmpfs_mib = Some(2048);
        ram.ram_workspace = Some(RamWorkspace {
            paths: vec!["target".into()],
            max_mib: 4608,
        });
        ram.cache = Some(CacheDef {
            paths: vec!["target".into()],
            key_files: vec![],
        });
        registration
    }

    fn published_cache_groups(registration: &Registration, repo_path: &str) -> BTreeSet<String> {
        let PipelineState::Jobs { jobs } = &registration.pipeline else {
            unreachable!();
        };
        jobs.iter()
            .filter_map(|job| {
                let cache = job.cache.as_ref()?;
                Some(CacheOwner::for_job(repo_path, job, cache).fingerprint())
            })
            .collect()
    }

    #[test]
    fn ram_memory_admission_uses_the_runner_startup_thresholds() {
        use gb_runner::ci::api::CommitMemory;

        let (_dir, store) = store();
        store
            .write_sync(|w| {
                register(
                    w,
                    ram_registration("head", "feature", &["rust-test"]),
                    10,
                    T0,
                )?;
                let attempt = &scheduled(w, false)?[0].0;
                for (vm, available, commit, allowed) in [
                    (7679, 18_000, None, false),
                    (7680, 18_000, None, true),
                    (8192, 18_000, None, true),
                    (7680, 9679, None, false),
                    (7680, 9680, Some(9679), false),
                    (7680, 9680, Some(9680), true),
                ] {
                    let mut runner = runner("runner", vm);
                    let machine = runner.machine.as_mut().unwrap();
                    machine.available_memory_mib = available;
                    machine.commit = commit.map(|available_mib| CommitMemory {
                        available_mib,
                        started_attempts: vec![],
                    });
                    let machine = runner.machine.as_ref().unwrap();
                    let startup = attempt.job.ram_workspace.as_ref().unwrap().memory_allows(
                        attempt.job.tmpfs_mib,
                        vm,
                        runner.limits,
                        machine,
                    );
                    let budget = runner.budget(&[]).unwrap();
                    assert_eq!(startup, allowed, "{vm} {available} {commit:?}");
                    assert_eq!(
                        runner.ram_allows(
                            attempt,
                            runner.ram_memory(&budget, Resources::default()).as_ref()
                        ),
                        startup
                    );
                }
                let mut runner = runner("runner", 7680);
                runner.limits.max_jobs = 2;
                let mut active = attempt.clone();
                active.runner = Some("runner".into());
                active.allocation = Some(Allocation {
                    cpus: 1,
                    vm_memory_mib: 7680,
                    reserved_memory_mib: 11_000,
                });
                let budget = runner.budget(&[active]).unwrap();
                assert!(!runner.ram_allows(
                    attempt,
                    runner.ram_memory(&budget, Resources::default()).as_ref()
                ));
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn concurrent_claims_pair_the_ram_job_and_keep_both_slots_working() {
        for names in [["rust-test", "rust-clippy"], ["rust-clippy", "rust-test"]] {
            let (_dir, store) = store();
            store
                .write_sync(|w| {
                    register(w, ram_registration("head", "feature", &names), 10, T0)?;
                    Ok(())
                })
                .unwrap();
            let gate = std::sync::Barrier::new(2);
            let claim = |name, vm, other, other_vm| {
                gate.wait();
                store
                    .write_sync(|w| {
                        super::claim_images(w, runner(name, vm), 60, T0, || {
                            Ok(vec![runner(other, other_vm)])
                        })
                    })
                    .unwrap()
                    .0
                    .unwrap()
                    .attempt
            };
            std::thread::scope(|scope| {
                let hub = scope.spawn(|| claim("hub", 6144, "linux", 8192));
                let linux = scope.spawn(|| claim("linux", 8192, "hub", 6144));
                let (hub, linux) = (hub.join().unwrap(), linux.join().unwrap());
                assert_eq!(
                    (hub.job.name.as_str(), linux.job.name.as_str()),
                    ("rust-clippy", "rust-test")
                );
                assert_ne!(hub.id, linux.id);
            });
        }
    }

    #[test]
    fn ram_preference_falls_back_when_the_peer_cannot_start_it() {
        for case in [
            "busy",
            "stale",
            "unknown",
            "cache-miss",
            "cache-other-repo",
            "guest-unsupported",
            "image",
            "cpu",
            "retry-selected",
            "retry-excluded",
        ] {
            let (_dir, store) = store();
            store
                .write_sync(|w| {
                    let registration =
                        ram_registration("head", "feature", &["rust-test", "rust-clippy"]);
                    let mut peer = runner("linux", 8192);
                    match case {
                        "busy" => {
                            register(w, reg("busy", "elsewhere", &["busy"]), 10, T0)?;
                            super::claim_images(w, runner("linux", 8192), 60, T0, || Ok(vec![]))?
                                .0
                                .unwrap();
                        }
                        "stale" => peer.ram_fresh = false,
                        "unknown" => peer.machine = None,
                        "cache-miss" => peer.cache_groups = Some(BTreeSet::new()),
                        "cache-other-repo" => {
                            peer.cache_groups =
                                Some(published_cache_groups(&registration, "acme/other"));
                        }
                        "guest-unsupported" => {
                            peer.ram_unsupported_images.insert(held_image().digest);
                        }
                        "image" => peer.images.clear(),
                        "cpu" => {
                            peer.limits.cpu_multiplier_percent = 100;
                            peer.machine.as_mut().unwrap().logical_cpus = peer.limits.max_cpus - 1;
                        }
                        _ => {}
                    }
                    register(w, registration, 10, T0)?;
                    if case.starts_with("retry") {
                        let mut a = scheduled(w, false)?.remove(0).0;
                        let selection = if case == "retry-selected" {
                            RunnerSelection {
                                runner: Some("hub".into()),
                                exclude_runners: vec![],
                            }
                        } else {
                            RunnerSelection {
                                runner: None,
                                exclude_runners: vec!["linux".into()],
                            }
                        };
                        a.retry_decision = Some(RetryDecision::of(&a, None, selection));
                        w.put(store::CI_ATTEMPTS, &Attempt::key(&a.id), &a)?;
                    }
                    let (claim, _) =
                        super::claim_images(w, runner("hub", 6144), 60, T0, || Ok(vec![peer]))?;
                    assert_eq!(claim.unwrap().attempt.job.name, "rust-test", "{case}");
                    Ok(())
                })
                .unwrap();
        }
    }

    #[test]
    fn ram_preference_preserves_priority_between_runs() {
        for earlier_branch in ["earlier", "main"] {
            let (_dir, store) = store();
            store
                .write_sync(|w| {
                    require_test(w)?;
                    register(w, reg("earlier", earlier_branch, &["other"]), 10, T0)?;
                    register(
                        w,
                        ram_registration("head", "feature", &["rust-clippy", "rust-test"]),
                        10,
                        T0,
                    )?;
                    let (claim, _) = super::claim_images(w, runner("linux", 8192), 60, T0, || {
                        Ok(vec![runner("hub", 6144)])
                    })?;
                    assert_eq!(claim.unwrap().attempt.sha, "earlier", "{earlier_branch}");
                    Ok(())
                })
                .unwrap();
        }
    }

    #[test]
    fn ram_preference_improves_adoption_without_delaying_the_check_group() {
        // A two-slot workload with a warm cache and a supported guest. Keep
        // the command durations and both poll orders fixed across policies.
        for (branch, first_poller, names) in [
            ("feature", "hub", ["rust-test", "rust-clippy"]),
            ("main", "linux", ["rust-clippy", "rust-test"]),
        ] {
            let run = |prefer_ram, cache_reported: bool| {
                let (_dir, store) = store();
                store
                    .write_sync(|w| {
                        let registration = ram_registration("head", branch, &names);
                        let cache_groups = cache_reported.then(|| {
                            published_cache_groups(&registration, &registration.repo_path)
                        });
                        register(w, registration, 10, T0)?;
                        let runners = [("hub", 6144), ("linux", 8192)].map(|(name, vm)| {
                            let mut report = runner(name, vm);
                            report.ram_fresh = prefer_ram;
                            report.cache_groups = cache_groups.clone();
                            report
                        });
                        let first = usize::from(first_poller == "linux");
                        let mut executed = Vec::new();
                        for index in [first, 1 - first] {
                            let report = &runners[index];
                            let (claim, _) =
                                super::claim_images(w, report.clone(), 300, T0, || {
                                    Ok(vec![runners[1 - index].clone()])
                                })?;
                            let claim = claim.unwrap();
                            let a = &claim.attempt;
                            let ram_applied = a.job.ram_workspace.as_ref().is_some_and(|ram| {
                                ram.memory_allows(
                                    a.job.tmpfs_mib,
                                    a.allocation.unwrap().vm_memory_mib,
                                    report.limits,
                                    report.machine.as_ref().unwrap(),
                                )
                            });
                            let duration = if a.job.name == "rust-test" { 90 } else { 30 };
                            executed.push((ram_applied, duration, claim));
                        }
                        executed.sort_by_key(|(_, duration, _)| *duration);
                        let mut adopted = 0;
                        let mut max_wait = 0;
                        let mut elapsed = 0;
                        for (ram_applied, duration, claim) in executed {
                            let end = later(T0, duration);
                            let a = finish(
                                w,
                                &claim.attempt.id,
                                &claim.lease,
                                "success",
                                None,
                                ResultInfo::default(),
                                &end,
                            )?;
                            assert_eq!(a.started_at.as_deref(), Some(T0));
                            let time = |s: &str| chrono::DateTime::parse_from_rfc3339(s).unwrap();
                            let started = time(a.started_at.as_deref().unwrap());
                            max_wait = max_wait.max((started - time(&a.created_at)).num_seconds());
                            elapsed = elapsed.max(
                                (started - time(T0)).num_seconds() as u64
                                    + duration_secs(&a).unwrap(),
                            );
                            adopted += usize::from(ram_applied);
                        }
                        Ok((adopted, max_wait, elapsed))
                    })
                    .unwrap()
            };
            for cache_reported in [false, true] {
                let before = run(false, cache_reported);
                let after = run(true, cache_reported);
                println!(
                    "{branch}, cache reported {cache_reported}: (RAM jobs, max job wait s, check group duration s) {before:?} -> {after:?}"
                );
                assert_eq!(before, (0, 0, 90));
                assert_eq!(after, (1, 0, 90));
            }
        }
    }

    #[test]
    fn windows_commit_admission_reserves_the_full_vm_until_it_starts() {
        use gb_runner::ci::api::CommitMemory;

        let (_d, store) = store();
        store
            .write_sync(|w| {
                require_test(w)?;
                let limits = JobLimits {
                    max_jobs: 2,
                    max_memory_mib: 8192,
                    memory_reserve_percent: 0,
                    ..limits()
                };
                let image = [held_image()];
                let claim = |now: &str, available_mib, started_attempts| {
                    claim_images(
                        w,
                        "windows",
                        60,
                        now,
                        &image,
                        limits,
                        Some(Machine {
                            logical_cpus: 16,
                            total_memory_mib: 32768,
                            available_memory_mib: 24576,
                            commit: Some(CommitMemory {
                                available_mib,
                                started_attempts,
                            }),
                        }),
                    )
                };
                register(w, reg("sha", "main", &["first", "second"]), 10, T0)?;
                let (unclaimed, wait) = claim(T0, 8191, vec![])?;
                assert!(unclaimed.is_none());
                let wait = wait.unwrap();
                assert_eq!(
                    (
                        wait.cpu_shortfall,
                        wait.memory_shortfall_mib,
                        wait.commit_shortfall_mib
                    ),
                    (0, 0, 1)
                );

                let first = claim(T0, 12288, vec![])?.0.unwrap().attempt;
                let (unclaimed, wait) = claim(T0, 12288, vec![])?;
                assert!(unclaimed.is_none());
                assert_eq!(wait.unwrap().commit_shortfall_mib, 4096);

                let same = claim(&later(T0, 2), 12288, vec![])?.1.unwrap();
                let unchanged = get_attempt(w, &same.attempt_id)?.unwrap();
                assert_eq!(unchanged.runner_deferrals["windows"].at, T0);
                assert_eq!(same.commit_shortfall_mib, 4096);

                let updated_at = later(T0, 4);
                let changed = claim(&updated_at, 13312, vec![])?.1.unwrap();
                assert_eq!(changed.commit_shortfall_mib, 3072);
                let second = claim(&later(T0, 6), 8192, vec![first.id])?
                    .0
                    .unwrap()
                    .attempt;
                assert_ne!(second.job.name, first.job.name);
                let deferral = &second.runner_deferrals["windows"];
                assert_eq!(deferral.at, updated_at);
                assert_eq!(deferral.wait.commit_shortfall_mib, 3072);
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn protected_runner_startup_failure_retries_elsewhere_before_falling_back() {
        let (_d, store) = store();
        store
            .write_sync(|w| {
                require_test(w)?;
                register(w, reg("sha", "main", &["test"]), 10, T0)?;
                let runner = |name: &str, machine| ClaimRunner {
                    name: name.into(),
                    images: vec![held_image()],
                    limits: limits(),
                    machine,
                    ram_fresh: false,
                    cache_groups: None,
                    ram_unsupported_images: BTreeSet::new(),
                };
                let empty = || Ok(Vec::new());
                let first = super::claim_images(w, runner("windows", None), 60, T0, empty)?
                    .0
                    .unwrap();
                finish(
                    w,
                    &first.attempt.id,
                    &first.lease,
                    "startup_failure",
                    None,
                    ResultInfo::default(),
                    T0,
                )?;
                let until = later(T0, RUNNER_RETRY_GRACE_SECS);
                assert!(
                    super::claim_images(w, runner("windows", None), 60, T0, empty)?
                        .0
                        .is_none()
                );
                assert!(
                    super::claim_images(w, runner("windows", None), 60, &until, || Ok(vec![
                        runner("linux", None)
                    ]))?
                    .0
                    .is_none()
                );

                let second = super::claim_images(w, runner("linux", None), 60, T0, empty)?
                    .0
                    .unwrap();
                assert_eq!(second.attempt.auto_retry.as_ref().unwrap().number, 1);
                let ended = finish(
                    w,
                    &second.attempt.id,
                    &second.lease,
                    "success",
                    None,
                    ResultInfo::default(),
                    T0,
                )?;
                let history = attempts_of(w, "repo_1", "sha")?;
                let recovery = ended.runner_recovery(&history).unwrap();
                assert_eq!(recovery.failed_attempt, first.attempt.id);
                assert_eq!(recovery.failed_runner, "windows");
                assert_eq!(recovery.successful_runner, "linux");

                register(w, reg("solo", "main", &["test"]), 10, T0)?;
                let first = super::claim_images(w, runner("windows", None), 60, T0, empty)?
                    .0
                    .unwrap();
                finish(
                    w,
                    &first.attempt.id,
                    &first.lease,
                    "timed_out",
                    None,
                    ResultInfo::default(),
                    T0,
                )?;
                assert!(
                    super::claim_images(w, runner("windows", None), 60, T0, empty)?
                        .0
                        .is_none()
                );
                let fallback = super::claim_images(w, runner("windows", None), 60, &until, empty)?
                    .0
                    .unwrap();
                assert_eq!(fallback.attempt.auto_retry.as_ref().unwrap().number, 1);
                assert_eq!(fallback.attempt.runner.as_deref(), Some("windows"));
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn resource_claims_route_to_a_runner_that_fits_and_reserve_the_head() {
        let (_d, s) = store();
        s.write_sync(|w| {
            let image = held_image();
            let limits = JobLimits {
                memory_reserve_percent: 0,
                cpu_multiplier_percent: 100,
                max_jobs: 2,
                ..limits()
            };
            let take = |runner, machine| {
                claim_images(
                    w,
                    runner,
                    60,
                    T0,
                    std::slice::from_ref(&image),
                    limits,
                    machine,
                )
            };
            for (name, peak) in [("heavy", 6000), ("light", 3000)] {
                register(w, reg(name, "sample", &[name]), 10, T0)?;
                let (claimed, _) = take("sampling", None)?;
                let claim = claimed.unwrap();
                finish(
                    w,
                    &claim.attempt.id,
                    &claim.lease,
                    "success",
                    None,
                    ResultInfo {
                        peak_memory_mib: Some(peak),
                        ..Default::default()
                    },
                    T1,
                )?;
            }
            register(
                w,
                reg("queued", "feature", &["heavy", "light", "cold"]),
                10,
                T0,
            )?;
            let machine = |total, available| {
                Some(Machine {
                    logical_cpus: 8,
                    total_memory_mib: total,
                    available_memory_mib: available,
                    commit: None,
                })
            };
            let (blocked, reason) = take("busy-small", machine(8500, 3500))?;
            assert!(
                blocked.is_none(),
                "light would take the head's reserved memory"
            );
            let reason = reason.unwrap();
            assert_eq!(
                (
                    reason.job.as_str(),
                    reason.cpu_shortfall,
                    reason.memory_shortfall_mib
                ),
                ("heavy", 0, 2500)
            );

            let (temporarily_blocked, _) = take("busy-large", machine(9000, 3500))?;
            assert!(
                temporarily_blocked.is_none(),
                "starting light would consume the head's OS memory reserve"
            );

            let (light, _) = take("small-machine", machine(5500, 3500))?;
            let light = light.unwrap().attempt;
            assert_eq!(light.job.name, "light");
            assert_eq!(light.allocation.unwrap().reserved_memory_mib, 3000);
            let (heavy, _) = take("idle", machine(8500, 8500))?;
            assert_eq!(heavy.unwrap().attempt.job.name, "heavy");
            let (cold, _) = take("unknown", None)?;
            assert_eq!(
                cold.unwrap()
                    .attempt
                    .allocation
                    .unwrap()
                    .reserved_memory_mib,
                limits.max_memory_mib
            );
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn resource_claims_count_held_allocations_and_keep_the_default_cpu_capacity() {
        let (_d, s) = store();
        s.write_sync(|w| {
            register(w, reg("queued", "feature", &["a", "b", "c"]), 10, T0)?;
            let image = [held_image()];
            let machine = Some(Machine {
                logical_cpus: 4,
                total_memory_mib: 20000,
                available_memory_mib: 20000,
                commit: None,
            });
            let limits = JobLimits {
                max_jobs: 3,
                ..limits()
            };
            let first = claim_images(w, "r", 60, T0, &image, limits, machine.clone())?
                .0
                .unwrap();
            assert_eq!(first.attempt.job.name, "a");
            let explicit = JobLimits {
                cpu_multiplier_percent: 100,
                max_cpus: 1,
                ..limits
            };
            let (blocked, reason) =
                claim_images(w, "r", 60, T0, &image, explicit, machine.clone())?;
            assert!(blocked.is_none());
            assert_eq!(reason.unwrap().cpu_shortfall, 1);
            for name in ["b", "c"] {
                let (cl, _) = claim_images(w, "r", 60, T0, &image, limits, machine.clone())?;
                assert_eq!(cl.unwrap().attempt.job.name, name);
            }
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn resource_claims_reuse_without_a_vm_and_return_the_position_if_the_image_changes() {
        let (_d, s) = store();
        s.write_sync(|w| {
            let mut image = held_image();
            register(w, reg("source", "feature", &["test"]), 10, T0)?;
            let original =
                claim_images(w, "r", 60, T0, std::slice::from_ref(&image), limits(), None)?
                    .0
                    .unwrap();
            finish(
                w,
                &original.attempt.id,
                &original.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(image.digest.clone()),
                    ..Default::default()
                },
                T0,
            )?;
            let mut next = reg("same-tree", "another", &["test"]);
            next.tree = Some("tree-source".into());
            register(w, next, 10, T0)?;
            let machine = Some(Machine {
                logical_cpus: 4,
                total_memory_mib: 8192,
                available_memory_mib: 0,
                commit: None,
            });
            let borrowed = claim_images(
                w,
                "r",
                60,
                T0,
                std::slice::from_ref(&image),
                limits(),
                machine.clone(),
            )?
            .0
            .unwrap();
            assert!(borrowed.reuse.is_some());
            assert!(borrowed.attempt.allocation.is_none());
            let pending = finish(
                w,
                &borrowed.attempt.id,
                &borrowed.lease,
                "deferred",
                None,
                ResultInfo::default(),
                T0,
            )?;
            assert_eq!(pending.status, "queued");
            assert_eq!(pending.seq, borrowed.attempt.seq);
            image.digest = format!("sha256:{}", "3".repeat(64));
            assert!(
                claim_images(w, "r", 60, T0, &[image], limits(), machine)?
                    .0
                    .is_none()
            );
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn inventory_filters_claims_and_missing_images_expire() {
        let (_d, s) = store();
        s.write_sync(|w| {
            register(w, reg("s1", "feature", &["test"]), 10, T0)?;
            let queued = scheduled(w, false)?[0].0.clone();
            let mut image = super::super::images::Image {
                reference: "wrong/image".into(),
                architecture: "arm64".into(),
                digest: "sha256:test".into(),
            };
            let take = |images: &[_]| {
                claim_images(w, "r", 60, T0, images, limits(), None).map(|(claimed, _)| claimed)
            };
            assert!(take(&[])?.is_none());
            assert!(take(std::slice::from_ref(&image))?.is_none());
            image.reference = queued.job.image.clone();
            image.architecture = "ppc64".into();
            assert!(take(std::slice::from_ref(&image))?.is_none());
            image.architecture = "amd64".into();
            assert!(take(&[image])?.is_some());
            register(w, reg("s2", "feature", &["test"]), 10, T0)?;
            let changed = fail_unavailable(w, T1, T2, |a| {
                Some(format!("Missing image {} for CPU amd64", a.job.image))
            })?;
            assert!(!changed.is_empty());
            let failed = attempts_of(w, "repo_1", "s2")?;
            assert!(
                failed
                    .iter()
                    .any(|a| a.conclusion.as_deref() == Some("startup_failure")
                        && a.details.as_deref().unwrap().contains("amd64"))
            );
            Ok(())
        })
        .unwrap();
    }

    fn require_test(w: &Wtx) -> AppResult<()> {
        let repo = crate::model::Repo {
            id: "repo_1".into(),
            path: "acme/demo".into(),
            workspace: "ws".into(),
            default_branch: "main".into(),
            protected_branches: vec![],
            post_merge_jobs: Vec::new(),
            required_approvals: 0,
            auto_land: false,
            hold_while_failing: false,
            ci_network: false,
            responder: None,
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
                        T0,
                    )?;
                    let current = latest_attempts(w, "repo_1", "sha")?.remove(0);
                    if n <= MAX_AUTO_RETRIES {
                        assert_eq!(
                            current.status, "queued",
                            "failure must never be the current check"
                        );
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
    fn automatic_retry_targets_all_protected_checks_and_only_retryable_endings() {
        for case in [
            "pr",
            "optional",
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
                    T0,
                )?;
                assert_eq!(
                    attempts_of(w, "repo_1", "sha")?
                        .iter()
                        .filter(|a| a.auto_retry.is_some())
                        .count(),
                    usize::from(matches!(case, "optional" | "startup")),
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
                    } else {
                        assert_eq!(current.id, ended.id, "{cause}");
                        assert!(details.contains("retry the job"), "{cause}: {details}");
                        assert!(!details.contains("queued as"), "{cause}: {details}");
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
                T2,
            )?;
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
            verified_sha: false,
            paths: Vec::new(),
            name: name.into(),
            image: "alpine".into(),
            commands: vec!["true".into()],
            timeout_secs: Some(60),
            tmpfs_mib: None,
            ram_workspace: None,
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
            post_merge_jobs: Vec::new(),
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
    fn post_merge_reselection_preserves_running_and_manual_work_and_replaces_queued() {
        for mode in ["queued", "running", "manual"] {
            let (_d, s) = store();
            s.write_sync(|w| register(w, selected("s1", &[]), 10, T0))
                .unwrap();
            if mode == "running" {
                s.write_sync(|w| claim(w, "runner", 60, T0)).unwrap();
            }
            if mode == "manual" {
                let a = latest(&s, "s1")
                    .into_iter()
                    .find(|a| a.job.name == "web")
                    .unwrap();
                s.write_sync(|w| cancel(w, &a.id, "member1", T0)).unwrap();
                s.write_sync(|w| retry(w, &a.id, 10, T0)).unwrap();
            }
            let mut reg = selected("s1", &["web"]);
            reg.selection.as_mut().unwrap().post_merge_jobs = vec!["web".into()];
            s.write_sync(|w| register(w, reg, 10, T1)).unwrap();
            let a = latest(&s, "s1")
                .into_iter()
                .find(|a| a.job.name == "web")
                .unwrap();
            if mode == "queued" {
                assert_eq!(a.conclusion.as_deref(), Some("skipped"));
                assert_eq!(a.path_skip.as_ref().unwrap().reason, SkipReason::PostMerge);
                let history = s.read_sync(|r| attempts_of(r, "repo_1", "s1")).unwrap();
                assert!(
                    !history
                        .iter()
                        .any(|a| a.conclusion.as_deref() == Some("cancelled"))
                );
                s.write_sync(|w| register(w, selected("s1", &[]), 10, T2))
                    .unwrap();
                let a = latest(&s, "s1")
                    .into_iter()
                    .find(|a| a.job.name == "web")
                    .unwrap();
                assert_eq!(a.status, "queued");
                assert!(a.path_skip.is_none());
            } else {
                assert!(a.path_skip.is_none());
                assert!(!a.cancel_requested);
                assert_eq!(
                    a.status,
                    if mode == "running" {
                        "in_progress"
                    } else {
                        "queued"
                    }
                );
            }
            assert!(
                s.read_sync(crate::model::index_problems)
                    .unwrap()
                    .is_empty()
            );
        }
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
            let mut reg = selected("s1", &["web", "rust"]);
            reg.selection.as_mut().unwrap().post_merge_jobs = vec!["web".into(), "rust".into()];
            s.write_sync(|w| register(w, reg, 1, T0)).unwrap();
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
                now,
            )
        })
        .unwrap()
    }

    const CACHE_KEY: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

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
    fn claims_carry_the_runs_input_digest_of_their_job() {
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
                let done = finish(w, &c.attempt.id, &c.lease, conclusion, None, info, T0)?;
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
                    finish(w, &c.attempt.id, &c.lease, "success", None, info, T1)?;
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
                peak_memory_mib: None,
            };
            let mut invalid = info.clone();
            invalid.image_digest = None;
            assert!(finish(w, &c.attempt.id, &c.lease, "success", None, invalid, T2).is_err());
            assert!(
                finish(
                    w,
                    &c.attempt.id,
                    &c.lease,
                    "failure",
                    None,
                    info.clone(),
                    T2
                )
                .is_err()
            );
            let done = finish(w, &c.attempt.id, &c.lease, "success", None, info, T2)?;
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
                    peak_memory_mib: None,
                },
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
                peak_memory_mib: None,
            };
            assert!(finish(w, &c.attempt.id, &c.lease, "success", None, info, T1).is_err());
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

    #[test]
    fn a_fast_forward_lands_the_cached_head_without_queuing_another_attempt() {
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
        s.read_sync(|r| {
            use gb_runner::ci::api::CacheDecision;
            let attempts = attempts_of(r, "repo_1", "s1")?;
            assert_eq!(attempts.len(), 2);
            let build = attempts.iter().find(|a| a.job.name == "build").unwrap();
            assert!(matches!(
                cache_decision(r, "r", &build.id)?,
                CacheDecision::Publish(_)
            ));
            assert!(matches!(
                cache_decision(r, "other", &build.id)?,
                CacheDecision::Discard
            ));
            Ok(())
        })
        .unwrap();
        assert!(s.write_sync(|w| claim(w, "r", 60, T2)).unwrap().is_none());
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
    #[test]
    fn verified_inputs_require_equal_sha_and_tree_even_with_paths() {
        for declared in [false, true] {
            for same_tree in [false, true] {
                for same_verified in [false, true] {
                    let (_d, s) = store();
                    s.write_sync(|w| {
                        let mut reg1 = reg("s1", "feature", &["test"]);
                        if let PipelineState::Jobs { jobs } = &mut reg1.pipeline {
                            jobs[0].verified_sha = declared;
                            jobs[0].paths = vec!["src/**".into()];
                        }
                        reg1.inputs.insert("test".into(), "inputs".into());
                        register(w, reg1, 10, T0)?;
                        let mut source = claim(w, "r", 60, T0)?.unwrap();
                        source.attempt.verified_sha = Some("v1".into());
                        source.attempt.status = "completed".into();
                        source.attempt.conclusion = Some("success".into());
                        source.attempt.image_digest = Some(DIGEST.into());
                        let mut reg2 = reg("s2", "feature", &["test"]);
                        reg2.pipeline = get_run(w, "repo_1", "s1")?.unwrap().pipeline;
                        reg2.inputs.insert("test".into(), "inputs".into());
                        reg2.tree = Some("tree-s1".into());
                        if !same_tree {
                            reg2.tree = Some("different".into());
                        }
                        register(w, reg2, 10, T0)?;
                        let mut target = claim(w, "r", 60, T0)?.unwrap().attempt;
                        target.verified_sha = Some(if same_verified { "v1" } else { "v2" }.into());
                        assert_eq!(
                            can_reuse(w, &target, &source.attempt)?,
                            !declared || (same_tree && same_verified)
                        );
                        Ok(())
                    })
                    .unwrap();
                }
            }
        }
    }

    #[test]
    fn verification_promotes_only_the_failed_jobs_retry() {
        let (_d, s) = store();
        s.write_sync(|w| {
            require_test(w)?;
            let mut v = super::super::verify::Verification {
                sha: "sha".into(),
                base: None,
                state: "running".into(),
                started_at: T0.into(),
                finished_at: None,
                failed_jobs: vec![],
                failing_since: None,
                last_verified: Some(super::super::verify::Verified {
                    sha: "verified".into(),
                    at: T0.into(),
                }),
            };
            w.put(store::CI_VERIFICATIONS, &Key::new().s("repo_1").bytes(), &v)?;
            let mut registration = reg("sha", "main", &["lint"]);
            if let PipelineState::Jobs { jobs } = &mut registration.pipeline {
                jobs[0].verified_sha = true;
            }
            register(w, registration, 0, T0)?;
            assert_eq!(
                admission_count(w, super::super::priority::Class::Normal, 10, None)?,
                0
            );
            assert_eq!(
                admission_count(w, super::super::priority::Class::Deferred, 10, None)?,
                0
            );
            v.last_verified = Some(super::super::verify::Verified {
                sha: "at-claim".into(),
                at: T0.into(),
            });
            w.put(store::CI_VERIFICATIONS, &Key::new().s("repo_1").bytes(), &v)?;
            let c = claim(w, "r", 60, T0)?.unwrap();
            assert_eq!(c.attempt.verified_sha.as_deref(), Some("at-claim"));
            assert_eq!(
                super::super::priority::class_of(w, &c.attempt)?.class,
                super::super::priority::Class::Verify
            );
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "failure",
                None,
                ResultInfo::default(),
                T1,
            )?;
            let next = latest_attempts(w, "repo_1", "sha")?.remove(0);
            assert_eq!(next.status, "queued");
            assert_eq!(next.auto_retry.as_ref().unwrap().number, 1);
            assert_eq!(
                super::super::priority::class_of(w, &next)?.class,
                super::super::priority::Class::Protected
            );
            assert_eq!(
                admission_count(w, super::super::priority::Class::Normal, 10, None)?,
                1
            );
            Ok(())
        })
        .unwrap();
    }
    #[test]
    fn only_failed_current_main_verification_reopens_with_retry_over_capacity() {
        for state in ["failed", "passed"] {
            for current in [false, true] {
                let (_d, s) = store();
                s.write_sync(|w| {
                    require_test(w)?;
                    register(w, reg("sha", "main", &["lint"]), 1, T0)?;
                    let mut last = None;
                    for _ in 0..=MAX_AUTO_RETRIES {
                        let c = claim(w, "r", 60, T0)?.unwrap();
                        finish(
                            w,
                            &c.attempt.id,
                            &c.lease,
                            "failure",
                            None,
                            ResultInfo::default(),
                            T1,
                        )?;
                        last = Some(c.attempt.id);
                    }
                    let v = super::super::verify::Verification {
                        sha: "sha".into(),
                        base: None,
                        state: state.into(),
                        started_at: T0.into(),
                        finished_at: Some(T1.into()),
                        failed_jobs: vec![],
                        failing_since: Some("first".into()),
                        last_verified: None,
                    };
                    w.put(store::CI_VERIFICATIONS, &Key::new().s("repo_1").bytes(), &v)?;
                    w.put(
                        store::REFS,
                        &Key::new().s("repo_1").s("refs/heads/main").bytes(),
                        &if current { "sha" } else { "new" },
                    )?;
                    let id = last.unwrap();
                    if state == "failed" && current {
                        let a = retry(w, &id, 0, T2)?;
                        assert_eq!(a.status, "queued");
                        let v = super::super::verify::get(w, "repo_1")?.unwrap();
                        assert_eq!(v.state, "running");
                        assert_eq!(v.failing_since.as_deref(), Some("first"));
                    } else {
                        assert!(retry(w, &id, 0, T2).is_err());
                        retry(w, &id, 1, T2)?;
                        assert_eq!(
                            super::super::verify::get(w, "repo_1")?.unwrap().state,
                            state
                        );
                    }
                    Ok(())
                })
                .unwrap();
            }
        }
    }
    #[test]
    fn probes_are_isolated_reusable_sources_and_survive_interruption_without_runs() {
        let (_d, s) = store();
        s.write_sync(|w| {
            require_test(w)?;
            crate::model::rebuild_indexes(w)?;
            let repo: crate::model::Repo = w
                .get(store::REPOS, &crate::model::Repo::key("repo_1"))?
                .unwrap();
            register(w, reg("base", "main", &["test"]), 10, T0)?;
            let c = claim(w, "r", 60, T0)?.unwrap();
            finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo::default(),
                T0,
            )?;
            let before = current_attempts(w, "repo_1", "base", &[job("test")])?;
            let a = probe(w, &repo, "base", &job("test"), 10, T0)?.unwrap();
            assert_eq!(
                super::super::priority::class_of(w, &a)?.class,
                super::super::priority::Class::Landing
            );
            let claimed = claim(w, "r", 60, T0)?.unwrap();
            assert_eq!(claimed.attempt.cache_scope, "probe");
            assert!(claimed.reuse.is_none());
            finish(
                w,
                &a.id,
                &claimed.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    ..Default::default()
                },
                T0,
            )?;
            assert_eq!(
                before,
                current_attempts(w, "repo_1", "base", &[job("test")])?
            );
            assert!(retry(w, &a.id, 10, T0).is_err());
            let mut same = reg("same", "feature", &["test"]);
            same.tree = Some("tree-base".into());
            register(w, same, 10, T0)?;
            let reused = claim(w, "r", 60, T0)?.unwrap();
            assert_eq!(reused.reuse.unwrap().attempt_id, a.id);
            cancel(w, &reused.attempt.id, "test", T0)?;
            let standalone = probe(w, &repo, "middle", &job("test"), 10, T0)?.unwrap();
            assert!(get_run(w, "repo_1", "middle")?.is_none());
            let c = claim(w, "r", 60, T0)?.unwrap();
            assert_eq!(c.attempt.id, standalone.id);
            interrupt_running(w, None, "service stop", T0)?;
            let again = claim(w, "r", 60, T0)?.unwrap();
            assert_eq!(again.attempt.purpose, Purpose::Probe);
            assert_eq!(again.attempt.seq, c.attempt.seq);
            assert_eq!(again.attempt.cache_scope, "probe");
            finish(
                w,
                &again.attempt.id,
                &again.lease,
                "failure",
                None,
                ResultInfo::default(),
                T0,
            )?;
            assert!(claim(w, "r", 60, T0)?.is_none());
            assert!(crate::model::index_problems(w)?.is_empty());
            Ok(())
        })
        .unwrap();
    }
    #[test]
    fn probe_inputs_supply_paths_reuse_but_intermediate_probes_have_no_reuse_index() {
        let (_d, s) = store();
        s.write_sync(|w| {
            require_test(w)?;
            let repo: crate::model::Repo = w
                .get(store::REPOS, &crate::model::Repo::key("repo_1"))?
                .unwrap();
            let mut r = with_inputs("base");
            r.protected = true;
            if let PipelineState::Jobs { jobs } = &mut r.pipeline {
                jobs[0].paths = vec!["src/**".into()];
            }
            let PipelineState::Jobs { jobs } = r.pipeline.clone() else {
                unreachable!()
            };
            register(w, r, 10, T0)?;
            let original = claim(w, "r", 60, T0)?.unwrap();
            finish(
                w,
                &original.attempt.id,
                &original.lease,
                "success",
                None,
                ResultInfo::default(),
                T0,
            )?;
            probe(w, &repo, "base", &jobs[0], 10, T0)?.unwrap();
            let c = claim(w, "r", 60, T0)?.unwrap();
            assert_eq!(c.attempt.inputs.as_deref(), Some("d1"));
            assert_eq!(c.attempt.cache_scope, "probe");
            let source = finish(
                w,
                &c.attempt.id,
                &c.lease,
                "success",
                None,
                ResultInfo {
                    image_digest: Some(DIGEST.into()),
                    ..Default::default()
                },
                T0,
            )?;
            let indexed = by_inputs(w)?;
            assert!(indexed.contains(&source.id));
            let mut target = with_inputs("different-tree");
            target.pipeline = PipelineState::Jobs { jobs: jobs.clone() };
            target.sha = "different-tree".into();
            target.tree = Some("different-tree".into());
            target.protected = false;
            target.branch = "feature".into();
            register(w, target, 10, T0)?;
            let c = claim(w, "r", 60, T0)?.unwrap();
            assert_eq!(c.reuse.unwrap().attempt_id, source.id);
            probe(w, &repo, "middle", &jobs[0], 10, T0)?.unwrap();
            let c = claim(w, "r", 60, T0)?.unwrap();
            assert!(c.attempt.inputs.is_none());
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
                T0,
            )?;
            assert_eq!(by_inputs(w)?, indexed);
            assert!(get_run(w, "repo_1", "middle")?.is_none());
            Ok(())
        })
        .unwrap();
    }
}

#[cfg(test)]
#[path = "priority_tests.rs"]
mod priority_tests;

#[cfg(test)]
#[path = "reuse_tests.rs"]
mod reuse_tests;

/// Admit one diagnostic attempt without registering or changing a CI run.
pub(super) fn probe(
    w: &Wtx,
    repo: &crate::model::Repo,
    sha: &str,
    job: &JobDef,
    max: usize,
    now: &str,
) -> AppResult<Option<Attempt>> {
    if admission_count(w, super::priority::Class::Landing, max, None)? >= max {
        return Ok(None);
    }
    let mut a = new_job(
        w,
        (&repo.id, &repo.path, sha, "probe"),
        job,
        repo.ci_network,
        now,
    )?;
    a.purpose = Purpose::Probe;
    a.allow_reuse = false;
    put_attempt(w, None, &a)?;
    Ok(Some(a))
}
