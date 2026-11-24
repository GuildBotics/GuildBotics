use serde::{Deserialize, Serialize};

use super::{api::Machine, config::JobLimits};

pub const JOB_USER_ID: u32 = 1000;
const COMMAND_HEADROOM_MIB: u64 = 1024;
/// One job as recorded with its attempts (the snapshot taken when the run was
/// registered; retries reuse it).
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct JobDef {
    pub verified_sha: bool,
    pub name: String,
    pub image: String,
    pub commands: Vec<String>,
    /// Git pathspec globs selecting PR-head jobs; empty means every change.
    pub paths: Vec<String>,
    /// The job's own time limit, when it gives one; the runner lowers it to
    /// its limit and runs a job that gives none with its limit.
    pub timeout_secs: Option<u64>,
    /// The maximum size of the job's /tmp in MiB. None uses a disk.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    pub tmpfs_mib: Option<u32>,
    /// Selected work directories with a shared RAM limit after cache restore.
    #[serde(deserialize_with = "Deserialize::deserialize")]
    pub ram_workspace: Option<RamWorkspace>,
    pub artifacts: Vec<ArtifactDef>,
    pub cache: Option<CacheDef>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RamWorkspace {
    pub paths: Vec<String>,
    pub max_mib: u32,
}

impl RamWorkspace {
    /// Whether the configured VM and host can support a RAM workspace.
    pub fn memory_capacity_allows(
        &self,
        tmpfs_mib: Option<u32>,
        vm_mib: u64,
        limits: JobLimits,
        total_mib: u64,
    ) -> bool {
        let guest = u64::from(self.max_mib) + u64::from(tmpfs_mib.unwrap_or_default());
        guest + COMMAND_HEADROOM_MIB <= vm_mib
            && vm_mib.saturating_mul(u64::from(limits.max_jobs))
                <= total_mib.saturating_sub(limits.memory_reserve_mib(total_mib))
    }

    /// The memory gate shared by admission (reported capacity) and VM startup
    /// (live capacity). Cache restore and guest support are separate checks.
    pub fn memory_allows(
        &self,
        tmpfs_mib: Option<u32>,
        vm_mib: u64,
        limits: JobLimits,
        machine: &Machine,
    ) -> bool {
        let reserve = limits.memory_reserve_mib(machine.total_memory_mib);
        self.memory_capacity_allows(tmpfs_mib, vm_mib, limits, machine.total_memory_mib)
            && vm_mib <= machine.available_memory_mib.saturating_sub(reserve)
            && machine
                .commit
                .as_ref()
                .is_none_or(|commit| vm_mib <= commit.available_mib.saturating_sub(reserve))
    }

    pub fn validate(&self) -> Result<(), String> {
        if !(1..=65_536).contains(&self.max_mib) {
            return Err("ram_workspace.max_mib must be between 1 and 65536.".into());
        }
        if !(1..=8).contains(&self.paths.len()) {
            return Err("ram_workspace.paths needs 1 to 8 directories.".into());
        }
        let mut seen: Vec<&str> = Vec::new();
        for path in &self.paths {
            check_relative(path)?;
            let p = path.trim_end_matches('/');
            if p.split('/').any(|part| part == ".git") {
                return Err("ram_workspace.paths cannot include .git.".into());
            }
            if seen
                .iter()
                .any(|other| p == *other || below(p, other) || below(other, p))
            {
                return Err("ram_workspace.paths cannot overlap.".into());
            }
            seen.push(p);
        }
        Ok(())
    }
}

fn below(path: &str, parent: &str) -> bool {
    path.strip_prefix(parent)
        .is_some_and(|rest| rest.starts_with('/'))
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct ArtifactDef {
    pub name: String,
    /// Files or directories, relative to the work directory.
    pub paths: Vec<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CacheDef {
    /// Directories (relative to the work directory) restored before and saved
    /// after the job.
    pub paths: Vec<String>,
    /// Files whose content is part of the cache key (`Cargo.lock`, ...).
    #[serde(default)]
    pub key_files: Vec<String>,
}

/// Cache generations are interchangeable only within this group.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct CacheOwner {
    pub repo: String,
    pub job: String,
    pub user: u32,
    pub paths: Vec<String>,
}

impl CacheOwner {
    pub fn for_job(repo: &str, job: &JobDef, cache: &CacheDef) -> Self {
        Self {
            repo: repo.into(),
            job: job.name.clone(),
            user: JOB_USER_ID,
            paths: cache.paths.clone(),
        }
    }

    pub fn fingerprint(&self) -> String {
        let bytes = serde_json::to_vec(self).expect("cache owner only contains strings and a u32");
        crate::util::hex(&crate::util::sha256(&bytes))
    }
}

pub fn check_relative(path: &str) -> Result<(), String> {
    let trimmed = path.strip_suffix('/').unwrap_or(path);
    let ok = !trimmed.is_empty()
        && trimmed.len() <= 512
        && !trimmed.starts_with('/')
        && !trimmed.contains('\\')
        && !trimmed.contains('\0')
        && !trimmed.contains(':')
        && trimmed
            .split('/')
            .all(|c| !c.is_empty() && c != "." && c != "..");
    if ok {
        Ok(())
    } else {
        Err(format!(
            "'{path}' is not a relative path inside the work directory."
        ))
    }
}

/// An OCI reference by tag, `[registry/]name[:tag]` (no digest: which
/// version runs is the image the runner's cache holds under the tag),
/// conservative characters only, never starting with `-` (it is passed as an
/// argument).
pub fn valid_image(image: &str) -> bool {
    !image.is_empty()
        && image.len() <= 256
        && !image.starts_with('-')
        && image
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-/:".contains(&b))
}

/// An image's configuration digest as the runner reports it:
/// `sha256:<64 hex>`.
pub fn valid_digest(digest: &str) -> bool {
    digest
        .strip_prefix("sha256:")
        .is_some_and(|h| h.len() == 64 && h.bytes().all(|b| b.is_ascii_hexdigit()))
}
