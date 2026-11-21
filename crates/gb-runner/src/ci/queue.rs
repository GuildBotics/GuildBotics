use serde::{Deserialize, Serialize};
/// A successful execution the runner may reuse after checking its image.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Reuse {
    pub attempt_id: String,
    pub sha: String,
    pub image_digest: String,
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

/// The execution metadata reported by the runner at finish.
#[derive(Serialize, Deserialize, Clone, Debug, Default)]
pub struct ResultInfo {
    pub image_digest: Option<String>,
    pub peak_memory_mib: Option<u64>,
    pub reused_from: Option<String>,
    pub cache_key: Option<String>,
}
