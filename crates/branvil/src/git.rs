//! Git child processes. Every command is an argv array, runs with the
//! service's `GIT_*` variables removed and with an empty global configuration,
//! and is bounded in time, output, and concurrency. The service's run in their
//! own process group: a terminal's Ctrl-C, or a supervisor that kills the
//! service's group, never reaches Git in the middle of a ref update (killed
//! there, it leaves lock files that refuse every later update of the ref).
//! So it can outlive the service; the lock it inherits (`hold`) keeps the
//! next service from starting on its repositories until it has ended.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use tokio::io::AsyncReadExt;
use tokio::sync::Semaphore;

use crate::config::Layout;
use crate::error::{AppError, AppResult};

pub const ZERO_SHA: &str = "0000000000000000000000000000000000000000";

/// Largest stdout a service-side command may produce (diffs are the big ones).
const MAX_OUTPUT: usize = 64 << 20;

/// How long a command stopped at its time limit gets to remove its lock files
/// before it is killed.
const TERM_GRACE: Duration = Duration::from_secs(2);

/// The data directory's Git lock, once this process holds it as the service.
static HELD: std::sync::OnceLock<std::fs::File> = std::sync::OnceLock::new();

/// Takes the data directory's Git lock (`Layout::git_lock`) for the service,
/// before it reads or changes any repository. Every Git it starts from then
/// on holds it too, and so do that Git's own children (its hooks), so the lock
/// is free only once the service and all of them have ended: Git that a
/// stopped service left running (it was killed or crashed, or a push's
/// receive-pack or a detached gc outlived its stop) still updates its refs, and a next service that recovered first would
/// record the opposite of what happened. That service waits for them here.
/// On Windows the lock ends with the service.
pub async fn hold(layout: &Layout) -> anyhow::Result<()> {
    use anyhow::Context;
    let path = layout.git_lock();
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .with_context(|| format!("opening {}", path.display()))?;
    let mut told = false;
    // Polled rather than blocked on, so that the caller can give up waiting.
    loop {
        match file.try_lock() {
            Ok(()) => break,
            Err(std::fs::TryLockError::WouldBlock) => {
                if !std::mem::replace(&mut told, true) {
                    tracing::warn!(
                        "waiting for the Git a stopped service started to end (it holds {})",
                        path.display()
                    );
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            Err(std::fs::TryLockError::Error(e)) => {
                return Err(e).with_context(|| format!("locking {}", path.display()));
            }
        }
    }
    HELD.set(file)
        .map_err(|_| anyhow::anyhow!("the Git lock is held already"))
}

#[derive(Clone)]
pub struct Git {
    layout: Layout,
    permits: Arc<Semaphore>,
    timeout: Duration,
}

pub struct Output {
    pub status: i32,
    pub stdout: Vec<u8>,
    pub stderr: String,
    /// stdout was longer than the cap and was cut.
    pub truncated: bool,
}

/// The commit tested before a pull request lands, or the paths preventing it.
#[derive(Debug, PartialEq)]
pub enum GateCommit {
    Ready { sha: String, tree: String },
    Conflicts { files: Vec<String> },
}

impl Output {
    pub fn ok(&self) -> bool {
        self.status == 0
    }
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.stdout).into_owned()
    }
}

/// Builds a Git command with a sanitized environment and `global_config` (an
/// empty file) as its global configuration. No attributes or ignore rules of
/// the machine apply either: none from the system, and `XDG_CONFIG_HOME` is
/// that file, so no `$XDG_CONFIG_HOME/git/attributes` (or `ignore`) can exist
/// and `~/.config/git` is not read. Shared with Smart HTTP, the runner API,
/// and the runner.
pub fn base_command(global_config: &Path) -> tokio::process::Command {
    let mut cmd = tokio::process::Command::from(base_command_sync(global_config));
    cmd.kill_on_drop(true);
    #[cfg(unix)]
    cmd.process_group(0);
    #[cfg(unix)]
    if let Some(lock) = HELD.get() {
        use std::os::fd::AsRawFd;
        let fd = lock.as_raw_fd();
        // SAFETY: fcntl is async-signal-safe; this clears close-on-exec in the
        // child only.
        unsafe {
            cmd.pre_exec(move || {
                if libc::fcntl(fd, libc::F_SETFD, 0) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    // CREATE_NEW_PROCESS_GROUP: Ctrl-C is not delivered to it.
    #[cfg(windows)]
    cmd.creation_flags(0x0000_0200);
    cmd
}

/// The same isolated Git environment for offline synchronous upgrades.
pub fn base_command_sync(global_config: &Path) -> std::process::Command {
    let mut cmd = std::process::Command::new("git");
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            cmd.env_remove(&key);
        }
    }
    cmd.env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", global_config)
        .env("GIT_ATTR_NOSYSTEM", "1")
        .env("XDG_CONFIG_HOME", global_config)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("LC_ALL", "C")
        .stdin(Stdio::null());
    cmd
}

/// Removes the lock files a killed Git can leave in the repository `dir`:
/// its own (`HEAD.lock`, `packed-refs.lock`, `config.lock`), its refs' and
/// reflogs', and those of gc and maintenance (`objects/info`, its
/// `commit-graphs`, `objects/pack`; the rest of `objects` is not walked).
/// Only at startup, once no Git of a stopped service runs (`hold`): unix
/// only, where that Git holds the lock.
#[cfg(unix)]
pub fn remove_stale_locks(dir: &Path) -> std::io::Result<()> {
    fn remove(dir: &Path, deep: bool) -> std::io::Result<()> {
        let entries = match std::fs::read_dir(dir) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            entries => entries?,
        };
        for entry in entries {
            let entry = entry?;
            let path = entry.path();
            if entry.file_type()?.is_dir() {
                if deep {
                    remove(&path, true)?;
                }
            } else if path.extension().is_some_and(|e| e == "lock") {
                std::fs::remove_file(&path)?;
                tracing::warn!("removed {}, left by a Git that was killed", path.display());
            }
        }
        Ok(())
    }
    for (sub, deep) in [
        ("", false),
        ("refs", true),
        ("logs", true),
        ("objects/info", false),
        ("objects/info/commit-graphs", false),
        ("objects/pack", false),
    ] {
        remove(&dir.join(sub), deep)?;
    }
    Ok(())
}

impl Git {
    pub fn new(layout: Layout, concurrency: usize, timeout: Duration) -> Self {
        Self {
            layout,
            permits: Arc::new(Semaphore::new(concurrency.max(1))),
            timeout,
        }
    }

    pub fn layout(&self) -> &Layout {
        &self.layout
    }

    pub fn repo_dir(&self, path: &str) -> PathBuf {
        self.layout.repo_dir(path)
    }

    pub async fn run(&self, dir: Option<&Path>, args: &[&str]) -> AppResult<Output> {
        self.run_env(dir, args, &[]).await
    }

    /// `run` with extra environment variables for the child only (external
    /// sync passes its askpass settings this way, never through argv).
    pub async fn run_env(
        &self,
        dir: Option<&Path>,
        args: &[&str],
        envs: &[(&str, &str)],
    ) -> AppResult<Output> {
        let _permit = self
            .permits
            .acquire()
            .await
            .map_err(|_| AppError::unavailable("Git is shutting down."))?;
        let mut cmd = base_command(&self.layout.git_config());
        if let Some(dir) = dir {
            cmd.arg("--git-dir").arg(dir);
        }
        cmd.envs(envs.iter().copied());
        cmd.args(args).stdout(Stdio::piped()).stderr(Stdio::piped());
        let mut child = cmd
            .spawn()
            .map_err(|e| AppError::internal(format!("cannot start git: {e}")))?;
        let mut stdout = child.stdout.take().expect("piped stdout");
        let mut stderr = child.stderr.take().expect("piped stderr");
        let work = async {
            let mut out = Vec::new();
            let mut err = Vec::new();
            let mut out_reader = (&mut stdout).take(MAX_OUTPUT as u64 + 1);
            let mut err_reader = (&mut stderr).take(64 << 10);
            let (a, b) = tokio::join!(
                out_reader.read_to_end(&mut out),
                err_reader.read_to_end(&mut err)
            );
            a.and(b)?;
            let status = child.wait().await?;
            Ok::<_, std::io::Error>((status, out, err))
        };
        let Ok(done) = tokio::time::timeout(self.timeout, work).await else {
            // SIGTERM to its group (Git and the hooks it started) lets Git
            // remove its lock files; it is killed (on drop) only when it has
            // not ended a moment later.
            #[cfg(unix)]
            if let Some(pid) = child.id() {
                // SAFETY: our own child leads its process group (with the
                // hooks it started) and is not reaped yet.
                unsafe { libc::killpg(pid as libc::pid_t, libc::SIGTERM) };
                let _ = tokio::time::timeout(TERM_GRACE, child.wait()).await;
            }
            return Err(AppError::unavailable(format!(
                "git {} timed out.",
                args.first().unwrap_or(&"")
            )));
        };
        let (status, mut out, err) =
            done.map_err(|e| AppError::internal(format!("git i/o: {e}")))?;
        let truncated = out.len() > MAX_OUTPUT;
        out.truncate(MAX_OUTPUT);
        Ok(Output {
            status: status.code().unwrap_or(-1),
            stdout: out,
            stderr: String::from_utf8_lossy(&err).into_owned(),
            truncated,
        })
    }

    /// Runs a command that must succeed and returns its trimmed stdout.
    pub async fn run_ok(&self, dir: Option<&Path>, args: &[&str]) -> AppResult<String> {
        let out = self.run(dir, args).await?;
        if !out.ok() {
            return Err(AppError::internal(format!(
                "git {} failed: {}",
                args.join(" "),
                out.stderr.trim()
            )));
        }
        Ok(out.text().trim().to_string())
    }

    pub async fn version(&self) -> AppResult<String> {
        self.run_ok(None, &["version"]).await
    }

    /// Creates an empty bare repository owned by Branvil.
    pub async fn init_bare(&self, dir: &Path, default_branch: &str) -> AppResult<()> {
        if let Some(parent) = dir.parent() {
            std::fs::create_dir_all(parent).map_err(AppError::internal)?;
        }
        let dir_text = dir.to_string_lossy().to_string();
        let head = format!("--initial-branch={default_branch}");
        self.run_ok(None, &["init", "--bare", "--quiet", &head, &dir_text])
            .await?;
        self.configure(dir).await?;
        // Repository-provided hooks never run: the hooks path points at
        // Branvil's directory, so the sample hooks are dead weight.
        let _ = std::fs::remove_dir_all(dir.join("hooks"));
        Ok(())
    }

    /// (Re)applies the configuration every managed repository carries.
    pub async fn configure(&self, dir: &Path) -> AppResult<()> {
        let hooks = self.layout.hooks().to_string_lossy().to_string();
        // A stalled upstream host ends a fetch or push even when nothing
        // times it out any more (its service is gone: `hold`).
        let stalled = self.timeout.as_secs().to_string();
        for (key, value) in [
            ("core.hooksPath", hooks.as_str()),
            ("http.lowSpeedLimit", "1"),
            ("http.lowSpeedTime", stalled.as_str()),
            ("core.logAllRefUpdates", "always"),
            ("http.receivepack", "true"),
            ("http.uploadpack", "true"),
            ("receive.fsckObjects", "true"),
            ("receive.denyDeleteCurrent", "true"),
            ("transfer.hideRefs", "refs/branvil"),
            ("uploadpack.allowAnySHA1InWant", "false"),
        ] {
            self.run_ok(Some(dir), &["config", key, value]).await?;
        }
        Ok(())
    }

    pub async fn set_head(&self, dir: &Path, branch: &str) -> AppResult<()> {
        let target = format!("refs/heads/{branch}");
        self.run_ok(Some(dir), &["symbolic-ref", "HEAD", &target])
            .await
            .map(|_| ())
    }

    /// Every ref of the repository (including hidden ones) with its object.
    pub async fn all_refs(&self, dir: &Path) -> AppResult<BTreeMap<String, String>> {
        let text = self
            .run_ok(
                Some(dir),
                &["for-each-ref", "--format=%(objectname) %(refname)"],
            )
            .await?;
        Ok(text
            .lines()
            .filter_map(|l| l.split_once(' '))
            .map(|(sha, name)| (name.to_string(), sha.to_string()))
            .collect())
    }

    /// Branch name → tip of every branch.
    pub async fn branch_tips(&self, dir: &Path) -> AppResult<BTreeMap<String, String>> {
        Ok(self
            .all_refs(dir)
            .await?
            .into_iter()
            .filter_map(|(name, sha)| {
                name.strip_prefix("refs/heads/")
                    .map(|b| (b.to_string(), sha))
            })
            .collect())
    }

    pub async fn commit_exists(&self, dir: &Path, sha: &str) -> AppResult<bool> {
        let out = self
            .run(Some(dir), &["cat-file", "-e", &format!("{sha}^{{commit}}")])
            .await?;
        Ok(out.ok())
    }

    pub async fn tree_oid(&self, dir: &Path, commit: &str) -> AppResult<Option<String>> {
        let out = self
            .run(
                Some(dir),
                &["rev-parse", "--verify", "-q", &format!("{commit}^{{tree}}")],
            )
            .await?;
        Ok(out.ok().then(|| out.text().trim().to_string()))
    }

    /// `(sha, author name, author date, parents, message)` of a commit.
    pub async fn commit_info(
        &self,
        dir: &Path,
        sha: &str,
    ) -> AppResult<Option<(String, String, String, Vec<String>, String)>> {
        let spec = format!("{sha}^{{commit}}");
        let out = self
            .run(
                Some(dir),
                &[
                    "show",
                    "-s",
                    "--no-color",
                    "--format=%H%x00%an%x00%aI%x00%P%x00%B",
                    &spec,
                    "--",
                ],
            )
            .await?;
        if !out.ok() {
            return Ok(None);
        }
        let text = out.text();
        let mut parts = text.splitn(5, '\0');
        let mut next = || parts.next().unwrap_or("").to_string();
        let (sha, author, date, parents, message) = (next(), next(), next(), next(), next());
        let parents = parents.split_whitespace().map(str::to_string).collect();
        Ok(Some((
            sha,
            author,
            date,
            parents,
            message.trim_end().to_string(),
        )))
    }

    pub async fn merge_base(&self, dir: &Path, a: &str, b: &str) -> AppResult<Option<String>> {
        let out = self.run(Some(dir), &["merge-base", a, b]).await?;
        Ok(out.ok().then(|| out.text().trim().to_string()))
    }

    /// True when `ancestor` is reachable from `descendant`.
    pub async fn is_ancestor(
        &self,
        dir: &Path,
        ancestor: &str,
        descendant: &str,
    ) -> AppResult<bool> {
        let out = self
            .run(
                Some(dir),
                &["merge-base", "--is-ancestor", ancestor, descendant],
            )
            .await?;
        match out.status {
            0 => Ok(true),
            1 => Ok(false),
            _ => Err(AppError::internal(format!(
                "git merge-base: {}",
                out.stderr.trim()
            ))),
        }
    }

    /// Merges two commits without a worktree. A conflicted merge's tree is
    /// deliberately discarded; only its unambiguously NUL-delimited paths
    /// leave this method.
    pub async fn merge_tree(
        &self,
        dir: &Path,
        base: &str,
        head: &str,
    ) -> AppResult<Result<String, Vec<String>>> {
        let out = self
            .run(
                Some(dir),
                &[
                    "merge-tree",
                    "--write-tree",
                    "--name-only",
                    "--no-messages",
                    "-z",
                    base,
                    head,
                ],
            )
            .await?;
        if out.truncated || !matches!(out.status, 0 | 1) {
            return Err(AppError::internal(format!(
                "git merge-tree failed: {}",
                out.stderr.trim()
            )));
        }
        let mut parts = out.stdout.split(|b| *b == 0);
        let tree = String::from_utf8_lossy(parts.next().unwrap_or_default()).to_string();
        if out.ok() {
            return Ok(Ok(tree.trim().to_string()));
        }
        let mut files: Vec<String> = parts
            .take_while(|p| !p.is_empty())
            .map(|p| String::from_utf8_lossy(p).into_owned())
            .collect();
        files.sort();
        files.dedup();
        Ok(Err(files))
    }

    /// When allowed by the caller, a head containing the base needs no new commit. Otherwise,
    /// creates the merge result with base first in its parent list. Identity
    /// is explicit because service Git inherits no author configuration.
    pub async fn gate_commit(
        &self,
        dir: &Path,
        base: &str,
        head: &str,
        number: u64,
        reuse_head: bool,
    ) -> AppResult<GateCommit> {
        let contains_base = self.is_ancestor(dir, base, head).await?;
        if contains_base && reuse_head {
            let tree = self
                .run_ok(Some(dir), &["rev-parse", &format!("{head}^{{tree}}")])
                .await?;
            return Ok(GateCommit::Ready {
                sha: head.to_string(),
                tree,
            });
        }
        let tree = if contains_base {
            self.run_ok(Some(dir), &["rev-parse", &format!("{head}^{{tree}}")])
                .await?
        } else {
            match self.merge_tree(dir, base, head).await? {
                Ok(tree) => tree,
                Err(files) => return Ok(GateCommit::Conflicts { files }),
            }
        };
        let out = self
            .run_env(
                Some(dir),
                &[
                    "commit-tree",
                    &tree,
                    "-p",
                    base,
                    "-p",
                    head,
                    "-m",
                    &format!("Branvil landing gate !{number}"),
                ],
                &[
                    ("GIT_AUTHOR_NAME", "Branvil"),
                    ("GIT_AUTHOR_EMAIL", "branvil@localhost"),
                    ("GIT_COMMITTER_NAME", "Branvil"),
                    ("GIT_COMMITTER_EMAIL", "branvil@localhost"),
                ],
            )
            .await?;
        if !out.ok() {
            return Err(AppError::internal(format!(
                "git commit-tree failed: {}",
                out.stderr.trim()
            )));
        }
        Ok(GateCommit::Ready {
            sha: out.text().trim().to_string(),
            tree,
        })
    }

    /// Commits reachable from `from` and not from `not`.
    pub async fn count_between(&self, dir: &Path, not: &str, from: &str) -> AppResult<u64> {
        let range = format!("{not}..{from}");
        let text = self
            .run_ok(Some(dir), &["rev-list", "--count", &range])
            .await?;
        text.parse().map_err(AppError::internal)
    }

    /// The commits of `range` (`base..head`), oldest first. The committer date
    /// is in the stored UTC form, so it sorts with the service's own times.
    pub async fn commits(&self, dir: &Path, range: &str) -> AppResult<Vec<CommitSummary>> {
        let text = self
            .run_ok(
                Some(dir),
                &[
                    "log",
                    "--reverse",
                    "--no-color",
                    "--format=%H%x00%an%x00%cI%x00%s",
                    range,
                    "--",
                ],
            )
            .await?;
        Ok(text
            .lines()
            .filter_map(|line| {
                let mut parts = line.splitn(4, '\0');
                let mut next = || parts.next().map(str::to_string);
                Some(CommitSummary {
                    sha: next()?,
                    author: next()?,
                    committed_at: crate::util::parse_time(&next()?).ok()?,
                    subject: next()?,
                })
            })
            .collect())
    }

    /// Conditional ref update: moves `refname` to `new` only if it is `old`.
    /// Returns whether the ref is now at `new`. When Git fails, the ref tells
    /// what happened: Git may have stopped after the update; a moved ref, a
    /// stale lock file, or a lock a push holds all mean nothing was written.
    pub async fn update_ref(
        &self,
        dir: &Path,
        refname: &str,
        new: &str,
        old: &str,
        reason: &str,
    ) -> AppResult<bool> {
        let ran = self
            .run(Some(dir), &["update-ref", "-m", reason, refname, new, old])
            .await;
        if ran.is_ok_and(|out| out.ok()) {
            return Ok(true);
        }
        let at = self.all_refs(dir).await?.remove(refname);
        Ok(at.as_deref().unwrap_or(ZERO_SHA) == new)
    }

    /// Paths changed in `range` (`base...head` diffs from their merge base).
    pub async fn changed_paths(&self, dir: &Path, range: &str) -> AppResult<Vec<String>> {
        let out = self
            .run(
                Some(dir),
                &[
                    "diff",
                    "--no-color",
                    "--no-ext-diff",
                    "-M",
                    "--name-only",
                    "-z",
                    range,
                    "--",
                ],
            )
            .await?;
        if !out.ok() {
            return Err(AppError::internal(format!(
                "git diff: {}",
                out.stderr.trim()
            )));
        }
        Ok(out
            .stdout
            .split(|b| *b == 0)
            .filter(|s| !s.is_empty())
            .map(|s| String::from_utf8_lossy(s).into_owned())
            .collect())
    }

    /// The per-file diff of `range` (`base...head` diffs from the merge base).
    pub async fn diff_files(
        &self,
        dir: &Path,
        range: &str,
        patch_limit: usize,
    ) -> AppResult<Vec<FileChange>> {
        let common = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "-M"];
        let status_args: Vec<&str> = common
            .iter()
            .copied()
            .chain(["--name-status", "-z", range, "--"])
            .collect();
        let numstat_args: Vec<&str> = common
            .iter()
            .copied()
            .chain(["--numstat", "-z", range, "--"])
            .collect();
        let patch_args: Vec<&str> = common.iter().copied().chain([range, "--"]).collect();
        let status = self.run(Some(dir), &status_args).await?;
        let numstat = self.run(Some(dir), &numstat_args).await?;
        let patch = self.run(Some(dir), &patch_args).await?;
        for out in [&status, &numstat, &patch] {
            if !out.ok() {
                return Err(AppError::internal(format!(
                    "git diff: {}",
                    out.stderr.trim()
                )));
            }
        }
        let mut files = parse_name_status(&status.stdout);
        let stats = parse_numstat(&numstat.stdout);
        let chunks = split_patch(&patch.stdout);
        for (i, file) in files.iter_mut().enumerate() {
            match stats.get(i) {
                Some(Some((a, d))) => {
                    file.additions = *a;
                    file.deletions = *d;
                }
                Some(None) => file.binary = true,
                None => {}
            }
            if file.binary {
                file.patch = None;
                continue;
            }
            match chunks.get(i) {
                Some(chunk) => {
                    let text = hunks_of(chunk);
                    if text.len() > patch_limit {
                        let mut cut = patch_limit;
                        while !text.is_char_boundary(cut) {
                            cut -= 1;
                        }
                        file.patch = Some(text[..cut].to_string());
                        file.patch_complete = false;
                    } else {
                        file.patch = Some(text);
                    }
                }
                None => {
                    file.patch = Some(String::new());
                    file.patch_complete = !patch.truncated;
                }
            }
        }
        Ok(files)
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct CommitSummary {
    pub sha: String,
    pub author: String,
    pub committed_at: String,
    pub subject: String,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct FileChange {
    pub path: String,
    pub previous_path: Option<String>,
    pub status: &'static str,
    pub additions: u64,
    pub deletions: u64,
    pub patch: Option<String>,
    pub patch_complete: bool,
    #[serde(skip)]
    pub binary: bool,
}

fn parse_name_status(raw: &[u8]) -> Vec<FileChange> {
    let fields: Vec<String> = raw
        .split(|b| *b == 0)
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < fields.len() {
        let code = &fields[i];
        if code.is_empty() {
            i += 1;
            continue;
        }
        let kind = code.as_bytes()[0];
        let (status, previous, path, step) = match kind {
            b'R' | b'C' => (
                if kind == b'R' { "renamed" } else { "added" },
                fields.get(i + 1).cloned(),
                fields.get(i + 2).cloned().unwrap_or_default(),
                3,
            ),
            b'A' => (
                "added",
                None,
                fields.get(i + 1).cloned().unwrap_or_default(),
                2,
            ),
            b'D' => (
                "removed",
                None,
                fields.get(i + 1).cloned().unwrap_or_default(),
                2,
            ),
            _ => (
                "modified",
                None,
                fields.get(i + 1).cloned().unwrap_or_default(),
                2,
            ),
        };
        out.push(FileChange {
            path,
            previous_path: if kind == b'R' { previous } else { None },
            status,
            additions: 0,
            deletions: 0,
            patch: None,
            patch_complete: true,
            binary: false,
        });
        i += step;
    }
    out
}

/// `Some((added, deleted))` per file, `None` for binary files.
fn parse_numstat(raw: &[u8]) -> Vec<Option<(u64, u64)>> {
    let fields: Vec<&[u8]> = raw.split(|b| *b == 0).collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < fields.len() {
        let field = String::from_utf8_lossy(fields[i]);
        if field.is_empty() {
            i += 1;
            continue;
        }
        let mut parts = field.splitn(3, '\t');
        let add = parts.next().unwrap_or("-");
        let del = parts.next().unwrap_or("-");
        let path = parts.next().unwrap_or("");
        // A rename has an empty path here followed by the old and new paths.
        i += if path.is_empty() { 3 } else { 1 };
        out.push(match (add.parse(), del.parse()) {
            (Ok(a), Ok(d)) => Some((a, d)),
            _ => None,
        });
    }
    out
}

fn split_patch(raw: &[u8]) -> Vec<String> {
    let text = String::from_utf8_lossy(raw);
    let mut chunks: Vec<String> = Vec::new();
    for line in text.split_inclusive('\n') {
        if line.starts_with("diff --git ") {
            chunks.push(String::new());
        }
        if let Some(last) = chunks.last_mut() {
            last.push_str(line);
        }
    }
    chunks
}

/// The part of a file's diff from its first `@@` line.
fn hunks_of(chunk: &str) -> String {
    let mut offset = 0;
    for line in chunk.split_inclusive('\n') {
        if line.starts_with("@@") {
            return chunk[offset..].to_string();
        }
        offset += line.len();
    }
    String::new()
}

/// Conservative branch-name validation (a subset of `git check-ref-format`).
pub fn valid_branch(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 255
        && !name.starts_with('-')
        && !name.starts_with('/')
        && !name.ends_with('/')
        && !name.ends_with('.')
        && !name.ends_with(".lock")
        && !name.contains("..")
        && !name.contains("//")
        && !name.contains("@{")
        && name != "@"
        && !name.split('/').any(|p| p.starts_with('.'))
        && name
            .chars()
            .all(|c| !c.is_control() && !" ~^:?*[\\".contains(c))
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn commit_file(git: &Git, dir: &Path, file: &str, content: &str) -> String {
        std::fs::write(dir.join(file), content).unwrap();
        git.run_ok(None, &["-C", dir.to_str().unwrap(), "add", file])
            .await
            .unwrap();
        git.run_env(
            None,
            &["-C", dir.to_str().unwrap(), "commit", "-qm", file],
            &[
                ("GIT_AUTHOR_NAME", "Test"),
                ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
                ("GIT_COMMITTER_NAME", "Test"),
                ("GIT_COMMITTER_EMAIL", "test@example.invalid"),
            ],
        )
        .await
        .unwrap();
        git.run_ok(None, &["-C", dir.to_str().unwrap(), "rev-parse", "HEAD"])
            .await
            .unwrap()
    }

    /// `update_ref` goes by the ref, not by Git's exit status.
    #[cfg(unix)]
    #[tokio::test]
    async fn update_ref_tells_what_happened_by_the_ref() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&temp.path().join("service"));
        layout.create().unwrap();
        let git = Git::new(layout.clone(), 2, Duration::from_secs(10));
        let dir = temp.path().join("demo.git");
        git.init_bare(&dir, "main").await.unwrap();
        let mut blobs = Vec::new();
        for content in ["a", "b"] {
            let file = temp.path().join(content);
            std::fs::write(&file, content).unwrap();
            blobs.push(
                git.run_ok(Some(&dir), &["hash-object", "-w", file.to_str().unwrap()])
                    .await
                    .unwrap(),
            );
        }
        let (a, b, name) = (&blobs[0], &blobs[1], "refs/branvil/x");
        git.run_ok(Some(&dir), &["update-ref", name, a])
            .await
            .unwrap();

        // Git killed after it updated the ref: done.
        let hook = layout.hooks().join("reference-transaction");
        std::fs::write(
            &hook,
            "#!/bin/sh\ncat >/dev/null\n[ \"$1\" = committed ] && kill -9 $PPID\nexit 0\n",
        )
        .unwrap();
        std::fs::set_permissions(&hook, std::fs::Permissions::from_mode(0o755)).unwrap();
        assert!(git.update_ref(&dir, name, b, a, "test").await.unwrap());
        std::fs::remove_file(&hook).unwrap();

        // A lock file (left by a killed Git, or held by a push): nothing
        // written.
        let lock = dir.join("refs/branvil/x.lock");
        std::fs::write(&lock, "").unwrap();
        assert!(!git.update_ref(&dir, name, a, b, "test").await.unwrap());
        assert_eq!(git.all_refs(&dir).await.unwrap()[name], *b);
        std::fs::remove_file(&lock).unwrap();

        // The ref is elsewhere: nothing written.
        assert!(
            !git.update_ref(&dir, name, a, ZERO_SHA, "test")
                .await
                .unwrap()
        );
        assert_eq!(git.all_refs(&dir).await.unwrap()[name], *b);
    }

    /// A command stopped at its time limit inside a ref transaction (a hook
    /// holds it with the ref's lock taken) removes its lock file, and the
    /// hook stops with it.
    #[cfg(unix)]
    #[tokio::test]
    async fn a_command_stopped_at_its_time_limit_leaves_no_lock() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&temp.path().join("service"));
        layout.create().unwrap();
        // Long enough for Git to reach its hook on a loaded runner.
        let git = Git::new(layout.clone(), 2, Duration::from_secs(5));
        let dir = temp.path().join("demo.git");
        git.init_bare(&dir, "main").await.unwrap();
        let blob = git
            .run_ok(Some(&dir), &["hash-object", "-w", "--stdin"])
            .await
            .unwrap();
        let reached = temp.path().join("reached");
        let hook = layout.hooks().join("reference-transaction");
        // `reached` holds the hook's pid, which `exec` gives to the sleep.
        std::fs::write(
            &hook,
            format!(
                "#!/bin/sh\ncat >/dev/null\n[ \"$1\" = prepared ] || exit 0\necho $$ >'{0}.tmp'\nmv '{0}.tmp' '{0}'\nexec sleep 30\n",
                reached.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&hook, std::fs::Permissions::from_mode(0o755)).unwrap();
        let name = "refs/branvil/x";
        assert!(
            !git.update_ref(&dir, name, &blob, ZERO_SHA, "test")
                .await
                .unwrap()
        );
        let sleep: libc::pid_t = std::fs::read_to_string(&reached)
            .expect("Git reached the hook before its time limit")
            .trim()
            .parse()
            .unwrap();
        assert!(!dir.join("refs/branvil/x.lock").exists());
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        // Gone, or a zombie no one has reaped yet (as under a PID 1 that
        // does not reap).
        let running = || {
            let ps = std::process::Command::new("ps")
                .args(["-o", "stat=", "-p", &sleep.to_string()])
                .output()
                .unwrap();
            let stat = String::from_utf8_lossy(&ps.stdout);
            !stat.trim().is_empty() && !stat.trim_start().starts_with('Z')
        };
        while running() {
            assert!(
                std::time::Instant::now() < deadline,
                "the hook outlived Git"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    #[cfg(unix)]
    #[test]
    fn stale_locks_are_removed_where_git_leaves_them() {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path();
        let stale = [
            "HEAD.lock",
            "refs/heads/feature/x.lock",
            "logs/refs/heads/main.lock",
            "objects/info/commit-graphs/commit-graph-chain.lock",
            "objects/pack/pack-1.idx.lock",
        ];
        let kept = ["objects/ab/cdef.lock", "refs/heads/main"];
        for file in stale.iter().chain(&kept) {
            std::fs::create_dir_all(dir.join(file).parent().unwrap()).unwrap();
            std::fs::write(dir.join(file), "").unwrap();
        }
        remove_stale_locks(dir).unwrap();
        for file in stale {
            assert!(!dir.join(file).exists(), "{file}");
        }
        for file in kept {
            assert!(dir.join(file).exists(), "{file}");
        }
    }

    #[tokio::test]
    async fn gate_commits_preserve_heads_merge_parents_and_report_conflict_paths() {
        let temp = tempfile::tempdir().unwrap();
        let layout = Layout::new(&temp.path().join("service"));
        layout.create().unwrap();
        let git = Git::new(layout, 2, Duration::from_secs(10));
        let work = temp.path().join("work");
        git.run_ok(None, &["init", "-q", "-b", "main", work.to_str().unwrap()])
            .await
            .unwrap();
        let initial = commit_file(&git, &work, "conflicted file\n.txt", "initial\n").await;
        let dir = work.join(".git");
        let head = commit_file(&git, &work, "feature.txt", "feature\n").await;
        let GateCommit::Ready { sha, .. } = git
            .gate_commit(&dir, &initial, &head, 7, true)
            .await
            .unwrap()
        else {
            panic!("fast-forward head conflicts");
        };
        assert_eq!(sha, head, "an up-to-date head creates no commit");
        let GateCommit::Ready { sha: forced, tree } = git
            .gate_commit(&dir, &initial, &head, 7, false)
            .await
            .unwrap()
        else {
            panic!("forced gate conflicts")
        };
        assert_ne!(forced, head);
        assert_eq!(
            tree,
            git.run_ok(Some(&dir), &["rev-parse", &format!("{head}^{{tree}}")])
                .await
                .unwrap()
        );
        assert_eq!(
            git.commit_info(&dir, &forced).await.unwrap().unwrap().3,
            vec![initial.clone(), head.clone()]
        );
        git.run_ok(Some(&dir), &["update-ref", "refs/heads/main", &initial])
            .await
            .unwrap();
        git.run_ok(
            None,
            &["-C", work.to_str().unwrap(), "reset", "--hard", &initial],
        )
        .await
        .unwrap();
        let base = commit_file(&git, &work, "main.txt", "main\n").await;
        let GateCommit::Ready { sha, tree } =
            git.gate_commit(&dir, &base, &head, 7, true).await.unwrap()
        else {
            panic!("independent changes conflict");
        };
        assert_ne!(sha, head);
        let (_, author, _, parents, _) = git.commit_info(&dir, &sha).await.unwrap().unwrap();
        assert_eq!(author, "Branvil");
        assert_eq!(
            git.run_ok(Some(&dir), &["show", "-s", "--format=%s", &sha])
                .await
                .unwrap(),
            "Branvil landing gate !7"
        );
        assert_eq!(parents, vec![base.clone(), head]);
        let identities = git
            .run_ok(
                Some(&dir),
                &["show", "-s", "--format=%an <%ae>%n%cn <%ce>", &sha],
            )
            .await
            .unwrap();
        assert_eq!(
            identities,
            "Branvil <branvil@localhost>\nBranvil <branvil@localhost>"
        );
        for file in ["feature.txt", "main.txt"] {
            assert!(
                git.run(Some(&dir), &["cat-file", "-e", &format!("{tree}:{file}")])
                    .await
                    .unwrap()
                    .ok()
            );
        }
        let base = commit_file(&git, &work, "conflicted file\n.txt", "base\n").await;
        git.run_ok(
            None,
            &["-C", work.to_str().unwrap(), "reset", "--hard", &initial],
        )
        .await
        .unwrap();
        let head = commit_file(&git, &work, "conflicted file\n.txt", "head\n").await;
        assert_eq!(
            git.gate_commit(&dir, &base, &head, 7, true).await.unwrap(),
            GateCommit::Conflicts {
                files: vec!["conflicted file\n.txt".into()]
            }
        );
    }

    #[test]
    fn branch_names() {
        assert!(valid_branch("main"));
        assert!(valid_branch("ticket/3"));
        assert!(!valid_branch("a..b"));
        assert!(!valid_branch("-x"));
        assert!(!valid_branch("a b"));
        assert!(!valid_branch("x.lock"));
        assert!(!valid_branch("a/.hidden"));
        assert!(!valid_branch(""));
    }

    #[test]
    fn numstat_handles_renames_and_binaries() {
        let raw = b"1\t2\ta.txt\0-\t-\tbin.png\0".to_vec();
        let mut raw = raw;
        raw.extend_from_slice(b"0\t0\t\0old.txt\0new.txt\0");
        assert_eq!(parse_numstat(&raw), vec![Some((1, 2)), None, Some((0, 0))]);
        let status = b"M\0a.txt\0A\0bin.png\0R100\0old.txt\0new.txt\0";
        let files = parse_name_status(status);
        assert_eq!(files.len(), 3);
        assert_eq!(files[2].status, "renamed");
        assert_eq!(files[2].previous_path.as_deref(), Some("old.txt"));
        assert_eq!(files[2].path, "new.txt");
    }

    #[test]
    fn hunks_start_at_the_first_at_line() {
        let chunk = "diff --git a/x b/x\nindex 1..2\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n";
        assert_eq!(hunks_of(chunk), "@@ -1 +1 @@\n-a\n+b\n");
        assert_eq!(hunks_of("diff --git a/x b/x\nrename from x\n"), "");
    }
}
