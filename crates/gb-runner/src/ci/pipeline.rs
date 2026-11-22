use serde::{Deserialize, Serialize};
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
    pub artifacts: Vec<ArtifactDef>,
    pub cache: Option<CacheDef>,
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
