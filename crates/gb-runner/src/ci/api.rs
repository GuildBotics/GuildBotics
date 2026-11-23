pub const LEASE_HEADER: &str = "x-branvil-lease";

use serde::{Deserialize, Serialize};

use super::{config::JobLimits, images::Image};

pub const CACHE_DECISION_BATCH: usize = 128;

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CacheDecision {
    Keep,
    Discard,
    Publish(chrono::DateTime<chrono::Utc>),
}

#[derive(Serialize, Deserialize)]
pub struct RunnerHealth {
    pub version: String,
    /// A Hub from a different build may only return the version.
    pub max_jobs: Option<u32>,
    pub cache_max_mib: Option<u64>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields)]
pub struct RunnerReport {
    pub version: String,
    pub max_jobs: u32,
    pub architecture: String,
    pub executor: String,
    pub executor_state: ExecutorState,
    pub executor_detail: Option<String>,
    pub images: Vec<Image>,
    pub accept: bool,
    pub running_jobs: usize,
    pub machine: Option<Machine>,
    /// Fingerprints of cache groups with a published generation. None means
    /// the inventory was unavailable, while an empty inventory means no cache.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_groups: Option<std::collections::BTreeSet<String>>,
    /// Image digests whose guest tool probe failed on this executor.
    #[serde(default, skip_serializing_if = "std::collections::BTreeSet::is_empty")]
    pub ram_unsupported_images: std::collections::BTreeSet<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Machine {
    pub logical_cpus: u32,
    pub total_memory_mib: u64,
    pub available_memory_mib: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub commit: Option<CommitMemory>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CommitMemory {
    pub available_mib: u64,
    /// Active allocations already reflected in this sample.
    pub started_attempts: Vec<String>,
}

impl RunnerReport {
    pub fn matches_hub_version(&self) -> bool {
        self.version == gb_base::BUILD_VERSION
    }

    pub fn can_run(&self) -> bool {
        self.matches_hub_version() && self.executor_state == ExecutorState::Ready
    }
}

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ExecutorState {
    Ready,
    Unavailable,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ExecutionLimits {
    pub limits: JobLimits,
    pub artifacts_max_mib: u64,
}
