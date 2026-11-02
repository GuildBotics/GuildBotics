//! Executors: where a job's commands run. The one implementation is
//! microsandbox (one microVM per job), driven through the pinned `msb` CLI.
//! There is no host executor: when the executor is unusable, jobs conclude
//! `startup_failure`.

use std::future::Future;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use tokio::io::AsyncReadExt;
use tokio::sync::{mpsc, watch};

use crate::ci::pipeline::JobDef;

/// The `msb` version this runner accepts (the CLI flags and behaviour it was
/// verified against).
pub const MSB_VERSION: &str = "0.6.17";

/// How long pulling or loading an image may take (a GiB-sized archive).
pub const IMAGE_TRANSFER: Duration = Duration::from_secs(1800);

/// An image of the cache, as `msb image inspect` reports it.
#[derive(Clone, Debug, serde::Serialize)]
pub struct ImageInfo {
    pub reference: String,
    /// The manifest digest, which `docker save` and `msb load` re-encode.
    pub digest: String,
    /// The configuration digest, which survives saving and loading: it
    /// tells which version of a tag a job ran on.
    pub config_digest: String,
    pub size_bytes: u64,
    pub created_at: String,
}

#[derive(serde::Deserialize)]
struct Inspected {
    reference: String,
    digest: String,
    config: InspectedConfig,
    #[serde(default)]
    size_bytes: u64,
    #[serde(default)]
    created_at: String,
}

#[derive(serde::Deserialize)]
struct InspectedConfig {
    digest: String,
}

impl From<Inspected> for ImageInfo {
    fn from(i: Inspected) -> Self {
        Self {
            reference: i.reference,
            digest: i.digest,
            config_digest: i.config.digest,
            size_bytes: i.size_bytes,
            created_at: i.created_at,
        }
    }
}

#[derive(serde::Deserialize)]
struct Listed {
    reference: String,
}

#[derive(serde::Deserialize)]
struct Volume {
    name: String,
}

#[derive(Clone, Debug)]
pub struct Health {
    pub ready: bool,
    pub detail: Option<String>,
}

/// One job, with the CPUs, memory, and time limit the runner gives it.
pub struct JobRun<'a> {
    pub attempt_id: &'a str,
    /// Run by its `image`, the tag `Executor::resolve_image` checked.
    pub job: &'a JobDef,
    /// The lock that came with the image's check, held until the VM has
    /// picked the image.
    pub image_lock: ImageLock,
    pub cpus: u32,
    pub memory_mib: u64,
    pub timeout_secs: u64,
    /// The host directory mounted at `/work`: a checkout of the commit.
    pub work: &'a Path,
    /// An empty host directory of this job only, mounted at [`LOG_MOUNT`]:
    /// the job's output is written there, so the runner reads it while the
    /// job runs and keeps it when the job is stopped.
    pub log_dir: &'a Path,
    pub network: bool,
    /// Environment of the job's commands (no secrets: CI metadata only).
    pub env: Vec<(String, String)>,
}

#[derive(Debug, PartialEq)]
pub enum Outcome {
    /// The commands ran; the exit code of the first failing one (or 0).
    Exited(i32),
    TimedOut,
    Cancelled,
    /// The commands never started (the reason).
    StartupFailure(String),
}

/// A lock of the image cache ([`Microsandbox::lock_images`]), released when
/// dropped (closing the file).
#[derive(Default)]
pub struct ImageLock {
    _file: Option<std::fs::File>,
}

pub trait Executor: Send + Sync {
    fn name(&self) -> &'static str;
    fn health(&self) -> impl Future<Output = Health> + Send;
    /// The configuration digest of the image the cache holds under the
    /// job's `image`, or `None` when it holds none (telling `log` when it
    /// waits for a pull or a load). The lock that comes with the answer keeps
    /// the cache from re-pointing the tag until `run` has started the job, so
    /// that the job runs the image of that digest.
    fn resolve_image(
        &self,
        image: &str,
        log: &mpsc::Sender<Vec<u8>>,
    ) -> impl Future<Output = Result<(Option<String>, ImageLock), String>> + Send;
    /// Runs the job; output goes to `log`, `cancel` turning true stops it.
    fn run(
        &self,
        job: JobRun<'_>,
        log: mpsc::Sender<Vec<u8>>,
        cancel: watch::Receiver<bool>,
    ) -> impl Future<Output = Outcome> + Send;
    /// Removes leftovers of earlier runs of this runner.
    fn sweep(&self) -> impl Future<Output = ()> + Send;
}

// ---------------------------------------------------------------- microsandbox

/// Where an accepted `msb` lives, or why none was accepted.
#[derive(Clone, Debug)]
pub enum Msb {
    Found(PathBuf),
    Missing(String),
}

fn home_dir() -> Option<PathBuf> {
    directories::BaseDirs::new().map(|b| b.home_dir().to_path_buf())
}

/// `~/.branvil` (short on every OS, outside OS temp directories).
pub fn branvil_home() -> PathBuf {
    home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".branvil")
}

/// The candidates in order: the configured binary, GuildBotics' deployed
/// copy, Branvil's own copy. The first that reports exactly the pinned
/// version wins; nothing is ever downloaded.
pub fn resolve_msb(configured: Option<&Path>) -> Msb {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(c) = configured {
        candidates.push(c.to_path_buf());
    }
    if let Some(home) = home_dir() {
        candidates.push(home.join(".guildbotics/data/msb/bin").join(msb_file()));
    }
    candidates.push(branvil_home().join("msb/bin").join(msb_file()));
    let mut notes = Vec::new();
    for c in candidates {
        if !c.is_file() {
            notes.push(format!("{}: not found", c.display()));
            continue;
        }
        match std::process::Command::new(&c)
            .arg("--version")
            .stdin(Stdio::null())
            .output()
        {
            Ok(out) => {
                let v = String::from_utf8_lossy(&out.stdout).trim().to_string();
                if v == format!("msb {MSB_VERSION}") {
                    return Msb::Found(c);
                }
                notes.push(format!(
                    "{}: reports '{v}', not msb {MSB_VERSION}",
                    c.display()
                ));
            }
            Err(e) => notes.push(format!("{}: cannot run ({e})", c.display())),
        }
    }
    Msb::Missing(format!(
        "No usable msb {MSB_VERSION} found ({}). Install GuildBotics' runtime or run `branvil runner install-msb --from <msb>`.",
        notes.join("; ")
    ))
}

fn msb_file() -> &'static str {
    if cfg!(windows) { "msb.exe" } else { "msb" }
}

pub struct Microsandbox {
    msb: Msb,
    /// Branvil's own `MSB_HOME` (image cache, sandbox records).
    home: PathBuf,
    /// Label value that marks this runner's sandboxes.
    runner: String,
    health: tokio::sync::Mutex<Option<(Instant, Health)>>,
}

impl Microsandbox {
    pub fn new(msb: Msb, home: PathBuf, runner: &str) -> Self {
        Self {
            msb,
            home,
            runner: runner.to_string(),
            health: tokio::sync::Mutex::new(None),
        }
    }

    pub fn msb(&self) -> &Msb {
        &self.msb
    }

    pub fn home(&self) -> &Path {
        &self.home
    }

    /// An `msb` command with Branvil's `MSB_HOME`, an empty Docker
    /// configuration (no host registry credentials), and only the host
    /// environment variables the runtime needs.
    pub fn command(&self) -> Option<tokio::process::Command> {
        let Msb::Found(path) = &self.msb else {
            return None;
        };
        let docker = self.home.join("docker-config-empty");
        let _ = std::fs::create_dir_all(&docker);
        let mut cmd = tokio::process::Command::new(path);
        cmd.env_clear();
        for key in [
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "TMPDIR",
            "LANG",
            "SystemRoot",
            "USERPROFILE",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
        ] {
            if let Some(v) = std::env::var_os(key) {
                cmd.env(key, v);
            }
        }
        cmd.env("MSB_HOME", &self.home)
            .env("DOCKER_CONFIG", &docker)
            .stdin(Stdio::null())
            .kill_on_drop(true);
        Some(cmd)
    }

    async fn output(&self, args: &[&str]) -> Result<std::process::Output, String> {
        self.output_within(args, Duration::from_secs(120)).await
    }

    async fn output_within(
        &self,
        args: &[&str],
        limit: Duration,
    ) -> Result<std::process::Output, String> {
        let mut cmd = self.command().ok_or_else(|| self.missing())?;
        cmd.args(args);
        tokio::time::timeout(limit, cmd.output())
            .await
            .map_err(|_| format!("msb {} timed out", args.first().unwrap_or(&"")))?
            .map_err(|e| format!("cannot run msb: {e}"))
    }

    /// The cached image `reference`, or `None` when the cache has none.
    pub async fn inspect(&self, reference: &str) -> Result<Option<ImageInfo>, String> {
        let out = self
            .output(&["image", "inspect", "--format", "json", reference])
            .await?;
        if !out.status.success() {
            return Ok(None);
        }
        serde_json::from_slice::<Inspected>(&out.stdout)
            .map(|i| Some(i.into()))
            .map_err(|e| format!("msb image inspect answered something unreadable: {e}"))
    }

    /// Every image of the cache.
    pub async fn images(&self) -> Result<Vec<ImageInfo>, String> {
        let out = self.output(&["image", "list", "--format", "json"]).await?;
        if !out.status.success() {
            return Err(format!(
                "msb image list failed: {}",
                tail(&out.stderr, 1024)
            ));
        }
        let listed: Vec<Listed> = serde_json::from_slice(&out.stdout)
            .map_err(|e| format!("msb image list answered something unreadable: {e}"))?;
        let mut images = Vec::new();
        // The list leaves out the configuration digest, which tells the
        // versions of a tag apart.
        for l in listed {
            if let Some(i) = self.inspect(&l.reference).await? {
                images.push(i);
            }
        }
        Ok(images)
    }

    /// Locks the image cache. The lock is a file of `MSB_HOME`, so that it
    /// holds across the processes sharing the cache (the service, its
    /// runner, `branvil runner pull`). Pulls and loads hold it exclusively; a
    /// job holds it shared from checking its image until its VM has started.
    /// Waiting polls (`waiting` is called once when it starts), so that it
    /// ends with the future: a cancelled job or a stopping service leaves no
    /// thread blocked on the lock.
    pub async fn lock_images(
        &self,
        exclusive: bool,
        waiting: impl FnOnce(),
    ) -> Result<ImageLock, String> {
        let fail = |e: std::io::Error| format!("cannot lock the image cache: {e}");
        std::fs::create_dir_all(&self.home).map_err(fail)?;
        let file = std::fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(self.home.join("branvil-images.lock"))
            .map_err(fail)?;
        let mut waiting = Some(waiting);
        loop {
            let locked = if exclusive {
                file.try_lock()
            } else {
                file.try_lock_shared()
            };
            match locked {
                Ok(()) => return Ok(ImageLock { _file: Some(file) }),
                Err(std::fs::TryLockError::WouldBlock) => {
                    if let Some(w) = waiting.take() {
                        w();
                    }
                    tokio::time::sleep(Duration::from_millis(200)).await;
                }
                Err(std::fs::TryLockError::Error(e)) => return Err(fail(e)),
            }
        }
    }

    /// Pulls `reference` into the cache; the caller holds
    /// `lock_images(true)`.
    pub async fn pull(&self, reference: &str) -> Result<(), String> {
        let _ = std::fs::create_dir_all(&self.home);
        self.finish(
            "pull",
            self.output_within(&["pull", "--quiet", reference], IMAGE_TRANSFER)
                .await?,
        )
    }

    /// Loads a `docker save` / OCI archive into the cache; the caller holds
    /// `lock_images(true)`.
    pub async fn load(&self, archive: &Path) -> Result<(), String> {
        let _ = std::fs::create_dir_all(&self.home);
        let path = archive.to_string_lossy();
        self.finish(
            "load",
            self.output_within(&["load", "--quiet", "--input", &path], IMAGE_TRANSFER)
                .await?,
        )
    }

    fn finish(&self, what: &str, out: std::process::Output) -> Result<(), String> {
        if out.status.success() {
            return Ok(());
        }
        Err(format!(
            "msb {what} failed: {}{}",
            tail(&out.stdout, 1024),
            tail(&out.stderr, 1024)
        ))
    }

    fn missing(&self) -> String {
        match &self.msb {
            Msb::Missing(m) => m.clone(),
            Msb::Found(_) => String::new(),
        }
    }

    fn sandbox_name(id: &str) -> String {
        format!("bv-job-{}", id.to_lowercase())
    }

    /// The volume of a job's `/tmp`. Volumes carry no labels, so the name
    /// tells this runner's apart (`sweep`).
    fn tmp_volume(&self, id: &str) -> String {
        format!("bv-tmp-{}-{id}", self.runner)
    }

    /// Why jobs cannot run here, if they cannot: no accepted msb, no Git to
    /// check the source out with, or what `msb doctor` reports.
    async fn problem(&self) -> Option<String> {
        if let Msb::Missing(m) = &self.msb {
            return Some(m.clone());
        }
        let git = tokio::process::Command::new("git")
            .arg("--version")
            .stdin(Stdio::null())
            .output()
            .await;
        if !git.is_ok_and(|out| out.status.success()) {
            return Some(
                "git is not on the runner's PATH (the runner checks the source out with it)".into(),
            );
        }
        if let Err(e) = std::fs::create_dir_all(&self.home) {
            return Some(format!("cannot create {}: {e}", self.home.display()));
        }
        match self.output(&["doctor"]).await {
            Ok(out) if out.status.success() => None,
            Ok(out) => Some(format!(
                "msb doctor reports a problem: {}{}",
                tail(&out.stdout, 1024),
                tail(&out.stderr, 1024)
            )),
            Err(e) => Some(e),
        }
    }

    /// Removes the `/tmp` volumes whose names `pick` accepts, warning when
    /// they cannot be listed or removed.
    async fn remove_volumes(&self, pick: impl Fn(&str) -> bool) {
        let listed = match self.output(&["volume", "list", "--format", "json"]).await {
            Ok(out) if out.status.success() => serde_json::from_slice::<Vec<Volume>>(&out.stdout)
                .map_err(|e| format!("msb answered something unreadable: {e}")),
            Ok(out) => Err(tail(&out.stderr, 1024)),
            Err(e) => Err(e),
        };
        let names: Vec<String> = match listed {
            Ok(volumes) => volumes
                .into_iter()
                .map(|v| v.name)
                .filter(|n| pick(n))
                .collect(),
            Err(e) => {
                tracing::warn!("listing /tmp volumes failed: {e}");
                return;
            }
        };
        if !names.is_empty() {
            let mut args = vec!["volume", "remove", "-q"];
            args.extend(names.iter().map(String::as_str));
            self.clean("/tmp volumes", &args).await;
        }
    }

    /// Runs a clean-up command, warning when it fails.
    async fn clean(&self, what: &str, args: &[&str]) {
        match self.output(args).await {
            Ok(out) if out.status.success() => {}
            Ok(out) => tracing::warn!(
                "removing {what} failed: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            ),
            Err(e) => tracing::warn!("removing {what} failed: {e}"),
        }
    }
}

/// Where a job's log directory is mounted in the VM.
pub const LOG_MOUNT: &str = "/.branvil-log";

/// The uid and gid of the fixed non-root user a job's commands run as. The
/// work and log directories are presented to the VM as this user's.
pub const JOB_USER_ID: u32 = 1000;

/// The size of the disk a job's `/tmp` is on: the size of the root disk msb
/// gives a VM by default, which Branvil keeps.
const TMP_DISK: &str = "4G";

/// The shell script a job runs: its output into the log file, `HOME` moved
/// to `/tmp/home` when the user cannot write the one the image gives it (or
/// it has none), a start marker, then each command in its own `sh -c`,
/// stopping at the first failure with its exit code.
///
/// The output goes to a file of the log mount, not to `msb run`'s stdout:
/// msb 0.6.17 hands a command's stdout and stderr over only when the command
/// exits, so a running job's log would stay empty and a stopped job's output
/// (a timeout, a cancellation) would be lost.
pub fn script(commands: &[String]) -> String {
    let mut s = format!(
        "exec >>{LOG_MOUNT}/out 2>&1\n[ -w \"$HOME\" ] || {{ export HOME=/tmp/home; mkdir -p \"$HOME\"; }}\nprintf '\\036branvil:started\\n'\n"
    );
    for c in commands {
        let q = c.replace('\'', "'\\''");
        s.push_str(&format!(
            "printf '\\n$ %s\\n' '{q}'\n/bin/sh -c '{q}' || exit $?\n"
        ));
    }
    s
}

const MARKER: &[u8] = b"\x1ebranvil:started\n";

/// A job's log file as the runner reads it: the start marker is taken out
/// (the startup time is noted in its place); everything else goes to the log.
struct JobLog<'a> {
    started_at: Instant,
    /// When the commands started (the marker arrived).
    started: Option<Instant>,
    pending: Vec<u8>,
    log: &'a mpsc::Sender<Vec<u8>>,
}

impl JobLog<'_> {
    /// Reads what was appended to `file` since the last read, at most 1 MiB
    /// at a time so that a job writing fast cannot hold off its timeout.
    async fn read_from(&mut self, file: &mut tokio::fs::File) {
        let mut buf = vec![0u8; 64 << 10];
        for _ in 0..16 {
            match file.read(&mut buf).await {
                Ok(0) | Err(_) => return,
                Ok(n) => self.feed(&buf[..n]).await,
            }
        }
    }

    async fn feed(&mut self, bytes: &[u8]) {
        if self.started.is_some() {
            let _ = self.log.send(bytes.to_vec()).await;
            return;
        }
        self.pending.extend_from_slice(bytes);
        if let Some(pos) = self.pending.windows(MARKER.len()).position(|w| w == MARKER) {
            self.started = Some(Instant::now());
            if pos > 0 {
                let _ = self.log.send(self.pending[..pos].to_vec()).await;
            }
            let note = format!(
                "[branvil] sandbox started in {} ms\n",
                self.started_at.elapsed().as_millis()
            );
            let _ = self.log.send(note.into_bytes()).await;
            let _ = self
                .log
                .send(self.pending[pos + MARKER.len()..].to_vec())
                .await;
            self.pending.clear();
        } else if self.pending.len() > 4096 {
            let keep = self.pending.split_off(self.pending.len() - MARKER.len());
            let _ = self
                .log
                .send(std::mem::replace(&mut self.pending, keep))
                .await;
        }
    }

    async fn flush(&mut self) {
        if !self.pending.is_empty() {
            let _ = self.log.send(std::mem::take(&mut self.pending)).await;
        }
    }
}

impl Executor for Microsandbox {
    fn name(&self) -> &'static str {
        "microsandbox"
    }

    async fn health(&self) -> Health {
        let mut cached = self.health.lock().await;
        if let Some((at, h)) = cached.as_ref()
            && at.elapsed() < Duration::from_secs(if h.ready { 300 } else { 30 })
        {
            return h.clone();
        }
        let problem = self.problem().await;
        let h = Health {
            ready: problem.is_none(),
            detail: problem,
        };
        *cached = Some((Instant::now(), h.clone()));
        h
    }

    async fn resolve_image(
        &self,
        image: &str,
        log: &mpsc::Sender<Vec<u8>>,
    ) -> Result<(Option<String>, ImageLock), String> {
        let lock = self
            .lock_images(false, || {
                let note = "[branvil] waiting for an image pull or load to finish\n";
                let _ = log.try_send(note.as_bytes().to_vec());
            })
            .await?;
        match self.inspect(image).await? {
            Some(i) if !crate::ci::pipeline::valid_digest(&i.config_digest) => Err(format!(
                "msb image inspect reports '{}' as the configuration digest of {image}, not sha256:<64 hex>",
                i.config_digest
            )),
            held => Ok((held.map(|i| i.config_digest), lock)),
        }
    }

    async fn run(
        &self,
        job: JobRun<'_>,
        log: mpsc::Sender<Vec<u8>>,
        mut cancel: watch::Receiver<bool>,
    ) -> Outcome {
        let Some(mut cmd) = self.command() else {
            return Outcome::StartupFailure(self.missing());
        };
        let work = job.work.to_string_lossy().to_string();
        let log_dir = job.log_dir.to_string_lossy().to_string();
        if let Some(dir) = [&work, &log_dir]
            .into_iter()
            .find(|d| d.contains(':') || d.contains(','))
        {
            return Outcome::StartupFailure(format!(
                "the work directory {dir} contains ':' or ',', which msb mount specifications cannot carry"
            ));
        }
        // Created here, before the VM exists, and read through this handle
        // only: whatever the job later puts at that path (a link, another
        // file) is never opened on the host.
        let mut output = match tokio::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(job.log_dir.join("out"))
            .await
        {
            Ok(f) => f,
            Err(e) => return Outcome::StartupFailure(format!("cannot create the job log: {e}")),
        };
        let name = Self::sandbox_name(job.attempt_id);
        let tmp = self.tmp_volume(job.attempt_id);
        let owner = format!("uid={JOB_USER_ID},gid={JOB_USER_ID}");
        let j = job.job;
        cmd.args(["run", "--name", &name, "--quiet", "--pull", "never"])
            .args(["--label", &format!("branvil.runner={}", self.runner)])
            .args(["--label", &format!("branvil.job={}", job.attempt_id)])
            .args(["--cpus", &job.cpus.to_string()])
            .args(["--memory", &format!("{}M", job.memory_mib)])
            .args(["--user", &format!("{JOB_USER_ID}:{JOB_USER_ID}")])
            .args(["--mount-dir", &format!("{work}:/work:{owner}")])
            .args(["--mount-dir", &format!("{log_dir}:{LOG_MOUNT}:{owner}")])
            // On a disk, not in the VM's memory (msb's default /tmp).
            .args([
                "--mount-named",
                &format!("{tmp}:/tmp:kind=disk,size={TMP_DISK}"),
            ])
            .args(["--workdir", "/work"])
            // Backstop: the VM itself ends shortly after the job's timeout.
            .args(["--max-duration", &format!("{}s", job.timeout_secs + 60)]);
        if job.network {
            // Admin-approved: public destinations only (never the host or
            // private networks). Hostname rules are best effort.
            cmd.args(["--net", "public"]);
        } else {
            cmd.arg("--no-net");
        }
        for (k, v) in &job.env {
            cmd.args(["--env", &format!("{k}={v}")]);
        }
        cmd.arg(&j.image)
            .args(["--", "/bin/sh", "-c", &script(&j.commands)])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut job_log = JobLog {
            started_at: Instant::now(),
            started: None,
            pending: Vec::new(),
            log: &log,
        };
        let mut image_lock = Some(job.image_lock);
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => return Outcome::StartupFailure(format!("cannot start msb: {e}")),
        };
        let mut stdout = child.stdout.take().expect("piped");
        let mut stderr = child.stderr.take().expect("piped");
        let (tx_err, mut rx_err) = mpsc::channel::<Vec<u8>>(64);
        let err_reader = tokio::spawn(async move {
            let mut buf = vec![0u8; 16 << 10];
            while let Ok(n) = stderr.read(&mut buf).await {
                if n == 0 || tx_err.send(buf[..n].to_vec()).await.is_err() {
                    break;
                }
            }
        });
        let mut stderr_tail: Vec<u8> = Vec::new();
        let mut out_buf = vec![0u8; 16 << 10];
        let deadline = tokio::time::sleep(Duration::from_secs(job.timeout_secs));
        tokio::pin!(deadline);
        let mut tail = tokio::time::interval(Duration::from_millis(50));
        // The host memory of `msb run` and its VM, sampled every second:
        // the VM's grows with what the guest touches and does not shrink,
        // so the last samples hold the peak.
        let mut memory = tokio::time::interval(Duration::from_secs(1));
        let pid = child.id();
        let mut peak_kib = 0;
        let mut out_open = true;
        let mut err_open = true;
        let mut stop: Option<Outcome> = None;
        while out_open || err_open {
            tokio::select! {
                _ = tail.tick() => {
                    job_log.read_from(&mut output).await;
                    // The commands run in the VM: msb has picked the image.
                    if job_log.started.is_some() {
                        drop(image_lock.take());
                    }
                }
                _ = memory.tick() => {
                    if let Some(kib) = tree_rss_kib(pid).await {
                        peak_kib = peak_kib.max(kib);
                    }
                }
                // msb's own words (the job's output is in the log file).
                n = stdout.read(&mut out_buf), if out_open => match n {
                    Ok(0) | Err(_) => out_open = false,
                    Ok(n) => {
                        let _ = log.send(out_buf[..n].to_vec()).await;
                    }
                },
                e = rx_err.recv(), if err_open => match e {
                    Some(bytes) => {
                        stderr_tail.extend_from_slice(&bytes);
                        if stderr_tail.len() > 4096 {
                            stderr_tail.drain(..stderr_tail.len() - 4096);
                        }
                        let _ = log.send(bytes).await;
                    }
                    None => err_open = false,
                },
                _ = &mut deadline => {
                    stop = Some(Outcome::TimedOut);
                    break;
                }
                _ = cancel.changed() => {
                    if *cancel.borrow() {
                        stop = Some(Outcome::Cancelled);
                        break;
                    }
                }
            }
        }
        // Started or not, the VM is past picking an image.
        drop(image_lock);
        let outcome = match stop {
            Some(o) => {
                // Killing `msb run` stops the VM (its watchdog watches the
                // parent), and every process inside it with it.
                let _ = child.kill().await;
                o
            }
            None => match child.wait().await {
                Ok(status) => {
                    // Everything the job wrote is in the file once it ended.
                    job_log.read_from(&mut output).await;
                    if job_log.started.is_some() {
                        Outcome::Exited(status.code().unwrap_or(-1))
                    } else {
                        Outcome::StartupFailure(format!(
                            "msb run ended ({status}) before the commands started: {}",
                            String::from_utf8_lossy(&stderr_tail).trim()
                        ))
                    }
                }
                Err(e) => Outcome::StartupFailure(format!("waiting for msb failed: {e}")),
            },
        };
        // What the job wrote up to the moment it was stopped.
        job_log.read_from(&mut output).await;
        job_log.flush().await;
        err_reader.abort();
        if let Some(at) = job_log.started {
            let note = format!(
                "[branvil] commands ran for {} ms\n",
                at.elapsed().as_millis()
            );
            let _ = log.send(note.into_bytes()).await;
        }
        if peak_kib > 0 {
            let note = format!(
                "[branvil] peak memory of msb run and its VM: {} MiB\n",
                peak_kib.div_ceil(1024)
            );
            let _ = log.send(note.into_bytes()).await;
        }
        let removing = Instant::now();
        // By label and from the list: one that never came to be is not an
        // error.
        let label = format!("branvil.job={}", job.attempt_id);
        self.clean(
            "the job's sandbox",
            &["remove", "--force", "-q", "--label", &label],
        )
        .await;
        // The volume outlives its sandbox (msb refuses to remove it while a
        // running sandbox has it).
        self.remove_volumes(|n| n == tmp).await;
        let note = format!(
            "[branvil] sandbox removed in {} ms\n",
            removing.elapsed().as_millis()
        );
        let _ = log.send(note.into_bytes()).await;
        outcome
    }

    async fn sweep(&self) {
        if !matches!(self.msb, Msb::Found(_)) {
            return;
        }
        let label = format!("branvil.runner={}", self.runner);
        self.clean(
            "leftover sandboxes",
            &["remove", "--force", "-q", "--label", &label],
        )
        .await;
        let prefix = self.tmp_volume("");
        self.remove_volumes(|n| {
            n.strip_prefix(&prefix)
                .is_some_and(crate::ci::files::valid_attempt_id)
        })
        .await;
    }
}

/// The resident memory of process `pid` and all its descendants, in KiB,
/// from `ps` (none when it cannot tell).
async fn tree_rss_kib(pid: Option<u32>) -> Option<u64> {
    let out = tokio::process::Command::new("ps")
        .args(["-A", "-o", "pid=,ppid=,rss="])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .await
        .ok()?;
    out.status.success().then_some(())?;
    tree_rss(&String::from_utf8_lossy(&out.stdout), pid?)
}

/// Sums the `rss` of `root` and its descendants in `pid ppid rss` lines;
/// none when `root` is not listed.
fn tree_rss(ps: &str, root: u32) -> Option<u64> {
    let procs: Vec<[u64; 3]> = ps
        .lines()
        .filter_map(|l| {
            let mut f = l.split_whitespace().map(|v| v.parse::<u64>().ok());
            Some([f.next()??, f.next()??, f.next()??])
        })
        .collect();
    let mut tree = vec![u64::from(root)];
    let mut i = 0;
    while let Some(&p) = tree.get(i) {
        tree.extend(
            procs
                .iter()
                .filter(|q| q[1] == p && q[0] != p)
                .map(|q| q[0]),
        );
        i += 1;
    }
    let listed = procs.iter().any(|q| q[0] == u64::from(root));
    listed.then(|| {
        procs
            .iter()
            .filter(|q| tree.contains(&q[0]))
            .map(|q| q[2])
            .sum()
    })
}

fn tail(bytes: &[u8], n: usize) -> String {
    let start = bytes.len().saturating_sub(n);
    String::from_utf8_lossy(&bytes[start..]).trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_memory_of_a_job_is_its_process_tree() {
        let ps = "    1     0  9000\n   40     1   100\n   41    40  2000\n   42    41  3000\n   43     1   700\n  junk\n";
        assert_eq!(tree_rss(ps, 40), Some(5100));
        assert_eq!(tree_rss(ps, 42), Some(3000));
        assert_eq!(tree_rss(ps, 99), None);
    }

    #[test]
    fn the_script_quotes_every_command() {
        let s = script(&["echo 'a b'".into(), "exit 3".into()]);
        assert!(s.starts_with(
            "exec >>/.branvil-log/out 2>&1\n[ -w \"$HOME\" ] || { export HOME=/tmp/home; mkdir -p \"$HOME\"; }\nprintf '\\036branvil:started\\n'\n"
        ));
        assert!(s.contains("/bin/sh -c 'echo '\\''a b'\\''' || exit $?"));
        assert!(s.contains("/bin/sh -c 'exit 3' || exit $?"));
    }

    /// Writes an executable script. On Unix a child process writes it: a
    /// descriptor of it open for writing here could pass into a process that
    /// another test forks meanwhile, and running the script would then fail
    /// with "text file busy".
    fn write_script(path: &Path, body: &str) {
        #[cfg(unix)]
        {
            use std::io::Write;
            let mut child = std::process::Command::new("/bin/sh")
                .args(["-c", "cat > \"$0\" && chmod 755 \"$0\""])
                .arg(path)
                .stdin(Stdio::piped())
                .spawn()
                .unwrap();
            let mut stdin = child.stdin.take().unwrap();
            stdin.write_all(body.as_bytes()).unwrap();
            drop(stdin);
            assert!(child.wait().unwrap().success());
        }
        #[cfg(not(unix))]
        std::fs::write(path, body).unwrap();
    }

    /// An msb whose cache holds `img:dev` as the digest in `$MSB_HOME/fake/tag`:
    /// `load` writes the archive's content there, `run` notes its arguments
    /// and the digest it started, writes the start marker, and runs until
    /// `fake/done` exists. `volume list` answers `fake/volumes`; removals are
    /// noted in `fake/removed`.
    #[cfg(unix)]
    const FAKE_MSB: &str = r#"#!/bin/sh
state="$MSB_HOME/fake"
case "$1" in
image)
  [ "$5" = "img:dev" ] || exit 1
  printf '{"reference":"img:dev","digest":"sha256:m","config":{"digest":"%s"}}' "$(cat "$state/tag")" ;;
load)
  cp "$4" "$state/tag" ;;
run)
  printf '%s\n' "$@" > "$state/args"
  prev=""
  for a in "$@"; do
    [ "$prev" = "--mount-dir" ] && case "$a" in *:/.branvil-log:*) log="${a%%:/.branvil-log:*}" ;; esac
    prev="$a"
  done
  cat "$state/tag" > "$state/ran"
  printf '\036branvil:started\n' >> "$log/out"
  while [ ! -f "$state/done" ]; do sleep 0.05; done ;;
remove)
  echo "$*" >> "$state/removed" ;;
volume)
  case "$2" in
  list) cat "$state/volumes" ;;
  remove) echo "$*" >> "$state/removed" ;;
  esac ;;
esac
"#;

    /// msb runs an image by its tag, so a load that re-points the tag waits
    /// for a job that checked it until the job's VM has started (and not for
    /// the rest of the job): the job runs the image whose digest it checked.
    /// A job's check waits for a load.
    #[cfg(unix)]
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_load_waits_until_a_job_that_checked_the_tag_has_started() {
        let dir = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(dir.path()).unwrap();
        let msb = root.join("msb");
        write_script(&msb, FAKE_MSB);
        let home = root.join("home");
        let state = home.join("fake");
        std::fs::create_dir_all(&state).unwrap();
        let (a, b) = (
            format!("sha256:{}", "a".repeat(64)),
            format!("sha256:{}", "b".repeat(64)),
        );
        std::fs::write(state.join("tag"), &a).unwrap();
        // The job's /tmp volume, and another runner's.
        std::fs::write(
            state.join("volumes"),
            r#"[{"name": "bv-tmp-t-a1"}, {"name": "bv-tmp-u-a1"}]"#,
        )
        .unwrap();
        let archive = root.join("b.tar");
        std::fs::write(&archive, &b).unwrap();
        // The runner and the service: two users of one cache.
        let cache = |name: &str| Microsandbox::new(Msb::Found(msb.clone()), home.clone(), name);
        let (runner, service) = (cache("t"), cache("service"));

        let (log, mut logs) = mpsc::channel(64);
        let (digest, image_lock) = runner.resolve_image("img:dev", &log).await.unwrap();
        assert_eq!(digest, Some(a.clone()));
        let loading = tokio::spawn(async move {
            let _lock = service.lock_images(true, || {}).await?;
            service.load(&archive).await
        });
        tokio::time::sleep(Duration::from_millis(500)).await;
        assert!(!loading.is_finished(), "the load waits for the checked job");

        let (work, log_dir) = (root.join("work"), root.join("log"));
        std::fs::create_dir_all(&work).unwrap();
        std::fs::create_dir_all(&log_dir).unwrap();
        let job = JobDef {
            paths: Vec::new(),
            name: "j".into(),
            image: "img:dev".into(),
            commands: vec![],
            timeout_secs: None,
            artifacts: vec![],
            cache: None,
        };
        let (_cancel, cancel) = watch::channel(false);
        let running = tokio::spawn(async move {
            let job = JobRun {
                attempt_id: "a1",
                job: &job,
                image_lock,
                cpus: 1,
                memory_mib: 256,
                timeout_secs: 60,
                work: &work,
                log_dir: &log_dir,
                network: false,
                env: vec![],
            };
            runner.run(job, log, cancel).await
        });
        tokio::time::timeout(Duration::from_secs(10), loading)
            .await
            .expect("the load goes ahead once the VM has started")
            .unwrap()
            .unwrap();
        assert_eq!(std::fs::read_to_string(state.join("ran")).unwrap(), a);
        assert_eq!(std::fs::read_to_string(state.join("tag")).unwrap(), b);
        assert!(!running.is_finished(), "the job still runs");
        std::fs::write(state.join("done"), "").unwrap();
        assert_eq!(running.await.unwrap(), Outcome::Exited(0));
        // The log times the commands and the removal, and gives the peak
        // memory of `msb run` and what it started.
        let mut text = String::new();
        while let Ok(bytes) = logs.try_recv() {
            text.push_str(&String::from_utf8_lossy(&bytes));
        }
        for line in [
            "[branvil] sandbox started in ",
            "[branvil] commands ran for ",
            "[branvil] peak memory of msb run and its VM: ",
            "[branvil] sandbox removed in ",
        ] {
            assert!(text.contains(line), "{line} in {text}");
        }
        // The job ran as the fixed user, its directories presented as that
        // user's, its /tmp on a disk volume of its own; the volume is removed
        // after the sandbox.
        let args = std::fs::read_to_string(state.join("args")).unwrap();
        let args: Vec<&str> = args.lines().collect();
        for (flag, value) in [
            ("--user", "1000:1000".to_string()),
            (
                "--mount-dir",
                format!("{}:/work:uid=1000,gid=1000", root.join("work").display()),
            ),
            (
                "--mount-dir",
                format!(
                    "{}:/.branvil-log:uid=1000,gid=1000",
                    root.join("log").display()
                ),
            ),
            ("--mount-named", "bv-tmp-t-a1:/tmp:kind=disk,size=4G".into()),
        ] {
            assert!(
                args.windows(2).any(|w| w == [flag, value.as_str()]),
                "{flag} {value} in {args:?}"
            );
        }
        assert_eq!(
            std::fs::read_to_string(state.join("removed")).unwrap(),
            "remove --force -q --label branvil.job=a1\nvolume remove -q bv-tmp-t-a1\n"
        );

        // While a load holds the cache, a job's check waits and says so; the
        // wait ends with its future (a cancelled job).
        let (runner, service) = (cache("t"), cache("service"));
        let load = service.lock_images(true, || {}).await.unwrap();
        let (log, mut logs) = mpsc::channel(64);
        let waited = tokio::time::timeout(
            Duration::from_millis(500),
            runner.resolve_image("img:dev", &log),
        )
        .await;
        assert!(waited.is_err(), "the check waits for the load");
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(5), logs.recv())
                .await
                .expect("the job's log tells it waits")
                .unwrap(),
            b"[branvil] waiting for an image pull or load to finish\n"
        );
        drop(load);
        let (digest, _lock) = runner.resolve_image("img:dev", &log).await.unwrap();
        assert_eq!(digest, Some(b));
        let (digest, _lock) = runner.resolve_image("img:other", &log).await.unwrap();
        assert_eq!(digest, None, "an image the cache does not hold");
        // An answer without a readable configuration digest is an error, not
        // an image to run.
        std::fs::write(state.join("tag"), "sha256:b").unwrap();
        let err = runner.resolve_image("img:dev", &log).await.err().unwrap();
        assert!(
            err.contains("'sha256:b' as the configuration digest of img:dev"),
            "{err}"
        );
    }

    /// The sweep removes this runner's sandboxes by their label and its
    /// `/tmp` volumes by their names, and nothing else.
    #[cfg(unix)]
    #[tokio::test]
    async fn the_sweep_removes_only_this_runners_leftovers() {
        let dir = tempfile::tempdir().unwrap();
        let msb = dir.path().join("msb");
        write_script(&msb, FAKE_MSB);
        let home = dir.path().join("home");
        let state = home.join("fake");
        std::fs::create_dir_all(&state).unwrap();
        let id = format!("att_{}", "0".repeat(26));
        let volumes: Vec<serde_json::Value> = [
            format!("bv-tmp-t-{id}"),
            format!("bv-tmp-t2-{id}"),
            format!("bv-tmp-t-x-{id}"),
            "bv-tmp-t-att_short".into(),
            "data".into(),
        ]
        .into_iter()
        .map(|n| serde_json::json!({"name": n, "kind": "disk"}))
        .collect();
        std::fs::write(state.join("volumes"), serde_json::to_vec(&volumes).unwrap()).unwrap();
        Microsandbox::new(Msb::Found(msb), home, "t").sweep().await;
        assert_eq!(
            std::fs::read_to_string(state.join("removed")).unwrap(),
            format!("remove --force -q --label branvil.runner=t\nvolume remove -q bv-tmp-t-{id}\n")
        );
    }

    /// Without Git on `PATH` the executor is not ready, and says why (in a
    /// child test process whose `PATH` has no Git).
    #[cfg(unix)]
    #[tokio::test]
    async fn a_missing_git_is_reported() {
        if std::env::var_os("BRANVIL_TEST_CHILD").is_some() {
            let dir = PathBuf::from(std::env::var_os("BRANVIL_TEST_DIR").unwrap());
            let ms = Microsandbox::new(Msb::Found(dir.join("msb")), dir.join("home"), "t");
            let h = ms.health().await;
            assert!(!h.ready);
            assert_eq!(
                h.detail.as_deref(),
                Some("git is not on the runner's PATH (the runner checks the source out with it)")
            );
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        write_script(&dir.path().join("msb"), FAKE_MSB);
        let ms = Microsandbox::new(
            Msb::Found(dir.path().join("msb")),
            dir.path().join("home"),
            "t",
        );
        assert!(
            ms.health().await.ready,
            "ready with Git (and msb doctor passing)"
        );
        crate::runner::pass_in_child(
            &format!("{}::a_missing_git_is_reported", module_path!()),
            &[
                ("BRANVIL_TEST_DIR", dir.path().as_os_str()),
                ("PATH", dir.path().join("empty").as_os_str()),
            ],
        );
    }

    #[test]
    fn a_missing_msb_is_reported_not_replaced() {
        let dir = tempfile::tempdir().unwrap();
        let fake = dir.path().join("msb");
        write_script(&fake, "#!/bin/sh\necho 'msb 0.0.1'\n");
        // The configured candidate has the wrong version; whatever else is
        // found must report exactly the pinned version.
        match resolve_msb(Some(&fake)) {
            Msb::Found(p) => assert_ne!(p, fake),
            Msb::Missing(m) => assert!(m.contains("not msb 0.6.17") || cfg!(windows), "{m}"),
        }
    }
}
