//! The pipeline definition, `.guildbotics-ci.toml`, read from the commit that runs
//! it (see `docs/ci.md`). The schema is small and validated strictly: anything
//! unknown or out of range makes the whole pipeline invalid, which shows as a
//! failing `pipeline` check, never as success.

use serde::Deserialize;
use serde::de::IgnoredAny;

use std::collections::BTreeMap;
use std::path::Path;

use super::queue::PipelineState;
use crate::error::{AppError, AppResult};
use crate::git::Git;
use crate::util::valid_name;

/// Where the pipeline lives in a commit.
pub const PIPELINE_PATH: &str = ".guildbotics-ci.toml";
/// Largest pipeline file read.
pub const MAX_PIPELINE_BYTES: usize = 64 << 10;

const MAX_JOBS: usize = 32;
const MAX_COMMANDS: usize = 64;
const MAX_COMMAND_BYTES: usize = 8 << 10;
const MAX_SCRIPT_BYTES: usize = 64 << 10;
const MAX_ARTIFACTS: usize = 16;
const MAX_PATHS: usize = 32;
const MAX_CACHE_PATHS: usize = 8;
const MAX_TMPFS_MIB: u32 = 4096;

/// Reads the pipeline of commit `sha` in the repository at `dir`. Only a
/// pipeline path the commit's tree does not have reads as `Absent`; any Git
/// failure (a missing commit, an unreadable tree or blob) is an error, never a
/// pipeline (the service's registration is retried by the reconciler).
pub async fn read(git: &Git, dir: &Path, sha: &str) -> AppResult<PipelineState> {
    Ok(match read_file(git, dir, sha).await? {
        File::Absent => PipelineState::Absent,
        File::Invalid(message) => PipelineState::Invalid { message },
        File::Parsed(file) => PipelineState::Jobs { jobs: file.jobs },
    })
}

/// The release rule of commit `sha`'s pipeline. A commit without a pipeline
/// has the default rule; `Err` is the message of an invalid pipeline.
pub async fn read_release(
    git: &Git,
    dir: &Path,
    sha: &str,
) -> AppResult<Result<ReleaseRule, String>> {
    Ok(match read_file(git, dir, sha).await? {
        File::Absent => Ok(ReleaseRule::default()),
        File::Invalid(message) => Err(message),
        File::Parsed(file) => Ok(file.release),
    })
}

/// When and under which tag names the default branch is released.
#[derive(Clone, Debug, PartialEq)]
pub struct ReleaseRule {
    /// What the name of every release tag starts with (after `refs/tags/`).
    pub tag_prefix: String,
    /// The automatic release times; empty releases only on request.
    pub schedule: Vec<ReleaseTime>,
    pub time_zone: chrono_tz::Tz,
}

impl Default for ReleaseRule {
    fn default() -> Self {
        Self {
            tag_prefix: DEFAULT_TAG_PREFIX.into(),
            schedule: Vec::new(),
            time_zone: chrono_tz::UTC,
        }
    }
}

/// The tag prefix of a pipeline without one.
pub const DEFAULT_TAG_PREFIX: &str = "release/";
const MAX_SCHEDULE: usize = 24;

/// One automatic release time, in the rule's time zone.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReleaseTime {
    /// `HH:MM`, every day.
    Daily { hour: u32, minute: u32 },
    /// `*:MM`, every hour.
    Hourly { minute: u32 },
}

impl ReleaseTime {
    fn parse(text: &str) -> Option<Self> {
        let (hour, minute) = text.split_once(':')?;
        let number = |s: &str, max: u32| {
            (s.len() == 2 && s.bytes().all(|b| b.is_ascii_digit()))
                .then(|| s.parse::<u32>().ok())
                .flatten()
                .filter(|n| *n <= max)
        };
        let minute = number(minute, 59)?;
        if hour == "*" {
            return Some(Self::Hourly { minute });
        }
        Some(Self::Daily {
            hour: number(hour, 23)?,
            minute,
        })
    }
}

impl std::fmt::Display for ReleaseTime {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Daily { hour, minute } => write!(f, "{hour:02}:{minute:02}"),
            Self::Hourly { minute } => write!(f, "*:{minute:02}"),
        }
    }
}

/// A tag prefix: tag name characters, ending with `/`, `-` or `.` so that
/// the name after it stays apart.
pub fn valid_tag_prefix(prefix: &str) -> bool {
    (1..=32).contains(&prefix.len()) && prefix.ends_with(['/', '-', '.']) && valid_tag_text(prefix)
}

/// Characters a release tag's name may use, without empty or dot-led
/// components, `..`, or a `.lock` component.
pub fn valid_tag_text(name: &str) -> bool {
    name.bytes()
        .all(|b| b.is_ascii_alphanumeric() || b"-_./".contains(&b))
        && !name.contains("..")
        && name.split('/').enumerate().all(|(i, part)| {
            (!part.is_empty() || i == name.matches('/').count())
                && !part.starts_with(['.', '-'])
                && !part.ends_with(".lock")
        })
}

/// What the pipeline of commit `sha` requires of the pull requests into its
/// branch, under the same rules as `read`.
pub async fn read_required(git: &Git, dir: &Path, sha: &str) -> AppResult<RequiredJobs> {
    Ok(match read_file(git, dir, sha).await? {
        File::Absent => RequiredJobs::Absent,
        File::Invalid(_) => RequiredJobs::Invalid,
        File::Parsed(file) => RequiredJobs::Jobs(file.required),
    })
}

/// Whether each job of commit `sha`'s pipeline runs only after merge, by job
/// name: empty without a pipeline, `None` when it is invalid.
pub async fn read_post_merge(
    git: &Git,
    dir: &Path,
    sha: &str,
) -> AppResult<Option<BTreeMap<String, bool>>> {
    Ok(match read_file(git, dir, sha).await? {
        File::Absent => Some(BTreeMap::new()),
        File::Invalid(_) => None,
        File::Parsed(file) => Some(file.post_merge),
    })
}

/// The jobs a commit's pipeline marks `required`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RequiredJobs {
    /// The commit has no pipeline, so it requires nothing.
    Absent,
    Jobs(Vec<String>),
    /// What it requires cannot be known, so nothing may land on it.
    Invalid,
}

enum File {
    Absent,
    Invalid(String),
    Parsed(ParsedFile),
}

async fn read_file(git: &Git, dir: &Path, sha: &str) -> AppResult<File> {
    let failed = |what: &str, stderr: &str| {
        AppError::internal(format!(
            "{what} {PIPELINE_PATH} of {sha}: {}",
            stderr.trim()
        ))
    };
    let out = git
        .run(Some(dir), &["ls-tree", "-z", sha, "--", PIPELINE_PATH])
        .await?;
    if !out.ok() {
        return Err(failed("listing", &out.stderr));
    }
    // `<mode> <type> <object>\t<path>`, or nothing when the path is not there.
    let entry = out.text();
    let mut fields = entry.trim_end_matches('\0').split(['\t', ' ']);
    let (kind, object) = match (fields.next(), fields.next(), fields.next()) {
        (None | Some(""), ..) => return Ok(File::Absent),
        (Some(_), Some(kind), Some(object)) => (kind, object.to_string()),
        _ => return Err(failed("listing", &format!("unexpected entry {entry:?}"))),
    };
    if kind != "blob" {
        return Ok(File::Invalid(format!("{PIPELINE_PATH} is not a file.")));
    }
    let out = git.run(Some(dir), &["cat-file", "blob", &object]).await?;
    if !out.ok() {
        return Err(failed("reading", &out.stderr));
    }
    if out.stdout.len() > MAX_PIPELINE_BYTES {
        return Ok(File::Invalid(format!(
            "{PIPELINE_PATH} is larger than {MAX_PIPELINE_BYTES} bytes."
        )));
    }
    Ok(match String::from_utf8(out.stdout) {
        Ok(text) => parse_file(&text).map_or_else(File::Invalid, File::Parsed),
        Err(_) => File::Invalid(format!("{PIPELINE_PATH} is not UTF-8.")),
    })
}

pub use gb_runner::ci::pipeline::{
    ArtifactDef, CacheDef, JobDef, check_relative, valid_digest, valid_image,
};
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FileSchema {
    #[serde(default = "one")]
    version: u32,
    #[serde(default)]
    jobs: Vec<JobSchema>,
    release: Option<ReleaseSchema>,
}

fn one() -> u32 {
    1
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ReleaseSchema {
    tag_prefix: Option<String>,
    #[serde(default)]
    schedule: Vec<String>,
    time_zone: Option<String>,
}

fn validate_release(r: ReleaseSchema) -> Result<ReleaseRule, String> {
    let tag_prefix = r.tag_prefix.unwrap_or_else(|| DEFAULT_TAG_PREFIX.into());
    if !valid_tag_prefix(&tag_prefix) {
        return Err(format!(
            "[release] tag_prefix '{tag_prefix}' is not a tag name prefix ([A-Za-z0-9_./-], at most 32, ending with '/', '-' or '.')."
        ));
    }
    if r.schedule.len() > MAX_SCHEDULE {
        return Err(format!(
            "[release] schedule takes at most {MAX_SCHEDULE} times."
        ));
    }
    let mut schedule = Vec::new();
    for text in &r.schedule {
        let time = ReleaseTime::parse(text).ok_or_else(|| {
            format!(
                "[release] schedule: '{text}' is not a time (HH:MM every day, *:MM every hour)."
            )
        })?;
        if !schedule.contains(&time) {
            schedule.push(time);
        }
    }
    let time_zone = match r.time_zone {
        None => chrono_tz::UTC,
        Some(name) => name
            .parse()
            .map_err(|_| format!("[release] time_zone '{name}' is not an IANA time zone name."))?,
    };
    Ok(ReleaseRule {
        tag_prefix,
        schedule,
        time_zone,
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct JobSchema {
    #[serde(default)]
    verified_sha: bool,
    /// What the branch demands of a PR, not how the job runs: it stays out of
    /// `JobDef`, so stored runs, reuse and the runner's messages do not change.
    #[serde(default)]
    required: bool,
    /// Like `required`, kept out of `JobDef`: the base decides it for the jobs
    /// it has (`ci::selection`).
    #[serde(default)]
    post_merge: bool,
    name: String,
    image: String,
    commands: Vec<String>,
    #[serde(default)]
    paths: Vec<String>,
    timeout_secs: Option<u64>,
    tmpfs_mib: Option<u32>,
    ram_workspace: Option<gb_runner::ci::pipeline::RamWorkspace>,
    #[serde(default)]
    artifacts: Vec<ArtifactDef>,
    cache: Option<CacheDef>,
    /// Not pipeline settings (the runner's): read only to say so.
    cpus: Option<IgnoredAny>,
    memory_mib: Option<IgnoredAny>,
}

#[cfg(test)]
pub fn parse(text: &str) -> Result<Vec<JobDef>, String> {
    Ok(parse_file(text)?.jobs)
}

struct ParsedFile {
    jobs: Vec<JobDef>,
    required: Vec<String>,
    post_merge: BTreeMap<String, bool>,
    release: ReleaseRule,
}

/// Parses and validates a pipeline. `Err` is the message shown on the
/// failing `pipeline` check.
fn parse_file(text: &str) -> Result<ParsedFile, String> {
    let file: FileSchema =
        toml::from_str(text).map_err(|e| format!("{PIPELINE_PATH} is not valid: {e}"))?;
    if file.version != 1 {
        return Err(format!(
            "{PIPELINE_PATH}: version {} is not supported (this server reads version 1).",
            file.version
        ));
    }
    if file.jobs.is_empty() {
        return Err(format!("{PIPELINE_PATH} defines no [[jobs]]."));
    }
    if file.jobs.len() > MAX_JOBS {
        return Err(format!(
            "{PIPELINE_PATH} defines more than {MAX_JOBS} jobs."
        ));
    }
    let (mut jobs, mut required, mut post_merge) = (Vec::new(), Vec::new(), BTreeMap::new());
    let release = file
        .release
        .map(validate_release)
        .transpose()?
        .unwrap_or_default();
    for j in file.jobs {
        let (is_required, is_post_merge) = (j.required, j.post_merge);
        let job = validate_job(j)?;
        if jobs
            .iter()
            .any(|x: &JobDef| crate::util::fold(&x.name) == crate::util::fold(&job.name))
        {
            return Err(format!("Two jobs are named {}.", job.name));
        }
        if is_required {
            required.push(job.name.clone());
        }
        post_merge.insert(job.name.clone(), is_post_merge);
        jobs.push(job);
    }
    Ok(ParsedFile {
        jobs,
        required,
        post_merge,
        release,
    })
}

fn validate_job(j: JobSchema) -> Result<JobDef, String> {
    let name = j.name;
    let at = |what: &str| format!("Job {name}: {what}");
    if !valid_name(&name) || name == "pipeline" {
        return Err(format!(
            "'{name}' is not a valid job name ([A-Za-z0-9_.-], at most 64, not 'pipeline')."
        ));
    }
    if j.required && j.post_merge {
        return Err(at(
            "required and post_merge cannot both be true: a post-merge job does not run on the pull requests that must pass it.",
        ));
    }
    if !valid_image(&j.image) {
        return Err(at(&format!(
            "'{}' is not an image reference ([registry/]name[:tag], without a digest: a job runs the image the runner's cache holds under the tag).",
            j.image
        )));
    }
    if j.commands.is_empty() || j.commands.len() > MAX_COMMANDS {
        return Err(at(&format!(
            "commands must have 1 to {MAX_COMMANDS} entries."
        )));
    }
    let mut total = 0;
    for c in &j.commands {
        if c.trim().is_empty() || c.len() > MAX_COMMAND_BYTES || c.contains('\0') {
            return Err(at(&format!(
                "every command must be non-empty, without NUL, at most {MAX_COMMAND_BYTES} bytes."
            )));
        }
        total += c.len();
    }
    if total > MAX_SCRIPT_BYTES {
        return Err(at(&format!(
            "the commands are longer than {MAX_SCRIPT_BYTES} bytes together."
        )));
    }
    if j.timeout_secs.is_some_and(|t| !(1..=86_400).contains(&t)) {
        return Err(at("timeout_secs must be between 1 and 86400."));
    }
    if j.tmpfs_mib
        .is_some_and(|mib| !(1..=MAX_TMPFS_MIB).contains(&mib))
    {
        return Err(at(&format!(
            "tmpfs_mib must be between 1 and {MAX_TMPFS_MIB}."
        )));
    }
    if let Some(ram) = &j.ram_workspace {
        ram.validate().map_err(|m| at(&m))?;
    }
    if j.paths.len() > MAX_PATHS {
        return Err(at(&format!("paths takes at most {MAX_PATHS} entries.")));
    }
    for path in &j.paths {
        check_relative(path).map_err(|m| at(&m))?;
    }
    if j.cpus.is_some() || j.memory_mib.is_some() {
        return Err(at(
            "cpus and memory_mib are not pipeline settings: every job runs with the CPUs and memory set for the runner (Settings → CI).",
        ));
    }
    if j.artifacts.len() > MAX_ARTIFACTS {
        return Err(at(&format!("at most {MAX_ARTIFACTS} artifacts.")));
    }
    let mut names: Vec<String> = Vec::new();
    for a in &j.artifacts {
        if !valid_name(&a.name) {
            return Err(at(&format!("'{}' is not a valid artifact name.", a.name)));
        }
        if names.contains(&crate::util::fold(&a.name)) {
            return Err(at(&format!("two artifacts are named {}.", a.name)));
        }
        names.push(crate::util::fold(&a.name));
        if a.paths.is_empty() || a.paths.len() > MAX_PATHS {
            return Err(at(&format!(
                "artifact {} needs 1 to {MAX_PATHS} paths.",
                a.name
            )));
        }
        for p in &a.paths {
            check_relative(p).map_err(|m| at(&m))?;
        }
    }
    if let Some(c) = &j.cache {
        if c.paths.is_empty() || c.paths.len() > MAX_CACHE_PATHS {
            return Err(at(&format!("cache needs 1 to {MAX_CACHE_PATHS} paths.")));
        }
        if c.key_files.len() > MAX_CACHE_PATHS {
            return Err(at(&format!(
                "cache takes at most {MAX_CACHE_PATHS} key_files."
            )));
        }
        for p in c.paths.iter().chain(&c.key_files) {
            check_relative(p).map_err(|m| at(&m))?;
        }
    }
    Ok(JobDef {
        verified_sha: j.verified_sha,
        name,
        image: j.image,
        commands: j.commands,
        paths: j.paths,
        timeout_secs: j.timeout_secs,
        tmpfs_mib: j.tmpfs_mib,
        ram_workspace: j.ram_workspace,
        artifacts: j.artifacts,
        cache: j.cache,
    })
}

/// A path inside the work directory: relative, `/`-separated, no `.`/`..`
/// components, no empty components except a trailing `/`.
#[cfg(test)]
mod tests {
    use super::*;

    const DIGEST: &str = "sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8";

    #[test]
    fn paths_are_optional_bounded_relative_git_globs_without_magic() {
        let job = "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n";
        assert!(parse(job).unwrap()[0].paths.is_empty());
        let paths = [
            "src/**",
            "tests/*.rs",
            "Cargo.lock",
            "rust-toolchain*",
            "-file",
            "a b/**",
        ];
        let jobs = parse(&format!("{job}paths={paths:?}")).unwrap();
        assert_eq!(jobs[0].paths, paths);
        for path in [
            "",
            "/root",
            "../x",
            "a/../x",
            "a//b",
            "a/./b",
            "a\\b",
            ":(glob)src/**",
            "a:b",
            "\0",
        ] {
            let escaped = serde_json::to_string(path).unwrap();
            assert!(
                parse(&format!("{job}paths=[{escaped}]")).is_err(),
                "{path:?}"
            );
        }
        assert!(parse(&format!("{job}paths={:?}", vec!["x"; MAX_PATHS])).is_ok());
        assert!(parse(&format!("{job}paths={:?}", vec!["x"; MAX_PATHS + 1])).is_err());
    }

    #[test]
    fn a_minimal_pipeline_leaves_the_limits_to_the_runner() {
        let jobs = parse(
            "[[jobs]]\nname = \"test\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"true\"]\n",
        )
        .unwrap();
        assert_eq!(jobs.len(), 1);
        assert_eq!(jobs[0].timeout_secs, None);
        assert!(jobs[0].artifacts.is_empty());
        let jobs = parse(
            "[[jobs]]\nname = \"test\"\nimage = \"alpine\"\ncommands = [\"true\"]\ntimeout_secs = 86400\n",
        )
        .unwrap();
        assert_eq!(jobs[0].timeout_secs, Some(86_400));
    }

    #[test]
    fn tmpfs_size_is_optional_and_bounded_per_job() {
        let pipeline = "[[jobs]]\nname='disk'\nimage='alpine'\ncommands=['true']\n\
                        [[jobs]]\nname='ram'\nimage='alpine'\ncommands=['true']\n";
        assert!(
            parse(pipeline)
                .unwrap()
                .iter()
                .all(|job| job.tmpfs_mib.is_none())
        );
        for size in [1, 2048, MAX_TMPFS_MIB] {
            let jobs = parse(&format!("{pipeline}tmpfs_mib = {size}")).unwrap();
            assert_eq!(jobs[0].tmpfs_mib, None);
            assert_eq!(jobs[1].tmpfs_mib, Some(size));
        }
        for size in ["0", "4097"] {
            assert_eq!(
                parse(&format!("{pipeline}tmpfs_mib = {size}")).unwrap_err(),
                "Job ram: tmpfs_mib must be between 1 and 4096."
            );
        }
        for value in ["-1", "1.5", "\"2048\"", "4294967296"] {
            let error = parse(&format!("{pipeline}tmpfs_mib = {value}")).unwrap_err();
            assert!(error.contains("tmpfs_mib"), "{value}: {error}");
        }
    }

    #[test]
    fn ram_workspace_paths_are_distinct_local_directories() {
        let pipeline = "[[jobs]]\nname='demo'\nimage='alpine'\ncommands=['true']\n";
        assert!(parse(pipeline).unwrap()[0].ram_workspace.is_none());
        let job = parse(&format!("{pipeline}tmpfs_mib = 2048\nram_workspace = {{ paths = ['out/cache/', 'out/logs'], max_mib = 4608 }}")).unwrap().remove(0);
        assert_eq!(job.tmpfs_mib, Some(2048));
        assert_eq!(job.ram_workspace.unwrap().paths, ["out/cache/", "out/logs"]);
        for (paths, size) in [
            ("[]", 1),
            ("['/work/out']", 1),
            ("['../out']", 1),
            ("['out/../build']", 1),
            ("['out//build']", 1),
            ("['.git/objects']", 1),
            ("['out', 'out/']", 1),
            ("['a/b', 'a']", 1),
            ("['a', 'a/b']", 1),
            ("['out']", 0),
            ("['out']", 65_537),
        ] {
            assert!(
                parse(&format!(
                    "{pipeline}ram_workspace = {{ paths = {paths}, max_mib = {size} }}"
                ))
                .is_err(),
                "{paths} {size}"
            );
        }
        assert!(
            parse(&format!(
                "{pipeline}ram_workspace = {{ paths = ['out'], max_mib = 65536 }}"
            ))
            .is_ok()
        );
    }

    #[test]
    fn everything_unknown_or_out_of_range_is_refused() {
        let job = |extra: &str| {
            format!("[[jobs]]\nname = \"t\"\nimage = \"alpine\"\ncommands = [\"true\"]\n{extra}")
        };
        assert!(parse("").is_err(), "no jobs");
        assert!(parse("version = 2\n[[jobs]]\nname=\"t\"\nimage=\"a\"\ncommands=[\"x\"]").is_err());
        assert!(parse(&job("privileged = true\n")).is_err(), "unknown key");
        assert!(parse(&job("timeout_secs = 0\n")).is_err());
        assert!(parse(&job("timeout_secs = 86401\n")).is_err());
        assert!(
            parse(&job("artifacts = [{name = \"r\", paths = [\"../etc\"]}]\n")).is_err(),
            "artifact path escapes"
        );
        assert!(parse(&job("artifacts = [{name = \"r\", paths = [\"/etc\"]}]\n")).is_err());
        assert!(parse(&job("artifacts = [{name = \"a/b\", paths = [\"x\"]}]\n")).is_err());
        assert!(parse(&job("cache = {paths = []}\n")).is_err());
        assert!(
            parse("[[jobs]]\nname=\"t\"\nimage=\"-x\"\ncommands=[\"x\"]").is_err(),
            "image looking like an option"
        );
        assert!(parse("[[jobs]]\nname=\"t\"\nimage=\"a\"\ncommands=[]").is_err());
        assert!(
            parse(
                "[[jobs]]\nname=\"t\"\nimage=\"a\"\ncommands=[\"x\"]\n[[jobs]]\nname=\"T\"\nimage=\"a\"\ncommands=[\"x\"]"
            )
            .is_err(),
            "duplicate names"
        );
        assert!(parse("[[jobs]]\nname=\"pipeline\"\nimage=\"a\"\ncommands=[\"x\"]").is_err());
        assert!(
            parse(&job(
                "artifacts = [{name = \"r\", paths = [\"out/\"]}]\ncache = {paths = [\"target\"], key_files = [\"Cargo.lock\"]}\n"
            ))
            .is_ok()
        );
    }

    #[test]
    fn the_runner_s_settings_are_refused_with_the_reason() {
        for extra in [
            "cpus = 1\n",
            "memory_mib = 1024\n",
            "cpus = 2\nmemory_mib = 512\n",
        ] {
            let err = parse(&format!(
                "[[jobs]]\nname = \"t\"\nimage = \"alpine\"\ncommands = [\"true\"]\n{extra}"
            ))
            .unwrap_err();
            assert_eq!(
                err,
                "Job t: cpus and memory_mib are not pipeline settings: every job runs with the CPUs and memory set for the runner (Settings → CI).",
                "{extra}"
            );
        }
    }

    #[test]
    fn an_image_is_named_by_its_tag_only() {
        let err = parse(&format!(
            "[[jobs]]\nname = \"t\"\nimage = \"docker.io/library/alpine@{DIGEST}\"\ncommands = [\"true\"]\n"
        ))
        .unwrap_err();
        assert!(err.contains("without a digest"), "{err}");
        assert!(valid_image("alpine"));
        assert!(valid_image("docker.io/library/alpine:3.22"));
        assert!(valid_image("registry:5000/a/b:1"));
        assert!(!valid_image(&format!("alpine@{DIGEST}")));
        assert!(!valid_image(&format!("alpine:3@{DIGEST}")));
        assert!(!valid_image("alpine latest"));
        assert!(!valid_image("--privileged"));
        assert!(!valid_image(""));
        assert!(valid_digest(DIGEST));
        assert!(!valid_digest("sha256:abc"));
        assert!(!valid_digest(&DIGEST.replace("sha256", "sha512")));
    }
    #[test]
    fn required_is_an_optional_boolean_that_leaves_the_job_definition_alone() {
        let job = "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n";
        assert!(parse_file(job).unwrap().required.is_empty());
        for (value, required) in [("true", vec!["test"]), ("false", vec![])] {
            let file = parse_file(&format!("{job}required={value}\n")).unwrap();
            assert_eq!(file.required, required, "{value}");
            assert_eq!(file.jobs, parse(job).unwrap(), "{value}");
        }
        for value in ["'yes'", "1"] {
            let error = parse(&format!("{job}required={value}\n")).unwrap_err();
            assert!(error.contains("required"), "{value}: {error}");
        }
    }
    #[test]
    fn post_merge_is_an_optional_boolean_that_cannot_be_required() {
        let job = "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n";
        assert_eq!(
            parse_file(job).unwrap().post_merge,
            [("test".to_string(), false)].into()
        );
        for (value, post_merge) in [("true", true), ("false", false)] {
            let file = parse_file(&format!("{job}post_merge={value}\n")).unwrap();
            assert_eq!(file.jobs, parse(job).unwrap(), "{value}");
            assert!(file.required.is_empty(), "{value}");
            assert_eq!(
                file.post_merge,
                [("test".to_string(), post_merge)].into(),
                "{value}"
            );
        }
        for value in ["'yes'", "1"] {
            let error = parse(&format!("{job}post_merge={value}\n")).unwrap_err();
            assert!(error.contains("post_merge"), "{value}: {error}");
        }
        let error = parse(&format!("{job}required=true\npost_merge=true\n")).unwrap_err();
        assert!(
            error.starts_with("Job test: required and post_merge"),
            "{error}"
        );
        let file = parse_file(&format!("{job}required=true\npost_merge=false\n")).unwrap();
        assert_eq!(file.required, ["test"]);
    }

    #[test]
    fn the_release_section_has_defaults_and_validates_every_value() {
        let job = "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n";
        assert_eq!(parse_file(job).unwrap().release, ReleaseRule::default());
        let file = parse_file(&format!(
            "[release]\ntag_prefix='v-'\nschedule=['12:00','*:05','12:00','00:00','23:59']\ntime_zone='Asia/Tokyo'\n{job}"
        ))
        .unwrap();
        assert_eq!(file.release.tag_prefix, "v-");
        assert_eq!(file.release.time_zone, chrono_tz::Asia::Tokyo);
        assert_eq!(
            file.release.schedule,
            [
                ReleaseTime::Daily {
                    hour: 12,
                    minute: 0
                },
                ReleaseTime::Hourly { minute: 5 },
                ReleaseTime::Daily { hour: 0, minute: 0 },
                ReleaseTime::Daily {
                    hour: 23,
                    minute: 59
                },
            ]
        );
        let rule = parse_file(&format!("[release]\n{job}")).unwrap().release;
        assert_eq!(rule, ReleaseRule::default());
        for (section, why) in [
            ("tag_prefix='release'", "tag_prefix"),
            ("tag_prefix='rel..ease/'", "tag_prefix"),
            ("tag_prefix='/x/'", "tag_prefix"),
            ("tag_prefix='a b/'", "tag_prefix"),
            ("tag_prefix='x.lock/'", "tag_prefix"),
            ("tag_prefix=''", "tag_prefix"),
            ("schedule=['24:00']", "'24:00'"),
            ("schedule=['12:60']", "'12:60'"),
            ("schedule=['9:00']", "'9:00'"),
            ("schedule=['*:5']", "'*:5'"),
            ("schedule=['noon']", "'noon'"),
            ("time_zone='Mars/Base'", "time_zone"),
            ("every='day'", "every"),
        ] {
            let error = parse(&format!("[release]\n{section}\n{job}")).unwrap_err();
            assert!(error.contains(why), "{section}: {error}");
        }
        let many = vec!["*:00"; MAX_SCHEDULE + 1];
        assert!(parse(&format!("[release]\nschedule={many:?}\n{job}")).is_err());
    }

    #[test]
    fn release_tag_names_keep_to_safe_tag_characters() {
        for prefix in ["release/", "v-", "r.", "team/release/"] {
            assert!(valid_tag_prefix(prefix), "{prefix}");
        }
        for prefix in ["release", "-x/", "a//", ".x/", "a/.b/", "x.lock/", "a~/"] {
            assert!(!valid_tag_prefix(prefix), "{prefix}");
        }
        assert!(!valid_tag_prefix(&format!("{}/", "a".repeat(32))));
        for name in ["release/2026-11-26-1", "release/x_y.z"] {
            assert!(valid_tag_text(name), "{name}");
        }
        for name in [
            "release/a b",
            "release/a..b",
            "release/.x",
            "release/-x",
            "release/a:b",
        ] {
            assert!(!valid_tag_text(name), "{name}");
        }
    }

    #[test]
    fn verified_sha_is_an_explicit_optional_pipeline_input() {
        let job = "[[jobs]]\nname='test'\nimage='alpine'\ncommands=['true']\n";
        assert!(!parse(job).unwrap()[0].verified_sha);
        assert!(parse(&format!("{job}verified_sha=true\n")).unwrap()[0].verified_sha);
        assert!(parse(&format!("{job}verified_sha='yes'\n")).is_err());
    }
}
