pub const LEASE_HEADER: &str = "x-branvil-lease";

use serde::{Deserialize, Serialize};

use super::{config::JobLimits, images::Image};

#[derive(Serialize, Deserialize)]
pub struct RunnerHealth {
    pub version: String,
    /// A Hub from a different build may only return the version.
    pub max_jobs: Option<u32>,
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
}

impl RunnerReport {
    pub fn can_run(&self) -> bool {
        self.version == gb_base::BUILD_VERSION && self.executor_state == ExecutorState::Ready
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
