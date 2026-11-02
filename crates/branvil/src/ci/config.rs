//! The `[ci]` section of `branvil.toml`: service-side limits, the supervised
//! runner, and the runner's executor settings (read by `branvil runner` when it
//! runs with the same configuration).

use std::path::PathBuf;

use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Clone, Copy, Debug, Default, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RunnerMode {
    /// No runner is started by the service (run `branvil runner` yourself).
    #[default]
    Off,
    /// `branvil serve` starts one `branvil runner` child and restarts it when
    /// it exits.
    Auto,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields, default)]
pub struct CiConfig {
    pub runner: RunnerMode,
    /// Queued attempts at most; runs that do not fit wait (visibly) for room.
    pub queue_max: usize,
    /// Lease length of a claimed attempt; the runner renews it by heartbeat.
    pub lease_secs: u64,
    /// Queued attempts conclude `startup_failure` when no runner has
    /// contacted the service for this long.
    pub runner_absent_secs: u64,
    /// How long a stopping service (SIGTERM) waits for the running jobs to
    /// finish; the ones still running then are queued again in their place.
    pub stop_wait_secs: u64,
    /// Log size kept per attempt, in MiB.
    pub log_max_mib: u64,
    /// Size of one artifact, in MiB.
    pub artifact_max_mib: u64,
    /// Logs and artifacts of attempts finished longer ago are deleted.
    pub retention_days: u64,
    pub executor: ExecutorConfig,
    pub microsandbox: MicrosandboxConfig,
}

impl Default for CiConfig {
    fn default() -> Self {
        Self {
            runner: RunnerMode::Off,
            queue_max: 256,
            lease_secs: 60,
            runner_absent_secs: 120,
            stop_wait_secs: 3600,
            log_max_mib: 16,
            artifact_max_mib: 64,
            retention_days: 30,
            executor: ExecutorConfig::default(),
            microsandbox: MicrosandboxConfig::default(),
        }
    }
}

/// What the runner gives every job.
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields, default)]
pub struct ExecutorConfig {
    /// Per-job work directories and the dependency cache [default: ~/.branvil/runner].
    pub work_dir: Option<PathBuf>,
    /// The CPUs and memory of every job's microVM.
    pub max_cpus: u32,
    pub max_memory_mib: u64,
    /// Jobs run at once, each in its own microVM.
    pub max_jobs: u32,
    /// The time limit of a job that gives none, and the cap of a pipeline's
    /// `timeout_secs`.
    pub max_timeout_secs: u64,
    /// Dependency cache size; superseded generations are removed before current
    /// entries, least recently used first within each group. Allow at least one
    /// generation of all cached jobs; extra capacity retains older generations.
    pub cache_max_mib: u64,
    /// Total size of one attempt's artifacts before zipping.
    pub artifacts_max_mib: u64,
}

impl Default for ExecutorConfig {
    fn default() -> Self {
        Self {
            work_dir: None,
            max_cpus: 4,
            max_memory_mib: 4096,
            max_jobs: 1,
            max_timeout_secs: 3600,
            cache_max_mib: 2048,
            artifacts_max_mib: 64,
        }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
#[serde(deny_unknown_fields, default)]
pub struct MicrosandboxConfig {
    /// An `msb` binary to use (it must report the pinned version).
    pub msb_path: Option<PathBuf>,
    /// Branvil's own `MSB_HOME` [default: ~/.branvil/msb-home].
    pub home: Option<PathBuf>,
}
