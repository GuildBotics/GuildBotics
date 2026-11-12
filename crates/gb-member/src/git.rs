//! History crosses the host/guest boundary as bundles, never as a repository.
use crate::{MemberError, Result, WriteGuard};
use gb_ports::{CodeHostingService, GitCredential};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    future::Future,
    path::{Path, PathBuf},
    pin::Pin,
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

pub const MAX_BUNDLE: usize = 1 << 30;
const MAX_OUTPUT: usize = 1 << 24;
const PUBLIC: &str = "refs/guildbotics/published/";
pub type Pending<'a, T> = Pin<Box<dyn Future<Output = Result<T>> + Send + 'a>>;

#[derive(Default)]
pub struct Output {
    pub code: i32,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}
impl Output {
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.stdout).trim().to_owned()
    }
    fn checked(self) -> Result<Self> {
        self.checked_text(false)
    }
    fn checked_bundle(mut self) -> Result<Self> {
        if self.code != 0 {
            self.stdout.clear();
        }
        self.checked()
    }
    fn checked_text(self, combine: bool) -> Result<Self> {
        if self.code == 0 {
            return Ok(self);
        }
        // Drop oversized streams as a whole: never split a credential across
        // a cutoff before the response's redactor can recognize it.
        let text = |bytes: &[u8]| {
            if bytes.len() > 4096 {
                String::new()
            } else {
                String::from_utf8_lossy(bytes).into_owned()
            }
        };
        let reason = if combine {
            format!("{}\n{}", text(&self.stdout), text(&self.stderr))
        } else if self.stderr.is_empty() {
            text(&self.stdout)
        } else {
            text(&self.stderr)
        };
        Err(MemberError::CapabilityArgs {
            key: "capabilities.member_git.git_failed_reason",
            args: [("reason".into(), reason.trim().to_owned())].into(),
        })
    }
}
fn failure(key: &'static str) -> MemberError {
    MemberError::Capability(match key {
        "git_failed" => "capabilities.member_git.git_failed",
        "timeout" => "capabilities.member_git.operation_timeout",
        "redirect" => "capabilities.member_git.redirect",
        "unprepared" => "capabilities.member_git.checkout_required",
        "detached" => "capabilities.member_git.detached",
        "bundle" => "capabilities.member_git.invalid_bundle",
        "path" => "capabilities.member_git.invalid_path",
        "work" => "capabilities.member_git.work_required",
        _ => "capabilities.member_git.checkout_reassigned",
    })
}
fn io_error<E>(_: E) -> MemberError {
    failure("git_failed")
}
fn lock_error(error: std::io::Error) -> MemberError {
    if error.kind() == std::io::ErrorKind::TimedOut {
        MemberError::Capability("capabilities.member_git.lock_timeout")
    } else {
        io_error(error)
    }
}
fn branch_error(key: &'static str, branch: &str) -> MemberError {
    MemberError::CapabilityArgs {
        key,
        args: [("branch".into(), branch.into())].into(),
    }
}
fn remaining(deadline: Instant) -> Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .ok_or_else(|| failure("timeout"))
}
pub fn object(value: &str) -> Result<&str> {
    if matches!(value.len(), 40 | 64)
        && value
            .bytes()
            .all(|v| v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
    {
        Ok(value)
    } else {
        Err(failure("bundle"))
    }
}
/// Only the running command can implement this authority. Credentials are
/// deliberately absent from its arguments and result.
pub trait Guest: Send + Sync {
    fn path(&self, path: &Path) -> Result<String> {
        Ok(path.to_string_lossy().into())
    }
    fn run<'a>(
        &'a self,
        cwd: &'a Path,
        argv: Vec<String>,
        env: BTreeMap<String, String>,
        stdin: Vec<u8>,
        limit: usize,
    ) -> Pending<'a, Output>;
}

struct Authentication {
    env: BTreeMap<String, String>,
    _temporary: Option<tempfile::TempDir>,
}
impl Authentication {
    fn new(credential: Option<GitCredential>, count: &str) -> Result<Self> {
        let mut env = BTreeMap::new();
        let count = count.parse::<usize>().map_err(|_| failure("git_failed"))?;
        env.insert(
            "GIT_CONFIG_COUNT".into(),
            count
                .checked_add(1)
                .ok_or_else(|| failure("git_failed"))?
                .to_string(),
        );
        env.insert(
            format!("GIT_CONFIG_KEY_{count}"),
            "credential.helper".into(),
        );
        env.insert(format!("GIT_CONFIG_VALUE_{count}"), String::new());
        let temporary = if credential.is_some() {
            Some(tempfile::tempdir().map_err(io_error)?)
        } else {
            None
        };
        if let Some(credential) = credential {
            let script = temporary
                .as_ref()
                .expect("credential temporary")
                .path()
                .join("askpass.sh");
            std::fs::write(&script,b"#!/bin/sh\ncase \"$1\" in\n*Username*) printf '%s\\n' \"$GIT_USERNAME\" ;;\n*Password*) printf '%s\\n' \"$GIT_PASSWORD\" ;;\nesac\n").map_err(io_error)?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o700))
                    .map_err(io_error)?;
            }
            env.insert("GIT_ASKPASS".into(), script.to_string_lossy().into());
            env.insert("GIT_USERNAME".into(), credential.username);
            env.insert("GIT_PASSWORD".into(), credential.password);
        }
        Ok(Self {
            env,
            _temporary: temporary,
        })
    }
}
/// Executes only in a directory created by the host under its private root.
/// No constructor accepts a clone's git directory or configuration.
pub struct Origin {
    directory: PathBuf,
    url: String,
    deadline: Instant,
}
impl Origin {
    pub async fn open(
        root: &Path,
        repository: &str,
        url: String,
        deadline: Instant,
    ) -> Result<Self> {
        if repository
            .split('/')
            .any(|v| v.is_empty() || v == "." || v == ".." || v.contains(['\\', '\0', ':']))
            || repository.starts_with('/')
        {
            return Err(failure("path"));
        }
        repository_path(repository)?;
        let directory = root.join("repositories").join(format!("{repository}.git"));
        gb_base::host_path::directory(root, true).map_err(io_error)?;
        let parent = directory.parent().ok_or_else(|| failure("path"))?;
        gb_base::host_path::directory(parent, true).map_err(io_error)?;
        std::fs::create_dir_all(parent).map_err(io_error)?;
        let origin = Self {
            directory: directory.clone(),
            url,
            deadline,
        };
        if !origin.directory.exists() {
            origin
                .command_at(
                    parent,
                    vec![
                        "init".into(),
                        "--bare".into(),
                        "-q".into(),
                        "--".into(),
                        origin
                            .directory
                            .file_name()
                            .unwrap()
                            .to_string_lossy()
                            .into(),
                    ],
                    BTreeMap::new(),
                    vec![],
                    MAX_OUTPUT,
                )
                .await?
                .checked()?;
        }
        gb_base::host_path::directory(&origin.directory, false).map_err(io_error)?;
        origin
            .git(
                &["config", "--replace-all", "remote.origin.url", &origin.url],
                vec![],
            )
            .await?;
        Ok(origin)
    }
    async fn command_at(
        &self,
        cwd: &Path,
        args: Vec<String>,
        env: BTreeMap<String, String>,
        stdin: Vec<u8>,
        limit: usize,
    ) -> Result<Output> {
        let mut command = tokio::process::Command::new("git");
        command
            .args(["-c", "fetch.fsckObjects=true"])
            .args(args)
            .current_dir(cwd)
            .env("GIT_TERMINAL_PROMPT", "0")
            .envs(env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        // A caller's shell must never select another Git directory on the host.
        for name in [
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_INDEX_FILE",
        ] {
            command.env_remove(name);
        }
        let mut child = command.spawn().map_err(io_error)?;
        let mut input = child.stdin.take().unwrap();
        let output = child.stdout.take().unwrap();
        let error = child.stderr.take().unwrap();
        let job = async {
            let write = async {
                input.write_all(&stdin).await.map_err(io_error)?;
                drop(input);
                Ok::<_, MemberError>(())
            };
            let read = async {
                let mut bytes = vec![];
                output
                    .take(limit as u64 + 1)
                    .read_to_end(&mut bytes)
                    .await
                    .map_err(io_error)?;
                if bytes.len() > limit {
                    return Err(failure("bundle"));
                }
                Ok(bytes)
            };
            let errors = async {
                let mut bytes = vec![];
                error
                    .take(MAX_OUTPUT as u64 + 1)
                    .read_to_end(&mut bytes)
                    .await
                    .map_err(io_error)?;
                if bytes.len() > MAX_OUTPUT {
                    return Err(failure("bundle"));
                }
                Ok(bytes)
            };
            let (_, stdout, stderr) = tokio::try_join!(write, read, errors)?;
            let status = child.wait().await.map_err(io_error)?;
            Ok(Output {
                code: status.code().unwrap_or(1),
                stdout,
                stderr,
            })
        };
        let result = tokio::time::timeout(remaining(self.deadline)?, job).await;
        match result {
            Ok(Ok(output)) => Ok(output),
            failed => {
                let _ = child.kill().await;
                let _ = child.wait().await;
                match failed {
                    Ok(Err(error)) => Err(error),
                    _ => Err(failure("timeout")),
                }
            }
        }
    }
    pub async fn git(&self, args: &[&str], stdin: Vec<u8>) -> Result<Output> {
        self.command_at(
            &self.directory,
            args.iter().map(|v| (*v).into()).collect(),
            BTreeMap::new(),
            stdin,
            MAX_OUTPUT,
        )
        .await?
        .checked()
    }
    pub async fn branch(&self, branch: &str) -> Result<()> {
        let result = self
            .command_at(
                &self.directory,
                vec!["check-ref-format".into(), "--branch".into(), branch.into()],
                BTreeMap::new(),
                vec![],
                MAX_OUTPUT,
            )
            .await?;
        if result.code != 0 || branch.starts_with('-') || result.text() != branch {
            return Err(branch_error(
                "capabilities.member_git.invalid_branch",
                branch,
            ));
        }
        Ok(())
    }
    async fn connected(&self, args: &[&str], credential: Option<GitCredential>) -> Result<Output> {
        self.authenticated(args, credential, vec![]).await
    }
    async fn authenticated(
        &self,
        args: &[&str],
        credential: Option<GitCredential>,
        input: Vec<u8>,
    ) -> Result<Output> {
        for args in [
            vec!["remote", "get-url", "--all", "origin"],
            vec!["remote", "get-url", "--push", "--all", "origin"],
        ] {
            if self.git(&args, vec![]).await?.text() != self.url {
                return Err(failure("redirect"));
            }
        }
        let authentication = Authentication::new(
            credential,
            &std::env::var("GIT_CONFIG_COUNT").unwrap_or_else(|_| "0".into()),
        )?;
        // The temporary directory and credentials live until the child settles.
        self.command_at(
            &self.directory,
            args.iter().map(|v| (*v).into()).collect(),
            authentication.env.clone(),
            input,
            MAX_OUTPUT,
        )
        .await?
        .checked_text(args.first() == Some(&"push"))
    }
    pub async fn fetch(&self, credential: Option<GitCredential>) -> Result<()> {
        self.connected(
            &[
                "fetch",
                "-q",
                "--prune",
                "origin",
                "+refs/heads/*:refs/remotes/origin/*",
                "+refs/tags/*:refs/tags/*",
            ],
            credential,
        )
        .await?;
        let refs = self.refs(&["refs/remotes/origin/", "refs/tags/"]).await?;
        self.remember(refs.values().cloned().collect()).await
    }
    /// Combine host-fetched objects with client history in a disposable repository.
    /// Only the client's refs select the returned bundle; fetched refs never escape.
    pub async fn complete_history(
        &self,
        incoming: &[u8],
        others: &[Origin],
        refs: &BTreeMap<String, String>,
        head: &str,
    ) -> Result<Vec<u8>> {
        let temporary = tempfile::tempdir().map_err(io_error)?;
        let assembled = Origin::open(
            temporary.path(),
            "copy/repository",
            String::new(),
            self.deadline,
        )
        .await?;
        // The caller holds the member Git lock through the final self-contained export.
        // Borrow only private objects; never import fetched refs or expose alternates to guests.
        let mut alternates = String::new();
        for origin in std::iter::once(self).chain(others.iter()) {
            let objects =
                std::fs::canonicalize(origin.directory.join("objects")).map_err(io_error)?;
            let objects = objects.to_str().ok_or_else(|| failure("path"))?;
            if objects.contains(['\n', '\r']) {
                return Err(failure("path"));
            }
            alternates.push_str(objects);
            alternates.push('\n');
        }
        std::fs::write(
            assembled.directory.join("objects/info/alternates"),
            alternates,
        )
        .map_err(io_error)?;
        if !incoming.is_empty() {
            let path = temporary.path().join("delta.bundle");
            std::fs::write(&path, incoming).map_err(io_error)?;
            let path = path.to_str().ok_or_else(|| failure("path"))?;
            assembled.git(&["bundle", "verify", path], vec![]).await?;
            assembled.git(&["bundle", "unbundle", path], vec![]).await?;
        }
        let mut revisions = Vec::new();
        for (name, oid) in refs {
            if !name.starts_with("refs/heads/")
                && !name.starts_with("refs/remotes/")
                && !name.starts_with("refs/tags/")
            {
                return Err(failure("bundle"));
            }
            object(oid)?;
            assembled.git(&["update-ref", name, oid], vec![]).await?;
            revisions.push(name.clone());
        }
        if !head.starts_with("refs/heads/") {
            object(head)?;
            assembled
                .git(&["update-ref", "refs/guildbotics/detached", head], vec![])
                .await?;
            revisions.push("refs/guildbotics/detached".into());
        }
        if revisions.is_empty() {
            return Ok(vec![]);
        }
        Ok(assembled
            .command_at(
                &assembled.directory,
                vec![
                    "bundle".into(),
                    "create".into(),
                    "-".into(),
                    "--stdin".into(),
                ],
                BTreeMap::new(),
                (revisions.join("\n") + "\n").into_bytes(),
                MAX_BUNDLE,
            )
            .await?
            .checked_bundle()?
            .stdout)
    }
    pub async fn remember(&self, names: Vec<String>) -> Result<()> {
        let mut input = String::new();
        for name in names.into_iter().collect::<std::collections::BTreeSet<_>>() {
            object(&name)?;
            input.push_str(&format!("update {PUBLIC}{name} {name}\n"));
        }
        self.git(&["update-ref", "--stdin"], input.into_bytes())
            .await?;
        Ok(())
    }
    pub async fn refs(&self, prefixes: &[&str]) -> Result<BTreeMap<String, String>> {
        let mut args = vec!["for-each-ref", "--format=%(refname) %(objectname)"];
        args.extend(prefixes);
        let output = self.git(&args, vec![]).await?;
        output
            .text()
            .lines()
            .map(|line| {
                let (r, n) = line.split_once(' ').ok_or_else(|| failure("bundle"))?;
                object(n)?;
                Ok((r.into(), n.into()))
            })
            .collect()
    }
    pub async fn receive(&self, branch: &str, head: &str, incoming: Option<&[u8]>) -> Result<bool> {
        self.branch(branch).await?;
        object(head)?;
        let reference = format!("refs/heads/{branch}");
        if let Some(incoming) = incoming {
            if incoming.len() > MAX_BUNDLE {
                return Err(failure("bundle"));
            }
            let tmp = tempfile::tempdir().map_err(io_error)?;
            let bundle = tmp.path().join("in.bundle");
            std::fs::write(&bundle, incoming).map_err(io_error)?;
            let path = bundle.to_str().ok_or_else(|| failure("path"))?;
            let verified = self
                .command_at(
                    &self.directory,
                    vec!["bundle".into(), "verify".into(), path.into()],
                    BTreeMap::new(),
                    vec![],
                    MAX_OUTPUT,
                )
                .await?;
            if verified.code != 0 {
                return Ok(false);
            }
            self.git(
                &[
                    "fetch",
                    "-q",
                    "--no-tags",
                    "--",
                    path,
                    &format!("+{reference}:{reference}"),
                ],
                vec![],
            )
            .await?;
            if self
                .git(&["rev-parse", "--verify", &reference], vec![])
                .await?
                .text()
                != head
            {
                return Err(failure("bundle"));
            }
        } else {
            let present = self
                .command_at(
                    &self.directory,
                    vec!["cat-file".into(), "-e".into(), format!("{head}^{{commit}}")],
                    BTreeMap::new(),
                    vec![],
                    MAX_OUTPUT,
                )
                .await?;
            if present.code != 0 {
                return Ok(false);
            }
            self.git(&["update-ref", &reference, head], vec![]).await?;
        }
        Ok(true)
    }
    pub async fn push(&self, branch: &str, credential: Option<GitCredential>) -> Result<bool> {
        self.branch(branch).await?;
        let reference = format!("refs/heads/{branch}");
        let remote = format!("refs/remotes/origin/{branch}");
        let refs = self.refs(&[&reference, &remote]).await?;
        let head = refs
            .get(&reference)
            .ok_or_else(|| branch_error("capabilities.member_git.branch_empty", branch))?;
        if refs.get(&remote) == Some(head) {
            return Ok(false);
        }
        self.connected(
            &[
                "push",
                "--porcelain",
                "--no-follow-tags",
                "origin",
                &format!("{reference}:{reference}"),
            ],
            credential,
        )
        .await
        .map_err(|error| match error {
            MemberError::CapabilityArgs {
                key: "capabilities.member_git.git_failed_reason",
                mut args,
            } => {
                args.insert("branch".into(), branch.into());
                MemberError::CapabilityArgs {
                    key: "capabilities.member_git.push_failed",
                    args,
                }
            }
            other => other,
        })?;
        self.remember(vec![head.clone()]).await?;
        self.git(&["update-ref", &remote, head], vec![]).await?;
        Ok(true)
    }
    async fn show_path(&self, commit: &str, path: &str) -> Result<Value> {
        let (name, kind) = if path == "." {
            (format!("{commit}^{{tree}}"), "directory")
        } else {
            let listed = self
                .git(
                    &["--literal-pathspecs", "ls-tree", "-z", commit, "--", path],
                    vec![],
                )
                .await?;
            if listed.stdout.is_empty() {
                return Ok(json!({"path":path,"kind":"missing"}));
            }
            let header = listed.stdout.split(|v| *v == b'\t').next().unwrap();
            let fields = std::str::from_utf8(header)
                .map_err(|_| failure("bundle"))?
                .split_whitespace()
                .collect::<Vec<_>>();
            if fields.len() != 3 {
                return Err(failure("bundle"));
            }
            (object(fields[2])?.to_owned(), tree_kind(fields[0]))
        };
        if kind == "submodule" {
            return Ok(json!({"path":path,"kind":kind,"commit":name}));
        }
        let size = self
            .git(&["cat-file", "-s", &name], vec![])
            .await?
            .text()
            .parse::<usize>()
            .map_err(|_| failure("bundle"))?;
        let mut result = json!({"path":path,"kind":kind,"size":size});
        if size > gb_ports::MAX_PAGE_BYTES / 2 {
            result["status"] = "too_large".into();
            return Ok(result);
        }
        if kind == "directory" {
            let output = self.git(&["ls-tree", "-z", &name], vec![]).await?;
            let mut entries = vec![];
            for entry in output.stdout.split(|v| *v == 0).filter(|v| !v.is_empty()) {
                let (header, file) = entry.split_at(
                    entry
                        .iter()
                        .position(|v| *v == b'\t')
                        .ok_or_else(|| failure("bundle"))?,
                );
                let mode = std::str::from_utf8(header)
                    .map_err(|_| failure("bundle"))?
                    .split_whitespace()
                    .next()
                    .unwrap_or("");
                entries.push(
                    json!({"name":String::from_utf8_lossy(&file[1..]),"kind":tree_kind(mode)}),
                );
            }
            result["status"] = "ok".into();
            result["entries"] = entries.into();
        } else {
            let output = self.git(&["cat-file", "blob", &name], vec![]).await?;
            match String::from_utf8(output.stdout) {
                Ok(text) if !text.contains('\0') => {
                    result["status"] = "ok".into();
                    result["content"] = text.into();
                }
                _ => result["status"] = "binary".into(),
            }
        }
        Ok(result)
    }
}
fn repository_path(repository: &str) -> Result<()> {
    if gb_ports::valid_repository_path(repository) {
        Ok(())
    } else {
        Err(MemberError::CapabilityArgs {
            key: "capabilities.member_git.invalid_repository",
            args: [("repository".into(), repository.into())].into(),
        })
    }
}
fn tree_kind(mode: &str) -> &'static str {
    match mode {
        "040000" => "directory",
        "120000" => "symlink",
        "160000" => "submodule",
        _ => "file",
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Checkout {
    repository: String,
    owner: String,
    #[serde(default)]
    target: Option<(gb_ports::WorkKind, String)>,
    branch: String,
    #[serde(default)]
    discarded: bool,
    #[serde(default)]
    removed: bool,
}
/// Host-owned invocation data. The requester cannot choose its work or guest.
pub struct Workspace<'a> {
    pub host_root: PathBuf,
    pub clones: PathBuf,
    pub cwd: PathBuf,
    pub work: Option<(&'a str, &'a str)>,
    pub guest: &'a dyn Guest,
    pub services: &'a dyn crate::repository::RepositoryServices,
    pub guard: &'a dyn WriteGuard,
    pub name: &'a str,
    pub email: &'a str,
    pub deadline: Instant,
    pub render_error: &'a (dyn Fn(&MemberError) -> String + Send + Sync),
}
fn digest(value: impl Serialize) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(crate::identity::bytes(value))
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
impl Workspace<'_> {
    pub fn resolve(&self, path: &Path) -> Result<PathBuf> {
        gb_base::host_path::normalize(&if path.is_absolute() || path.starts_with("~") {
            path.into()
        } else {
            self.cwd.join(path)
        })
        .map_err(|_| failure("path"))
    }
    fn code(&self) -> Result<&dyn CodeHostingService> {
        self.services.code()
    }
    /// Interactive clients exchange only history and identity metadata. The
    /// repo_path in a receipt is never opened by the host.
    pub async fn client_git(
        &self,
        phase: &str,
        data: Value,
        incoming: Option<&[u8]>,
    ) -> Result<(Value, Vec<u8>)> {
        self.client_git_with(phase, data, incoming, &|| {}).await
    }
    pub async fn client_git_with(
        &self,
        phase: &str,
        data: Value,
        incoming: Option<&[u8]>,
        before_push: &(dyn Fn() + Send + Sync),
    ) -> Result<(Value, Vec<u8>)> {
        if phase == "identity" {
            return Ok((json!({"user":self.name,"email":self.email}), vec![]));
        }
        if phase == "record" {
            self.guard.ensure_current()?;
            let receipt: Commit = serde_json::from_value(data).map_err(|_| failure("bundle"))?;
            return Ok((
                serde_json::to_value(receipt).map_err(|_| failure("bundle"))?,
                vec![],
            ));
        }
        if !matches!(phase, "known" | "push" | "tracking") {
            return Err(failure("bundle"));
        }
        let remotes = data["remotes"].as_array().ok_or_else(|| failure("path"))?;
        if remotes.len() != 1 {
            return Err(failure("path"));
        }
        let repository = self
            .code()?
            .repository_from_remote(remotes[0].as_str().ok_or_else(|| failure("path"))?)
            .ok_or_else(|| failure("path"))?;
        self.code()?.check_push(&repository)?;
        let root = self.host_root.join("current");
        let _lock =
            gb_base::files::FileLock::hold(&root.join("git.lock"), remaining(self.deadline)?)
                .map_err(lock_error)?;
        let origin = Origin::open(
            &root,
            &repository,
            self.code()?.clone_url(&repository).await?,
            self.deadline,
        )
        .await?;
        if phase != "tracking" {
            origin.fetch(self.code()?.git_credential().await?).await?;
        }
        if phase == "known" {
            let listed = origin
                .git(
                    &["for-each-ref", "--format=%(objectname) %(objecttype)"],
                    vec![],
                )
                .await?
                .text();
            let known = listed
                .lines()
                .filter_map(|v| v.strip_suffix(" commit"))
                .collect::<std::collections::BTreeSet<_>>();
            return Ok((json!({"known":known}), vec![]));
        }
        if phase == "tracking" {
            let refs = origin.refs(&["refs/remotes/origin/"]).await?;
            let mut known = std::collections::BTreeSet::new();
            if let Some(names) = data.get("known") {
                for name in names.as_array().ok_or_else(|| failure("bundle"))? {
                    let name = object(name.as_str().ok_or_else(|| failure("bundle"))?)?;
                    if origin
                        .git(&["cat-file", "-e", &format!("{name}^{{commit}}")], vec![])
                        .await
                        .is_ok()
                    {
                        known.insert(name.to_owned());
                    }
                }
            }
            let mut revisions = refs
                .iter()
                .filter(|(_, sha)| !known.contains(*sha))
                .map(|(r, _)| r.clone())
                .collect::<Vec<_>>();
            if revisions.is_empty() {
                return Ok((json!({"refs":refs}), vec![]));
            }
            let mut check = vec!["rev-list", "--max-count=1"];
            check.extend(revisions.iter().map(String::as_str));
            check.push("--not");
            check.extend(known.iter().map(String::as_str));
            check.push("--");
            if origin.git(&check, vec![]).await?.stdout.is_empty() {
                return Ok((json!({"refs":refs}), vec![]));
            }
            revisions.extend(known.into_iter().map(|v| format!("^{v}")));
            let bundle = origin
                .command_at(
                    &origin.directory,
                    vec![
                        "bundle".into(),
                        "create".into(),
                        "-q".into(),
                        "-".into(),
                        "--stdin".into(),
                    ],
                    BTreeMap::new(),
                    (revisions.join("\n") + "\n").into_bytes(),
                    MAX_BUNDLE,
                )
                .await?
                .checked_bundle()?;
            return Ok((json!({"refs":refs}), bundle.stdout));
        }
        self.guard.ensure_current()?;
        let branch = data["branch"].as_str().ok_or_else(|| failure("bundle"))?;
        let head = object(data["head"].as_str().ok_or_else(|| failure("bundle"))?)?;
        if !origin.receive(branch, head, incoming).await? {
            return Ok((json!({"retry":true}), vec![]));
        }
        let reference = format!("refs/heads/{branch}");
        let listed = origin
            .git(
                &[
                    "log",
                    "--format=%H%x00%s",
                    &reference,
                    "--not",
                    "--remotes=origin",
                    "--",
                ],
                vec![],
            )
            .await?
            .text();
        let code = self.code()?;
        let commits=listed.lines().filter_map(|l|l.split_once('\0')).map(|(sha,subject)|json!({"id":sha,"message":subject,"url":code.commit_url(&repository,sha)})).collect::<Vec<_>>();
        before_push();
        let pushed = origin
            .push(branch, self.code()?.git_credential().await?)
            .await?;
        origin.remember(vec![head.into()]).await?;
        origin
            .git(
                &["update-ref", &format!("refs/remotes/origin/{branch}"), head],
                vec![],
            )
            .await?;
        let mut receipt = json!({"repo_path":data["repo_path"],"branch":branch,"pushed":pushed,"status":if pushed{"pushed"}else{"up_to_date"},"commits":commits,"pull_requests":[]});
        match crate::repository::open_pull_request_readiness(self.code()?, &repository, branch)
            .await
        {
            Ok(prs) => {
                receipt["pull_requests"] =
                    serde_json::to_value(prs).map_err(|_| failure("bundle"))?
            }
            Err(error) => receipt["pull_requests_error"] = (self.render_error)(&error).into(),
        }
        Ok((receipt, vec![]))
    }
    async fn target(&self) -> Option<(gb_ports::WorkKind, String)> {
        use gb_ports::WorkKind;
        let (kind, url) = self.work?;
        let kind = match kind {
            "ticket" => WorkKind::Ticket,
            "pull_request" => WorkKind::PullRequest,
            _ => return None,
        };
        // Conversation identity is independent of the provider's target kind.
        let canonical = self.state(kind, url).await.map(|v| v.0).unwrap_or(kind);
        Some((canonical, url.into()))
    }
    async fn state(
        &self,
        kind: gb_ports::WorkKind,
        url: &str,
    ) -> Result<(gb_ports::WorkKind, bool)> {
        use gb_ports::WorkKind;
        match self.read_state(kind, url).await {
            Ok(state) => Ok(state),
            Err(_) => {
                self.read_state(
                    match kind {
                        WorkKind::Ticket => WorkKind::PullRequest,
                        WorkKind::PullRequest => WorkKind::Ticket,
                    },
                    url,
                )
                .await
            }
        }
    }
    async fn read_state(
        &self,
        kind: gb_ports::WorkKind,
        url: &str,
    ) -> Result<(gb_ports::WorkKind, bool)> {
        use gb_ports::{Lane, Resource, WorkKind};
        let page = match kind {
            WorkKind::Ticket => {
                self.services
                    .tickets()?
                    .read(Resource::Tickets, url, None, "")
                    .await?
            }
            WorkKind::PullRequest => {
                self.code()?
                    .read(Resource::PullRequests, url, None, "")
                    .await?
            }
        };
        let value = page
            .items()
            .first()
            .ok_or(gb_ports::ServiceError::InvalidResult)?
            .clone();
        Ok(match kind {
            WorkKind::Ticket => {
                let t: gb_ports::Ticket = serde_json::from_value(value)
                    .map_err(|_| gb_ports::ServiceError::InvalidResult)?;
                (t.kind, t.state == "open" && t.lane != Some(Lane::Done))
            }
            WorkKind::PullRequest => {
                let p: gb_ports::PullRequest = serde_json::from_value(value)
                    .map_err(|_| gb_ports::ServiceError::InvalidResult)?;
                (p.kind, p.state == "open" && !p.merged)
            }
        })
    }
    async fn active(&self, record: &Checkout) -> bool {
        let Some((kind, url)) = &record.target else {
            return false;
        };
        // Unknown state must retain the checkout without blocking other work.
        self.state(*kind, url).await.map(|v| v.1).unwrap_or(true)
    }
    fn owner(&self) -> Result<String> {
        self.work.map(digest).ok_or_else(|| failure("work"))
    }
    async fn run(
        &self,
        path: &Path,
        args: &[&str],
        input: Vec<u8>,
        limit: usize,
    ) -> Result<Output> {
        let mut argv = vec!["git".into()];
        argv.extend(args.iter().map(|v| (*v).into()));
        self.guest
            .run(
                path,
                argv,
                [("GIT_TERMINAL_PROMPT".into(), "0".into())].into(),
                input,
                limit,
            )
            .await
    }
    async fn git(&self, path: &Path, args: &[&str], input: Vec<u8>) -> Result<Output> {
        self.run(path, args, input, MAX_OUTPUT).await?.checked()
    }
    fn record_path(&self, slot: &str) -> PathBuf {
        self.host_root
            .join("checkouts")
            .join(format!("{slot}.json"))
    }
    fn save(&self, slot: &str, record: &Checkout) -> Result<()> {
        gb_base::files::atomic_write(
            &self.record_path(slot),
            &serde_json::to_vec(record).map_err(|_| failure("path"))?,
        )
        .map_err(io_error)
    }
    fn records(&self) -> Result<BTreeMap<String, Checkout>> {
        let root = self.host_root.join("checkouts");
        std::fs::create_dir_all(&root).map_err(io_error)?;
        let mut result = BTreeMap::new();
        for entry in std::fs::read_dir(root).map_err(io_error)? {
            let path = entry.map_err(io_error)?.path();
            if path.extension().is_some_and(|v| v == "json") {
                let slot = path.file_stem().unwrap().to_string_lossy().into_owned();
                object(&slot[..slot.len().min(40)])?;
                result.insert(
                    slot,
                    serde_json::from_slice(&std::fs::read(path).map_err(io_error)?)
                        .map_err(|_| failure("path"))?,
                );
            }
        }
        Ok(result)
    }
    async fn origin(&self, repository: &str) -> Result<Origin> {
        Origin::open(
            &self.host_root,
            repository,
            self.code()?.clone_url(repository).await?,
            self.deadline,
        )
        .await
    }
    async fn assignment(&self, path: &Path) -> Result<Option<Checkout>> {
        // Validate spelling only; a clone's .git may be a worktree pointer.
        if path
            .components()
            .any(|c| c == std::path::Component::ParentDir)
            || !path.starts_with(&self.clones)
            || path == self.clones
        {
            return Err(failure("path"));
        }
        let common = self
            .git(
                path,
                &["rev-parse", "--path-format=absolute", "--git-common-dir"],
                vec![],
            )
            .await?
            .text();
        let common = Path::new(&common);
        let place = common.parent().ok_or_else(|| failure("path"))?;
        if common.file_name().is_none_or(|v| v != ".git")
            || place.parent() != Some(Path::new(&self.guest.path(&self.clones)?))
        {
            return Ok(None);
        }
        let slot = place
            .file_name()
            .unwrap()
            .to_str()
            .ok_or_else(|| failure("path"))?;
        let Some(bytes) =
            gb_base::files::read_if_present(&self.record_path(slot)).map_err(io_error)?
        else {
            return Ok(None);
        };
        let record: Checkout = serde_json::from_slice(&bytes).map_err(|_| failure("path"))?;
        if record.discarded || record.owner != self.owner()? {
            return Err(failure("assignment"));
        }
        Ok(Some(record))
    }
    pub async fn known(&self, path: &Path) -> Result<Vec<String>> {
        if self
            .git(path, &["rev-parse", "--is-shallow-repository"], vec![])
            .await?
            .text()
            == "true"
        {
            return Ok(vec![]);
        }
        Ok(self
            .git(
                path,
                &["for-each-ref", "--format=%(objectname) %(objecttype)"],
                vec![],
            )
            .await?
            .text()
            .lines()
            .filter_map(|l| l.strip_suffix(" commit").map(str::to_owned))
            .collect())
    }
    async fn send(
        &self,
        origin: &Origin,
        path: &Path,
        refs: &BTreeMap<String, String>,
    ) -> Result<()> {
        let mut wanted = vec![];
        for (reference, sha) in refs {
            if self
                .run(
                    path,
                    &["cat-file", "-e", &format!("{sha}^{{commit}}")],
                    vec![],
                    MAX_OUTPUT,
                )
                .await?
                .code
                != 0
            {
                wanted.push(reference.clone());
            }
        }
        if wanted.is_empty() {
            return Ok(());
        }
        let known = self.known(path).await?;
        let mut revisions = wanted.clone();
        for sha in known {
            object(&sha)?;
            if origin
                .git(&["cat-file", "-e", &format!("{sha}^{{commit}}")], vec![])
                .await
                .is_ok()
            {
                revisions.push(format!("^{sha}"));
            }
        }
        for attempt in 0..2 {
            let input = (if attempt == 0 { &revisions } else { &wanted }).join("\n") + "\n";
            let bundle = origin
                .command_at(
                    &origin.directory,
                    vec![
                        "bundle".into(),
                        "create".into(),
                        "-q".into(),
                        "-".into(),
                        "--stdin".into(),
                    ],
                    BTreeMap::new(),
                    input.into_bytes(),
                    MAX_BUNDLE,
                )
                .await?
                .checked_bundle()?;
            let script = "f=$(mktemp) || exit 1; cat >\"$f\" || exit 1; git bundle verify \"$f\" >/dev/null 2>&1 || { rm -f \"$f\"; exit 3; }; git bundle unbundle \"$f\" >/dev/null; s=$?; rm -f \"$f\"; exit $s";
            let result = self
                .guest
                .run(
                    path,
                    vec!["sh".into(), "-c".into(), script.into()],
                    BTreeMap::new(),
                    bundle.stdout,
                    MAX_OUTPUT,
                )
                .await?;
            if result.code == 0 {
                return Ok(());
            }
            if result.code != 3 || attempt == 1 {
                return Err(failure("bundle"));
            }
        }
        Err(failure("bundle"))
    }
    async fn reusable(
        &self,
        origin: &Origin,
        path: &Path,
        public: &BTreeMap<String, String>,
    ) -> Result<bool> {
        if !self
            .git(
                path,
                &[
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--ignore-submodules=none",
                ],
                vec![],
            )
            .await?
            .stdout
            .is_empty()
        {
            return Ok(false);
        }
        if self
            .run(path, &["symbolic-ref", "-q", "HEAD"], vec![], MAX_OUTPUT)
            .await?
            .code
            != 0
        {
            return Ok(false);
        }
        let files = self.git(path, &["ls-files", "-v", "-z"], vec![]).await?;
        if files
            .stdout
            .split(|v| *v == 0)
            .any(|v| !v.is_empty() && v[0] != b'H')
        {
            return Ok(false);
        }
        let script = "for s in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD MERGE_AUTOSTASH rebase-merge rebase-apply sequencer BISECT_LOG index.lock HEAD.lock packed-refs.lock config.lock shallow.lock; do p=$(git rev-parse --git-path \"$s\") || exit 2; [ ! -e \"$p\" ] || exit 1; done";
        if self
            .guest
            .run(
                path,
                vec!["sh".into(), "-c".into(), script.into()],
                BTreeMap::new(),
                vec![],
                MAX_OUTPUT,
            )
            .await?
            .code
            != 0
        {
            return Ok(false);
        }
        let worktrees = self
            .git(path, &["worktree", "list", "--porcelain", "-z"], vec![])
            .await?;
        if worktrees
            .stdout
            .split(|v| *v == 0)
            .filter(|v| v.starts_with(b"worktree "))
            .count()
            != 1
        {
            return Ok(false);
        }
        let refs = self
            .git(
                path,
                &["for-each-ref", "--format=%(refname) %(objectname)"],
                vec![],
            )
            .await?
            .text();
        if refs.lines().any(|v| v.starts_with("refs/stash ")) {
            return Ok(false);
        }
        self.send(origin, path, public).await?;
        let mut revisions = vec!["HEAD".into()];
        for line in refs.lines() {
            let (r, n) = line.split_once(' ').ok_or_else(|| failure("bundle"))?;
            if !r.starts_with("refs/remotes/") {
                revisions.push(object(n)?.into());
            }
        }
        revisions.extend(public.values().map(|v| format!("^{v}")));
        Ok(self
            .git(
                path,
                &["rev-list", "--objects", "--max-count=1", "--stdin"],
                (revisions.join("\n") + "\n").into_bytes(),
            )
            .await?
            .stdout
            .is_empty())
    }
    /// The caller resolves a ticket or PR through the provider before passing
    /// its canonical repository and branch here.
    pub async fn prepare(&self, repository: &str, requested: &str) -> Result<Value> {
        let owner = self.owner()?;
        repository_path(repository)?;
        let _lock = gb_base::files::FileLock::hold(
            &self.host_root.join("git.lock"),
            remaining(self.deadline)?,
        )
        .map_err(lock_error)?;
        let origin = self.origin(repository).await?;
        origin.branch(requested).await?;
        let default = self.code()?.default_branch(repository).await?;
        origin.branch(&default).await?;
        origin.fetch(self.code()?.git_credential().await?).await?;
        let refs = origin.refs(&["refs/remotes/origin/", "refs/tags/"]).await?;
        let public = origin
            .refs(&["refs/remotes/origin/", "refs/tags/", PUBLIC])
            .await?;
        let records = self.records()?;
        let target = self.target().await;
        let mut owned = None;
        let mut idle = vec![];
        for (slot, record) in &records {
            if record.repository != repository || record.discarded {
                continue;
            }
            if record.owner == owner
                && (record.branch == requested
                    || matches!((&record.target, &target), (Some((_, old)), Some((_, current))) if old == current))
            {
                owned = Some(slot.clone());
            }
            if !self.active(record).await
                && self
                    .reusable(&origin, &self.clones.join(slot), &public)
                    .await
                    .unwrap_or(false)
            {
                idle.push(slot.clone());
            }
        }
        let slot = owned.or_else(|| idle.first().cloned()).unwrap_or_else(|| {
            (0u64..)
                .map(|n| digest((repository, n)))
                .find(|s| !records.contains_key(s))
                .unwrap()
        });
        let path = self.clones.join(&slot);
        let initialized = self
            .run(
                &path,
                &["rev-parse", "-q", "--verify", "HEAD"],
                vec![],
                MAX_OUTPUT,
            )
            .await
            .is_ok_and(|v| v.code == 0);
        let reset = !initialized || idle.contains(&slot);
        let record = Checkout {
            repository: repository.into(),
            owner,
            target,
            branch: requested.into(),
            discarded: false,
            removed: false,
        };
        if !initialized {
            self.save(&slot, &record)?;
            self.git(&self.clones, &["init", "-q", "--", &slot], vec![])
                .await?;
        }
        if reset {
            for (key, value) in [
                ("remote.origin.url", origin.url.as_str()),
                ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
                ("user.name", self.name),
                ("user.email", self.email),
            ] {
                self.git(&path, &["config", "--replace-all", key, value], vec![])
                    .await?;
            }
        }
        self.send(&origin, &path, &refs).await?;
        let old = self
            .git(
                &path,
                &[
                    "for-each-ref",
                    "--format=delete %(refname)",
                    "refs/remotes/origin/",
                ],
                vec![],
            )
            .await?
            .stdout;
        self.git(&path, &["update-ref", "--no-deref", "--stdin"], old)
            .await?;
        let existing_tags = self
            .git(
                &path,
                &["for-each-ref", "--format=%(refname)", "refs/tags/"],
                vec![],
            )
            .await?
            .text();
        let updates = refs
            .iter()
            .filter(|(r, _)| {
                !r.starts_with("refs/tags/") || !existing_tags.lines().any(|old| old == r.as_str())
            })
            .map(|(r, n)| format!("update {r} {n}\n"))
            .collect::<String>();
        self.git(
            &path,
            &["update-ref", "--no-deref", "--stdin"],
            updates.into_bytes(),
        )
        .await?;
        if reset {
            let remote = format!("refs/remotes/origin/{requested}");
            let start = if refs.contains_key(&remote) {
                remote
            } else {
                format!("refs/remotes/origin/{default}")
            };
            self.git(
                &path,
                &[
                    "checkout",
                    "-q",
                    "--track=direct",
                    "-B",
                    requested,
                    &start,
                    "--",
                ],
                vec![],
            )
            .await?;
        }
        self.save(&slot, &record)?;
        let mut retired = records.clone();
        let warm = (!idle.contains(&slot))
            .then(|| idle.first().cloned())
            .flatten();
        for other in idle
            .into_iter()
            .filter(|v| v != &slot && Some(v) != warm.as_ref())
        {
            let record = retired.get_mut(&other).unwrap();
            record.discarded = true;
            self.save(&other, record)?;
        }
        for (other, record) in &retired {
            if !record.discarded || record.removed || record.repository != repository {
                continue;
            }
            // Discarded IDs remain reserved even after deletion. Never inspect
            // partially removed Git metadata when retrying cleanup.
            let result = self
                .guest
                .run(
                    &self.clones,
                    vec!["rm".into(), "-rf".into(), "--".into(), other.clone()],
                    BTreeMap::new(),
                    vec![],
                    MAX_OUTPUT,
                )
                .await;
            if result.is_ok_and(|output| output.code == 0) {
                let mut removed = record.clone();
                removed.removed = true;
                self.save(other, &removed)?;
            }
        }
        let branch = self
            .run(
                &path,
                &["symbolic-ref", "-q", "--short", "HEAD"],
                vec![],
                MAX_OUTPUT,
            )
            .await?
            .text();
        Ok(
            json!({"repository":repository,"repo_path":path,"branch":branch,"default_branch":default}),
        )
    }
    pub async fn commit(&self, path: &Path, message: &str) -> Result<Commit> {
        let _lock = gb_base::files::FileLock::hold(
            &self.host_root.join("git.lock"),
            remaining(self.deadline)?,
        )
        .map_err(lock_error)?;
        self.assignment(path).await?;
        commit(self.guest, path, message, self.name, self.email, self.guard).await
    }
    pub async fn push(&self, path: &Path) -> Result<Value> {
        let _lock = gb_base::files::FileLock::hold(
            &self.host_root.join("git.lock"),
            remaining(self.deadline)?,
        )
        .map_err(lock_error)?;
        let assignment = self
            .assignment(path)
            .await?
            .ok_or_else(|| failure("unprepared"))?;
        self.code()?.check_push(&assignment.repository)?;
        let branch = self
            .run(
                path,
                &["symbolic-ref", "-q", "--short", "HEAD"],
                vec![],
                MAX_OUTPUT,
            )
            .await?;
        if branch.code != 0 {
            return Err(failure("detached"));
        }
        let branch = branch.text();
        let origin = self.origin(&assignment.repository).await?;
        origin.branch(&branch).await?;
        self.guard.ensure_current()?;
        origin.fetch(self.code()?.git_credential().await?).await?;
        let reference = format!("refs/heads/{branch}");
        let head = self
            .run(
                path,
                &["rev-parse", "--verify", &format!("{reference}^{{commit}}")],
                vec![],
                MAX_OUTPUT,
            )
            .await?;
        if head.code != 0 {
            return Err(branch_error(
                "capabilities.member_git.branch_empty",
                &branch,
            ));
        }
        let head = head.text();
        object(&head)?;
        let known = origin.refs(&[]).await?;
        let mut revisions = vec![reference.clone()];
        for sha in known.values() {
            if self
                .run(
                    path,
                    &["cat-file", "-e", &format!("{sha}^{{commit}}")],
                    vec![],
                    MAX_OUTPUT,
                )
                .await?
                .code
                == 0
            {
                revisions.push(format!("^{sha}"));
            }
        }
        if origin.receive(&branch, &head, None).await? {
            // The host already holds this exact object; no bundle is needed.
        } else {
            let bundle = self
                .run(
                    path,
                    &["bundle", "create", "-q", "-", "--stdin"],
                    (revisions.join("\n") + "\n").into_bytes(),
                    MAX_BUNDLE,
                )
                .await?
                .checked_bundle()?;
            if !origin.receive(&branch, &head, Some(&bundle.stdout)).await? {
                return Err(failure("bundle"));
            }
        }
        let listed = origin
            .git(
                &[
                    "log",
                    "--format=%H%x00%s",
                    &reference,
                    "--not",
                    "--remotes=origin",
                    "--",
                ],
                vec![],
            )
            .await?
            .text();
        let code = self.code()?;
        let commits=listed.lines().filter_map(|l|l.split_once('\0')).map(|(sha,subject)|json!({"id":sha,"message":subject,"url":code.commit_url(&assignment.repository,sha)})).collect::<Vec<_>>();
        let pushed = origin
            .push(&branch, self.code()?.git_credential().await?)
            .await?;
        origin.remember(vec![head.clone()]).await?;
        if pushed {
            self.git(
                path,
                &[
                    "update-ref",
                    &format!("refs/remotes/origin/{branch}"),
                    &head,
                ],
                vec![],
            )
            .await?;
        }
        let mut result = json!({"repo_path":path,"branch":branch,"pushed":pushed,"status":if pushed{"pushed"}else{"up_to_date"},"commits":commits,"pull_requests":[]});
        match crate::repository::open_pull_request_readiness(
            self.code()?,
            &assignment.repository,
            &branch,
        )
        .await
        {
            Ok(prs) => {
                result["pull_requests"] =
                    serde_json::to_value(prs).map_err(|_| failure("bundle"))?
            }
            Err(error) => result["pull_requests_error"] = (self.render_error)(&error).into(),
        }
        Ok(result)
    }
    pub async fn publish(&self, path: &Path, message: &str) -> Result<Value> {
        let committed = self.commit(path, message).await?;
        let mut pushed = self.push(path).await?;
        pushed["commit_sha"] = committed.commit_sha.clone().into();
        pushed["has_changes"] = committed.has_changes.into();
        pushed["status"] = if committed.commit_sha.is_some() || pushed["pushed"] == true {
            "published"
        } else {
            "up_to_date"
        }
        .into();
        Ok(pushed)
    }
}

pub async fn show(
    root: &Path,
    code: &dyn CodeHostingService,
    repository: &str,
    paths: &[String],
    deadline: Instant,
) -> Result<Value> {
    show_default(root, code, repository, paths, deadline)
        .await
        .map_err(|error| match error {
            MemberError::Capability("capabilities.member_git.operation_timeout") => {
                MemberError::Capability("capabilities.member_git.show_timeout")
            }
            other => other,
        })
}
async fn show_default(
    root: &Path,
    code: &dyn CodeHostingService,
    repository: &str,
    paths: &[String],
    deadline: Instant,
) -> Result<Value> {
    repository_path(repository)?;
    if paths.is_empty()
        || serde_json::to_vec(&(repository, paths))
            .map_err(|_| failure("path"))?
            .len()
            + 128 * paths.len()
            > gb_ports::MAX_PAGE_BYTES / 4
    {
        return Err(MemberError::Capability(
            "capabilities.member_git.show_request_large",
        ));
    }
    for path in paths {
        if path.is_empty()
            || path.starts_with('/')
            || path.contains(['\\', '\0'])
            || path.split('/').any(|p| p == "..")
        {
            return Err(failure("path"));
        }
    }
    let branch = code.default_branch(repository).await?;
    let credential = code.git_credential().await?;
    let _lock = gb_base::files::FileLock::hold(&root.join("git.lock"), remaining(deadline)?)
        .map_err(lock_error)?;
    let origin = Origin::open(
        root,
        repository,
        code.clone_url(repository).await?,
        deadline,
    )
    .await?;
    origin.branch(&branch).await?;
    origin.fetch(credential).await?;
    let commit = origin
        .git(
            &[
                "rev-parse",
                "--verify",
                &format!("refs/remotes/origin/{branch}^{{commit}}"),
            ],
            vec![],
        )
        .await?
        .text();
    object(&commit)?;
    let mut result =
        json!({"repository":repository,"default_branch":branch,"commit":commit,"items":[]});
    let mut used = serde_json::to_vec(&result).unwrap().len();
    for path in paths {
        let normalized = Path::new(path)
            .components()
            .filter_map(|v| match v {
                std::path::Component::Normal(v) => Some(v.to_string_lossy().into_owned()),
                _ => None,
            })
            .collect::<Vec<_>>()
            .join("/");
        let mut item = origin
            .show_path(
                &commit,
                if normalized.is_empty() {
                    "."
                } else {
                    &normalized
                },
            )
            .await?;
        let size = serde_json::to_vec(&item).unwrap().len();
        if used + size > gb_ports::MAX_PAGE_BYTES * 3 / 4
            && (item.get("content").is_some() || item.get("entries").is_some())
        {
            item.as_object_mut().unwrap().remove("content");
            item.as_object_mut().unwrap().remove("entries");
            item["status"] = "too_large".into();
        }
        used += serde_json::to_vec(&item).unwrap().len() + 2;
        result["items"].as_array_mut().unwrap().push(item);
    }
    Ok(result)
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Commit {
    pub repo_path: PathBuf,
    pub branch: String,
    pub commit_sha: Option<String>,
    pub has_changes: bool,
    pub status: String,
}
pub async fn commit(
    guest: &dyn Guest,
    path: &Path,
    message: &str,
    name: &str,
    email: &str,
    _guard: &dyn WriteGuard,
) -> Result<Commit> {
    if message.trim().is_empty() {
        return Err(MemberError::Capability(
            "capabilities.member_git.empty_message",
        ));
    }
    let git = |mut args: Vec<String>, stdin: Vec<u8>| {
        args.insert(0, "git".into());
        guest.run(
            path,
            args,
            [
                ("GIT_AUTHOR_NAME".into(), name.into()),
                ("GIT_AUTHOR_EMAIL".into(), email.into()),
                ("GIT_COMMITTER_NAME".into(), name.into()),
                ("GIT_COMMITTER_EMAIL".into(), email.into()),
            ]
            .into(),
            stdin,
            MAX_OUTPUT,
        )
    };
    let branch = git(
        vec!["symbolic-ref".into(), "-q".into(), "HEAD".into()],
        vec![],
    )
    .await?;
    if branch.code != 0 {
        return Err(failure("detached"));
    }
    let branch = branch
        .text()
        .strip_prefix("refs/heads/")
        .ok_or_else(|| failure("detached"))?
        .to_owned();
    git(
        vec!["check-ref-format".into(), "--branch".into(), branch.clone()],
        vec![],
    )
    .await?
    .checked()
    .map_err(|_| branch_error("capabilities.member_git.invalid_branch", &branch))?;
    let staged = git(
        vec![
            "diff".into(),
            "--cached".into(),
            "--quiet".into(),
            "--exit-code".into(),
        ],
        vec![],
    )
    .await?;
    if !matches!(staged.code, 0 | 1) {
        return Err(failure("git_failed"));
    }
    let merge = git(
        vec![
            "rev-parse".into(),
            "-q".into(),
            "--verify".into(),
            "MERGE_HEAD".into(),
        ],
        vec![],
    )
    .await?;
    let changed = staged.code == 1 || merge.code == 0;
    let sha = if changed {
        let args = vec![
            "-c".into(),
            format!("user.name={name}"),
            "-c".into(),
            format!("user.email={email}"),
            "commit".into(),
            "--no-gpg-sign".into(),
            "--file=-".into(),
        ];
        git(args, format!("{}\n", message.trim()).into_bytes())
            .await?
            .checked()?;
        let value = git(
            vec!["rev-parse".into(), "--verify".into(), "HEAD".into()],
            vec![],
        )
        .await?
        .checked()?
        .text();
        object(&value)?;
        Some(value)
    } else {
        None
    };
    Ok(Commit {
        repo_path: path.into(),
        branch,
        commit_sha: sha,
        has_changes: changed,
        status: if changed {
            "committed"
        } else {
            "nothing_staged"
        }
        .into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn git(path: &Path, args: &[&str]) -> String {
        let result = std::process::Command::new("git")
            .args(args)
            .current_dir(path)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_AUTHOR_NAME", "Fixture")
            .env("GIT_AUTHOR_EMAIL", "fixture@example.invalid")
            .env("GIT_COMMITTER_NAME", "Fixture")
            .env("GIT_COMMITTER_EMAIL", "fixture@example.invalid")
            .output()
            .unwrap();
        assert!(
            result.status.success(),
            "{}",
            String::from_utf8_lossy(&result.stderr)
        );
        String::from_utf8(result.stdout).unwrap().trim().into()
    }
    fn seeded(root: &Path) -> PathBuf {
        let repository = root.join("upstream");
        std::fs::create_dir_all(&repository).unwrap();
        git(&repository, &["init", "-q", "-b", "main"]);
        std::fs::write(repository.join("readme"), "first").unwrap();
        std::fs::write(repository.join("binary"), b"a\0b").unwrap();
        std::fs::create_dir(repository.join("docs")).unwrap();
        std::fs::write(repository.join("docs/guide"), "guide").unwrap();
        git(&repository, &["add", "."]);
        git(&repository, &["commit", "-qm", "initial"]);
        repository
    }
    async fn origin(root: &Path, upstream: &Path) -> Origin {
        Origin::open(
            &root.join("host"),
            "owner/repo",
            upstream.to_string_lossy().into(),
            Instant::now() + Duration::from_secs(30),
        )
        .await
        .unwrap()
    }
    #[tokio::test]
    async fn completed_history_keeps_stale_client_refs_and_requires_available_bases() {
        let temp = tempfile::tempdir().unwrap();
        let upstream = seeded(temp.path());
        let base = git(&upstream, &["rev-parse", "HEAD"]);
        let client = temp.path().join("client");
        git(
            temp.path(),
            &[
                "clone",
                "-q",
                upstream.to_str().unwrap(),
                client.to_str().unwrap(),
            ],
        );
        git(&client, &["checkout", "-qb", "topic"]);
        std::fs::write(client.join("readme"), "local").unwrap();
        git(&client, &["commit", "-qam", "local"]);
        git(&client, &["tag", "-a", "local-tag", "-m", "tag"]);
        let topic = git(&client, &["rev-parse", "HEAD"]);
        let tag = git(&client, &["rev-parse", "local-tag"]);
        let delta = temp.path().join("delta.bundle");
        git(
            &client,
            &[
                "bundle",
                "create",
                delta.to_str().unwrap(),
                "topic",
                "local-tag",
                "^refs/remotes/origin/main",
            ],
        );
        let incoming = std::fs::read(&delta).unwrap();
        let refs = BTreeMap::from([
            ("refs/heads/topic".into(), topic.clone()),
            ("refs/remotes/origin/main".into(), base.clone()),
            ("refs/tags/local-tag".into(), tag),
        ]);
        // The server's code host advances while the client's tracking ref stays old.
        std::fs::write(upstream.join("readme"), "server").unwrap();
        git(&upstream, &["commit", "-qam", "server"]);
        let origin = origin(temp.path(), &upstream).await;
        assert!(
            origin
                .complete_history(&incoming, &[], &refs, "refs/heads/topic")
                .await
                .is_err()
        );
        origin.fetch(None).await.unwrap();
        for (bundle, selected, head) in [
            (incoming.as_slice(), refs.clone(), "refs/heads/topic"),
            (
                &[][..],
                BTreeMap::from([("refs/remotes/origin/main".into(), base.clone())]),
                base.as_str(),
            ),
        ] {
            let complete = origin
                .complete_history(bundle, &[], &selected, head)
                .await
                .unwrap();
            std::fs::write(&delta, complete).unwrap();
            let restored = tempfile::tempdir().unwrap();
            git(restored.path(), &["init", "--bare", "-q"]);
            git(
                restored.path(),
                &["bundle", "verify", delta.to_str().unwrap()],
            );
            git(
                restored.path(),
                &["bundle", "unbundle", delta.to_str().unwrap()],
            );
            for (name, oid) in &selected {
                git(restored.path(), &["update-ref", name, oid]);
            }
            assert_eq!(
                git(restored.path(), &["rev-parse", "refs/remotes/origin/main"]),
                base
            );
            assert_eq!(
                git(
                    restored.path(),
                    &["log", "--format=%H", "refs/remotes/origin/main"]
                ),
                git(&client, &["log", "--format=%H", "origin/main"])
            );
            if selected.len() > 1 {
                assert_eq!(
                    git(restored.path(), &["log", "--format=%H", "refs/heads/topic"]),
                    git(&client, &["log", "--format=%H", "topic"])
                );
            }
            assert!(
                !git(
                    restored.path(),
                    &["bundle", "list-heads", delta.to_str().unwrap()]
                )
                .contains(&git(&upstream, &["rev-parse", "HEAD"]))
            );
        }
    }
    #[tokio::test]
    async fn default_history_is_fresh_and_object_reads_preserve_kinds() {
        let temp = tempfile::tempdir().unwrap();
        let upstream = seeded(temp.path());
        let origin = origin(temp.path(), &upstream).await;
        origin.fetch(None).await.unwrap();
        let head = git(&upstream, &["rev-parse", "HEAD"]);
        assert_eq!(
            origin.show_path(&head, "readme").await.unwrap()["content"],
            "first"
        );
        assert_eq!(
            origin.show_path(&head, "binary").await.unwrap()["status"],
            "binary"
        );
        assert_eq!(
            origin.show_path(&head, "missing").await.unwrap()["kind"],
            "missing"
        );
        assert_eq!(
            origin.show_path(&head, "docs").await.unwrap()["entries"],
            json!([{"name":"guide","kind":"file"}])
        );
        std::fs::write(upstream.join("readme"), "second").unwrap();
        git(&upstream, &["commit", "-qam", "second"]);
        origin.fetch(None).await.unwrap();
        let next = origin
            .git(&["rev-parse", "refs/remotes/origin/main"], vec![])
            .await
            .unwrap()
            .text();
        assert_ne!(next, head);
        assert_eq!(
            origin.show_path(&next, "readme").await.unwrap()["content"],
            "second"
        );
        assert_eq!(
            origin.show_path(&head, "readme").await.unwrap()["content"],
            "first"
        );
        let remembered = origin.refs(&[PUBLIC]).await.unwrap();
        assert!(remembered.values().any(|v| v == &head));
    }
    #[test]
    fn git_diagnostics_keep_both_streams_and_distinguish_lock_timeout() {
        let error = Output {
            code: 1,
            stdout: b"branch rejected non-fast-forward".to_vec(),
            stderr: b"remote unavailable".to_vec(),
        }
        .checked_text(true)
        .err()
        .unwrap();
        let MemberError::CapabilityArgs { args, .. } = error else {
            panic!()
        };
        assert!(args["reason"].contains("non-fast-forward"));
        assert!(args["reason"].contains("remote unavailable"));
        assert_eq!(
            lock_error(std::io::Error::from(std::io::ErrorKind::TimedOut)).to_string(),
            "capabilities.member_git.lock_timeout"
        );
        assert_eq!(
            remaining(Instant::now() - Duration::from_secs(1))
                .unwrap_err()
                .to_string(),
            "capabilities.member_git.operation_timeout"
        );
    }
    #[tokio::test]
    async fn invalid_repositories_branches_and_object_names_are_refused() {
        let temp = tempfile::tempdir().unwrap();
        for repository in ["../x", "/x", "a//b", "a/../b", "a\\b", "a:b", ""] {
            assert!(
                Origin::open(
                    temp.path(),
                    repository,
                    "unused".into(),
                    Instant::now() + Duration::from_secs(10)
                )
                .await
                .is_err()
            );
        }
        let upstream = seeded(temp.path());
        let origin = origin(temp.path(), &upstream).await;
        for branch in ["-x", "HEAD", "a..b", "a:b", "refs/heads/../x", "a\nb"] {
            let error = origin.branch(branch).await.unwrap_err();
            let MemberError::CapabilityArgs { key, args } = error else {
                panic!("missing branch diagnostic")
            };
            assert_eq!(key, "capabilities.member_git.invalid_branch");
            assert_eq!(args["branch"], branch);
        }
        for sha in ["--all", "HEAD", "0000", &"A".repeat(40)] {
            assert!(object(sha).is_err());
        }
        assert!(object(&"a".repeat(40)).is_ok());
        assert!(object(&"b".repeat(64)).is_ok());
    }
    #[tokio::test]
    async fn redirected_destinations_are_refused_before_authentication() {
        let temp = tempfile::tempdir().unwrap();
        let upstream = seeded(temp.path());
        let origin = origin(temp.path(), &upstream).await;
        origin
            .git(
                &[
                    "config",
                    "remote.origin.pushurl",
                    "file:///must-not-connect",
                ],
                vec![],
            )
            .await
            .unwrap();
        let err = origin.fetch(None).await.unwrap_err();
        assert_eq!(err.to_string(), "capabilities.member_git.redirect");
    }
    #[tokio::test]
    async fn bundle_receive_verifies_prerequisites_head_and_only_imports_requested_branch() {
        let temp = tempfile::tempdir().unwrap();
        let upstream = seeded(temp.path());
        let origin = origin(temp.path(), &upstream).await;
        git(&upstream, &["branch", "topic"]);
        git(&upstream, &["tag", "extra"]);
        let head = git(&upstream, &["rev-parse", "HEAD"]);
        assert!(!origin.receive("topic", &head, None).await.unwrap());
        assert!(
            !origin
                .receive("topic", &head, Some(b"not a bundle"))
                .await
                .unwrap()
        );
        let bundle = temp.path().join("in.bundle");
        git(
            &upstream,
            &["bundle", "create", bundle.to_str().unwrap(), "--all"],
        );
        assert!(
            origin
                .receive("topic", &head, Some(&std::fs::read(bundle).unwrap()))
                .await
                .unwrap()
        );
        let refs = origin.refs(&[]).await.unwrap();
        assert_eq!(refs.len(), 1);
        assert_eq!(refs["refs/heads/topic"], head);
        assert!(origin.receive("topic", &head, None).await.unwrap());
    }
    #[tokio::test]
    async fn deadline_and_fetch_failures_do_not_become_missing_objects() {
        let temp = tempfile::tempdir().unwrap();
        let upstream = seeded(temp.path());
        let mut origin = origin(temp.path(), &upstream).await;
        origin.deadline = Instant::now() - Duration::from_secs(1);
        assert_eq!(
            origin.fetch(None).await.unwrap_err().to_string(),
            "capabilities.member_git.operation_timeout"
        );
        origin.deadline = Instant::now() + Duration::from_secs(10);
        std::fs::remove_dir_all(upstream).unwrap();
        assert!(origin.fetch(None).await.is_err());
    }
    struct LocalGuest;
    impl Guest for LocalGuest {
        fn run<'a>(
            &'a self,
            cwd: &'a Path,
            argv: Vec<String>,
            env: BTreeMap<String, String>,
            stdin: Vec<u8>,
            _: usize,
        ) -> Pending<'a, Output> {
            Box::pin(async move {
                let mut child = tokio::process::Command::new(&argv[0])
                    .args(&argv[1..])
                    .current_dir(cwd)
                    .envs(env)
                    .env("GIT_CONFIG_NOSYSTEM", "1")
                    .env("GIT_CONFIG_GLOBAL", "/dev/null")
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped())
                    .spawn()
                    .map_err(io_error)?;
                child
                    .stdin
                    .take()
                    .unwrap()
                    .write_all(&stdin)
                    .await
                    .map_err(io_error)?;
                let result = child.wait_with_output().await.map_err(io_error)?;
                Ok(Output {
                    code: result.status.code().unwrap_or(1),
                    stdout: result.stdout,
                    stderr: result.stderr,
                })
            })
        }
    }
    struct Current;
    impl WriteGuard for Current {
        fn ensure_current(&self) -> Result<()> {
            Ok(())
        }
    }
    #[tokio::test]
    async fn commit_is_local_staged_unsigned_and_keeps_identity_config() {
        let temp = tempfile::tempdir().unwrap();
        let upstream = seeded(temp.path());
        git(&upstream, &["config", "user.name", "Original"]);
        for key in ["author.name", "committer.name"] {
            git(&upstream, &["config", key, "Override"]);
        }
        for key in ["author.email", "committer.email"] {
            git(&upstream, &["config", key, "override@example.invalid"]);
        }
        git(&upstream, &["config", "commit.gpgsign", "true"]);
        let first = git(&upstream, &["rev-parse", "HEAD"]);
        let unchanged = commit(
            &LocalGuest,
            &upstream,
            "noop",
            "Member",
            "member@example.invalid",
            &Current,
        )
        .await
        .unwrap();
        assert!(!unchanged.has_changes);
        assert!(unchanged.commit_sha.is_none());
        assert_eq!(unchanged.status, "nothing_staged");
        std::fs::write(upstream.join("readme"), "staged").unwrap();
        git(&upstream, &["add", "readme"]);
        std::fs::write(upstream.join("unstaged"), "keep").unwrap();
        let result = commit(
            &LocalGuest,
            &upstream,
            "  message  ",
            "Member",
            "member@example.invalid",
            &Current,
        )
        .await
        .unwrap();
        assert!(result.has_changes);
        assert_ne!(result.commit_sha.unwrap(), first);
        assert_eq!(
            git(&upstream, &["show", "-s", "--format=%an <%ae>", "HEAD"]),
            "Member <member@example.invalid>"
        );
        assert_eq!(
            git(&upstream, &["show", "-s", "--format=%cn <%ce>", "HEAD"]),
            "Member <member@example.invalid>"
        );
        let raw = std::process::Command::new("git")
            .args(["cat-file", "commit", "HEAD"])
            .current_dir(&upstream)
            .output()
            .unwrap();
        assert!(raw.status.success());
        assert_eq!(
            String::from_utf8(raw.stdout)
                .unwrap()
                .split_once("\n\n")
                .unwrap()
                .1,
            "message\n"
        );
        assert_eq!(git(&upstream, &["config", "author.name"]), "Override");
        assert_eq!(git(&upstream, &["config", "user.name"]), "Original");
        assert!(git(&upstream, &["status", "--porcelain"]).contains("unstaged"));
        assert!(
            commit(&LocalGuest, &upstream, " ", "Member", "m@x", &Current)
                .await
                .is_err()
        );
    }
    #[tokio::test]
    async fn credentials_reset_helpers_append_command_config_and_remove_askpass_on_failure() {
        let temp = tempfile::tempdir().unwrap();
        let upstream = seeded(temp.path());
        let origin = origin(temp.path(), &upstream).await;
        let marker = temp.path().join("helper-ran");
        origin
            .git(
                &[
                    "config",
                    "credential.helper",
                    &format!("!touch {}", marker.display()),
                ],
                vec![],
            )
            .await
            .unwrap();
        let result = origin
            .authenticated(
                &["credential", "fill"],
                Some(GitCredential {
                    username: "fixture-user".into(),
                    password: "fixture-password".into(),
                }),
                b"protocol=https\nhost=fixture.invalid\n\n".to_vec(),
            )
            .await
            .unwrap();
        assert!(result.text().contains("username=fixture-user"));
        assert!(result.text().contains("password=fixture-password"));
        assert!(!marker.exists());
        // A failing process still drops its private temporary script.
        assert!(
            origin
                .authenticated(
                    &["not-a-command"],
                    Some(GitCredential {
                        username: "fixture-user".into(),
                        password: "fixture-password".into()
                    }),
                    vec![]
                )
                .await
                .is_err()
        );
        // Configuration unrelated to the checked remote leaves credentials scoped.
        origin
            .git(
                &[
                    "config",
                    "url.file:///unrelated.insteadOf",
                    "https://unrelated.invalid/",
                ],
                vec![],
            )
            .await
            .unwrap();
        origin.fetch(None).await.unwrap();
    }
    #[test]
    fn authentication_preserves_existing_configuration_and_cleans_its_script() {
        let authentication = Authentication::new(
            Some(GitCredential {
                username: "fixture-user".into(),
                password: "fixture-password".into(),
            }),
            "1",
        )
        .unwrap();
        assert_eq!(authentication.env["GIT_CONFIG_COUNT"], "2");
        assert_eq!(authentication.env["GIT_CONFIG_KEY_1"], "credential.helper");
        assert_eq!(authentication.env["GIT_CONFIG_VALUE_1"], "");
        assert!(!authentication.env.contains_key("GIT_CONFIG_KEY_0"));
        let script = PathBuf::from(&authentication.env["GIT_ASKPASS"]);
        let content = std::fs::read_to_string(&script).unwrap();
        assert!(!content.contains("fixture-user"));
        assert!(!content.contains("fixture-password"));
        drop(authentication);
        assert!(!script.parent().unwrap().exists());
        assert!(Authentication::new(None, "invalid").is_err());
    }
    #[test]
    fn checkout_and_work_ids_are_distinct_bounded_and_stable() {
        let mut ids = vec![];
        for repository in ["owner/repo", "fork/repo"] {
            for slot in 0..3 {
                ids.push(digest((repository, slot)));
            }
        }
        for kind in ["ticket", "chat", "manual"] {
            for identity in ["a:b", "a/b", "é", "e\u{301}", &"x".repeat(1000)] {
                ids.push(digest((kind, identity)));
            }
        }
        assert_eq!(
            ids.iter().collect::<std::collections::BTreeSet<_>>().len(),
            ids.len()
        );
        assert!(ids.iter().all(|v| {
            v.len() == 64
                && v.bytes()
                    .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        }));
    }
    #[test]
    fn binary_bundle_failures_never_enter_diagnostics_and_text_is_bounded() {
        for stderr in [vec![], b"bundle failed".to_vec()] {
            let error = Output {
                code: 1,
                stdout: vec![255; 1024 * 1024],
                stderr,
            }
            .checked_bundle()
            .err()
            .unwrap();
            let MemberError::CapabilityArgs { args, .. } = error else {
                panic!()
            };
            assert!(!args["reason"].contains('\u{fffd}'));
            assert!(args["reason"].len() <= 4096);
        }
        let output = || Output {
            code: 1,
            stdout: b"[rejected] topic".to_vec(),
            stderr: b"push failed".to_vec(),
        };
        let MemberError::CapabilityArgs { args, .. } = output().checked().err().unwrap() else {
            panic!()
        };
        assert_eq!(args["reason"], "push failed");
        let MemberError::CapabilityArgs { args, .. } = output().checked_text(true).err().unwrap()
        else {
            panic!()
        };
        assert_eq!(args["reason"], "[rejected] topic\npush failed");
        let MemberError::CapabilityArgs { args, .. } = (Output {
            code: 1,
            stdout: vec![b'x'; 5000],
            stderr: vec![],
        })
        .checked_text(true)
        .err()
        .unwrap() else {
            panic!()
        };
        assert_eq!(args["reason"], "");
    }
}
