//! Executors: where a job's commands run. The one implementation is
//! microsandbox (one microVM per job), driven through the pinned `msb` CLI.
//! There is no host executor: when the executor is unusable, jobs conclude
//! `startup_failure`.

#[cfg(feature = "host")]
mod sdk;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use tokio::io::AsyncReadExt;
use tokio::sync::{mpsc, watch};

use crate::ci::pipeline::JobDef;

static HOST_RUNTIME: std::sync::OnceLock<(PathBuf, Option<PathBuf>)> = std::sync::OnceLock::new();

/// An embedding application's runtime default. The runner's own cache home
/// stays independent, and an explicit CI setting still takes precedence.
pub fn set_runtime_binary(path: PathBuf, firmware: Option<PathBuf>) -> Result<(), String> {
    if !path.is_absolute() || firmware.as_ref().is_some_and(|path| !path.is_absolute()) {
        return Err("The runtime binary must have an absolute path".into());
    }
    let runtime = (path, firmware);
    if let Some(current) = HOST_RUNTIME.get() {
        return if current == &runtime {
            Ok(())
        } else {
            Err("The runtime binary was already configured".into())
        };
    }
    HOST_RUNTIME
        .set(runtime)
        .map_err(|_| "The runtime binary was already configured".into())
}

/// The `msb` version this runner accepts (the CLI flags and behaviour it was
/// verified against).
pub use gb_vm::RUNTIME_VERSION as MSB_VERSION;

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
    digest: String,
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
    pub peak_memory_mib: &'a mut Option<u64>,
    /// Run by its `image`, the tag `Executor::resolve_image` checked.
    pub job: &'a JobDef,
    /// The lock that came with the image's check, held until the VM has
    /// picked the image.
    pub image_lock: ImageLock,
    pub cpus: u32,
    pub memory_mib: u64,
    /// What the job may write to `/work` beyond what it holds at the start.
    pub work_quota_mib: u64,
    pub timeout_secs: u64,
    /// A host directory of this job only that carries archives between the
    /// host and `/work`, a disk of the job's own: it holds [`CHECKOUT`] and
    /// receives [`ARTIFACTS`]. Never mounted where the job's commands run.
    pub io: &'a Path,
    /// A host path of this job only that no VM mounts: the disk image of a
    /// cache entry, when there is one, which `/work` starts from; later, a
    /// link to the new cache image when the job saves one.
    pub cache_disk: &'a Path,
    /// What the commit tracks, as its index records it.
    pub tree: &'a super::workspace::Tree,
    /// The bytes of data the file system of the cache entry's image holds.
    pub cache_data: Option<u64>,
    /// Whether the restored generation matched the requested key. Reusing
    /// it skips saving only if preparation actually made it into `/work`.
    pub exact_cache: bool,
    /// Set to the bytes of data the file system of the image left at
    /// `cache_disk` holds, as the VM that kept the cached paths measured
    /// them, once there is one.
    pub kept_data: &'a mut Option<u64>,
    /// Whether to keep the job's cached paths for the cache.
    pub save_cache: bool,
    /// The host directory of the checkout; once the job ended, it holds
    /// what the job left at its artifacts' paths.
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

pub struct Inventory {
    pub images: Vec<crate::ci::images::Image>,
    pub detail: Option<String>,
}

pub trait Executor: Send + Sync {
    fn name(&self) -> &'static str;
    fn machine(&self) -> Option<crate::ci::api::Machine> {
        None
    }
    fn inventory(
        &self,
        api: &super::Api,
    ) -> impl Future<Output = Result<Inventory, super::ApiError>> + Send;
    fn synchronize(
        &self,
        _api: &super::Api,
    ) -> impl Future<Output = Result<(), super::ApiError>> + Send {
        async { Ok(()) }
    }
    /// Whether runtime resolution failed and should be retried before a claim.
    /// Health failures of an accepted runtime keep its existing cache.
    fn needs_resolution(&self) -> bool {
        false
    }
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

/// The candidates in order: the configured binary, the embedding host's
/// default, Branvil's own copy. The first that reports exactly the pinned
/// version wins; nothing is ever downloaded.
pub fn resolve_msb(configured: Option<&Path>) -> Msb {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(c) = configured {
        candidates.push(c.to_path_buf());
    }
    if let Some((path, _)) = HOST_RUNTIME.get() {
        candidates.push(path.clone());
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
        "No usable msb {MSB_VERSION} found ({}). Install GuildBotics' runtime or run `guildbotics runner install-msb --from <msb>`.",
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
    started: std::sync::Mutex<std::collections::BTreeSet<String>>,
    #[cfg(feature = "host")]
    sdk: bool,
}

impl Microsandbox {
    pub fn new(msb: Msb, home: PathBuf, runner: &str) -> Self {
        Self {
            msb,
            home,
            runner: runner.to_string(),
            health: tokio::sync::Mutex::new(None),
            started: std::sync::Mutex::new(Default::default()),
            #[cfg(feature = "host")]
            sdk: false,
        }
    }

    #[cfg(feature = "host")]
    pub fn hub(home: PathBuf, runner: &str) -> Self {
        let msb = match gb_vm::ensure(&home) {
            Ok(()) => Msb::Found(gb_vm::placed_files(&home)[0].clone()),
            Err(error) => Msb::Missing(error.to_string()),
        };
        Self {
            sdk: true,
            ..Self::new(msb, home, runner)
        }
    }

    fn uses_sdk(&self) -> bool {
        #[cfg(feature = "host")]
        return self.sdk;
        #[cfg(not(feature = "host"))]
        false
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
        if let Some((_, Some(firmware))) = HOST_RUNTIME.get().filter(|(binary, _)| binary == path) {
            cmd.env("MSB_LIBKRUNFW_PATH", firmware);
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
        self.inspect_status(reference, false).await
    }

    async fn inspect_status(
        &self,
        reference: &str,
        listed: bool,
    ) -> Result<Option<ImageInfo>, String> {
        #[cfg(feature = "host")]
        if self.sdk {
            let found = self.sdk_inspect(reference).await?;
            if listed && found.is_none() {
                return Err(format!("Cached image {reference} was removed"));
            }
            return Ok(found);
        }
        let out = self
            .output(&["image", "inspect", "--format", "json", reference])
            .await?;
        if !out.status.success() {
            if listed {
                return Err(format!(
                    "msb image inspect failed: {}{}",
                    tail(&out.stdout, 1024),
                    tail(&out.stderr, 1024)
                ));
            }
            return Ok(None);
        }
        serde_json::from_slice::<Inspected>(&out.stdout)
            .map(|i| Some(i.into()))
            .map_err(|e| format!("msb image inspect answered something unreadable: {e}"))
    }

    /// Every reference of the cache.
    async fn listed(&self) -> Result<Vec<Listed>, String> {
        #[cfg(feature = "host")]
        if self.sdk {
            return self.sdk_listed().await;
        }
        let out = self.output(&["image", "list", "--format", "json"]).await?;
        if !out.status.success() {
            return Err(format!(
                "msb image list failed: {}",
                tail(&out.stderr, 1024)
            ));
        }
        serde_json::from_slice(&out.stdout)
            .map_err(|e| format!("msb image list answered something unreadable: {e}"))
    }

    /// Every image of the cache a job can name: by its tag, never by a
    /// version's pin ([`Self::pin`]).
    pub async fn images(&self) -> Result<Vec<ImageInfo>, String> {
        let mut images = Vec::new();
        // The list leaves out the configuration digest, which tells the
        // versions of a tag apart.
        for l in self.listed().await? {
            if !pinned(&l.reference)
                && let Some(i) = self.inspect(&l.reference).await?
            {
                images.push(i);
            }
        }
        Ok(images)
    }

    /// Names the version the tag `name` now points to by its manifest digest,
    /// the second name of `archive` when it came from one; nothing is loaded
    /// or pulled again when that name already points to the version. msb cannot name a
    /// manifest no reference points to: without this, a load or pull that
    /// moves the tag leaves the old version in the cache for good. The tag
    /// has moved either way, so a failure is a warning, not the transfer's.
    pub async fn pin(&self, name: &str, archive: Option<&Path>) {
        // SDK imports and pulls already register their digest references.
        if self.uses_sdk() {
            return;
        }
        if let Err(e) = self.try_pin(name, archive).await {
            tracing::warn!("{name} is not pinned, so its version stays when replaced: {e}");
        }
    }

    async fn try_pin(&self, name: &str, archive: Option<&Path>) -> Result<(), String> {
        if pinned(name) {
            return Err("it is named by a digest, not a tag".into());
        }
        let held = self
            .inspect(name)
            .await?
            .ok_or("the cache holds no image under it")?;
        let start = name.rfind('/').map_or(0, |i| i + 1);
        let repository = name[start..].find(':').map_or(name, |i| &name[..start + i]);
        let pin = format!("{repository}@{}", held.digest);
        // A pin under this name for another version (made by an earlier
        // build that pinned a wrong name) is named again.
        if self
            .inspect(&pin)
            .await?
            .is_some_and(|p| p.digest == held.digest)
        {
            return Ok(());
        }
        let path = archive.map(|a| a.to_string_lossy().to_string());
        let args = match &path {
            Some(path) => vec!["load", "--quiet", "--input", path, "--tag", &pin],
            None => vec!["pull", "--quiet", &pin],
        };
        self.finish("pin", self.output_within(&args, IMAGE_TRANSFER).await?)
    }

    /// Removes the versions no tag points to anymore. One a job still runs
    /// on is refused and stays for the next time: after a load or pull, and
    /// before each job. The caller holds `lock_images`, so that no load is
    /// writing the layer files a removal deletes.
    pub async fn collect(&self) {
        // The Hub sweep reindexes workspace snapshots before removing old versions.
        if self.uses_sdk() {
            return;
        }
        let listed = match self.listed().await {
            Ok(listed) => listed,
            Err(e) => return tracing::warn!("listing the image cache failed: {e}"),
        };
        let tagged: std::collections::HashSet<&str> = listed
            .iter()
            .filter(|l| !pinned(&l.reference))
            .map(|l| l.digest.as_str())
            .collect();
        for l in &listed {
            if pinned(&l.reference) && !tagged.contains(l.digest.as_str()) {
                match self.output(&["image", "remove", "-q", &l.reference]).await {
                    Ok(out) if out.status.success() => {}
                    // Most often a job still runs on it.
                    Ok(out) => tracing::debug!(
                        "{} stays for now: {}",
                        l.reference,
                        tail(&out.stderr, 1024)
                    ),
                    Err(e) => tracing::warn!("removing {} failed: {e}", l.reference),
                }
            }
        }
    }

    /// Locks the image cache. The lock is a file of `MSB_HOME`, so that it
    /// holds across the processes sharing the cache (the service, its
    /// runner, `guildbotics runner pull`). Pulls and loads hold it exclusively; a
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
            .open(self.home.join(gb_vm::IMAGE_LOCK))
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

    /// Pulls `reference` into the cache and pins it; the caller holds
    /// `lock_images(true)`.
    pub async fn pull(&self, reference: &str) -> Result<(), String> {
        let _ = std::fs::create_dir_all(&self.home);
        self.finish(
            "pull",
            self.output_within(&["pull", "--quiet", reference], IMAGE_TRANSFER)
                .await?,
        )?;
        self.register_pull(reference).await
    }

    pub(super) async fn register_pull(&self, reference: &str) -> Result<(), String> {
        #[cfg(feature = "host")]
        if self.sdk {
            return self.sdk_register_pulled_image(reference).await;
        }
        self.pin(reference, None).await;
        Ok(())
    }

    /// Loads a `docker save` / OCI archive into the cache; the caller holds
    /// `lock_images(true)` and pins the reference the load registered (msb
    /// tags only the first image of an archive).
    pub async fn load(&self, archive: &Path) -> Result<(), String> {
        #[cfg(feature = "host")]
        if self.sdk {
            return self.sdk_load(archive, Vec::new()).await;
        }
        let _ = std::fs::create_dir_all(&self.home);
        let path = archive.to_string_lossy();
        self.finish(
            "load",
            self.output_within(&["load", "--quiet", "--input", &path], IMAGE_TRANSFER)
                .await?,
        )
    }

    /// Load one configuration under exactly the requested tag, without moving its other aliases.
    pub async fn load_as(&self, archive: &Path, reference: &str) -> Result<(), String> {
        #[cfg(feature = "host")]
        if self.sdk {
            return self.sdk_load(archive, vec![reference.to_owned()]).await;
        }
        let path = archive.to_string_lossy();
        self.finish(
            "load",
            self.output_within(
                &["load", "--quiet", "--input", &path, "--tag", reference],
                IMAGE_TRANSFER,
            )
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

    /// The volume of a job's `/work`, named like its `/tmp`'s.
    fn work_volume(&self, id: &str) -> String {
        format!("bv-work-{}-{id}", self.runner)
    }

    fn cache_volume(&self, id: &str) -> String {
        format!("bv-cache-{}-{id}", self.runner)
    }

    /// The name of the new `/work` that replaces `name` when the cache entry
    /// is dropped.
    fn fresh_volume(name: &str) -> String {
        format!("{name}{FRESH}")
    }

    /// Creates the disk volume `name` of `mib` MiB;
    /// returns the host file of its image.
    async fn create_disk(&self, name: &str, mib: u64) -> Result<PathBuf, String> {
        #[cfg(feature = "host")]
        if self.sdk {
            return self.sdk_create_disk(name, mib).await;
        }
        let size = format!("{mib}M");
        let args = [
            "volume", "create", name, "--kind", "disk", "--size", &size, "-q",
        ];
        let out = self.output(&args).await?;
        if !out.status.success() {
            return Err(format!("msb volume create: {}", tail(&out.stderr, 1024)));
        }
        let out = self.output(&["volume", "inspect", name]).await?;
        if !out.status.success() {
            return Err(format!("msb volume inspect: {}", tail(&out.stderr, 1024)));
        }
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .find_map(|l| l.strip_prefix("Path:").map(|p| PathBuf::from(p.trim())))
            .ok_or_else(|| format!("msb volume inspect names no path of {name}"))
    }

    /// Runs `script` as root in a VM of the job's image with the job's
    /// `/work` (`disk`) and the transfer directory `io`, to which it may add
    /// `quota_mib`, to prepare `/work` or to collect from it; `cancel`
    /// turning true stops it. The VM is removed once it ended. Returns why it
    /// failed, or `None` when it was stopped.
    async fn helper(
        &self,
        job: &JobRun<'_>,
        what: &str,
        quota_mib: u64,
        disks: HelperDisks<'_>,
        script: &str,
        cancel: &mut watch::Receiver<bool>,
    ) -> Result<(), Option<String>> {
        #[cfg(feature = "host")]
        if self.sdk {
            return self
                .sdk_helper(job, what, quota_mib, disks, script, cancel)
                .await;
        }
        let name = format!("bv-{what}-{}", job.attempt_id.to_lowercase());
        let mut cmd = self.command().ok_or_else(|| Some(self.missing()))?;
        let io = job.io.to_string_lossy();
        cmd.args(["run", "--name", &name, "--quiet", "--pull", "never"])
            .args(["--label", &format!("branvil.runner={}", self.runner)])
            .args(["--label", &format!("branvil.job={}", job.attempt_id)])
            .args(["--user", "0:0"])
            .args([
                "--mount-dir",
                &format!("{io}:{IO_MOUNT}:quota={quota_mib}M"),
            ]);
        for (at, disk) in disks.mounts() {
            cmd.args(["--mount-named", &disk.mount(at)]);
        }
        cmd.args(["--max-duration", &format!("{HELPER_SECS}s")])
            .arg("--no-net")
            .arg(&job.job.image)
            .args(["--", "/bin/sh", "-c", script])
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        let child = cmd
            .spawn()
            .map_err(|e| Some(format!("cannot start msb: {e}")))?;
        // Dropping the child on a stop kills `msb run`, which stops its VM.
        let ended = tokio::select! {
            out = child.wait_with_output() => Some(out),
            Ok(_) = cancel.wait_for(|stop| *stop) => None,
        };
        self.clean("the job's sandbox", &["remove", "--force", "-q", &name])
            .await;
        match ended {
            None => Err(None),
            Some(Ok(out)) if out.status.success() => Ok(()),
            Some(Ok(out)) => Err(Some(format!("{}: {}", out.status, tail(&out.stderr, 1024)))),
            Some(Err(e)) => Err(Some(format!("waiting for msb failed: {e}"))),
        }
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

    /// Removes the volumes whose names `pick` accepts, warning when
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
                tracing::warn!("listing volumes failed: {e}");
                return;
            }
        };
        if !names.is_empty() {
            let mut args = vec!["volume", "remove", "-q"];
            args.extend(names.iter().map(String::as_str));
            self.clean("volumes", &args).await;
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

/// Whether `reference` is a version's pin: a job names an image only by its
/// tag (`pipeline::valid_image` refuses `@`).
fn pinned(reference: &str) -> bool {
    reference.contains('@')
}

/// Where a job's log directory is mounted in the VM.
pub const LOG_MOUNT: &str = "/.branvil-log";

/// A mount source after its Windows drive (`C:\`): msb reads that colon as
/// part of the path, and any other ':' or ',' as a separator.
fn after_drive(dir: &str) -> &str {
    match dir.as_bytes() {
        [d, b':', b'\\', ..] if d.is_ascii_alphabetic() => &dir[2..],
        _ => dir,
    }
}

/// The spelling of the canonical directory `dir` that Git and msb take on
/// the host. Windows' canonical form is verbatim (`\\?\C:\…`), which both
/// refuse; its plain drive path is used only when `resolve` (the OS's
/// canonical form) takes it back to `dir`, as Windows normalizes a plain path
/// (dropping a component's trailing dots, for one) and it could then name
/// another directory. Other directories are kept as they are.
pub fn plain_path(
    dir: PathBuf,
    resolve: impl Fn(&Path) -> std::io::Result<PathBuf>,
) -> std::io::Result<PathBuf> {
    let Some(plain) = dir.to_str().and_then(|s| s.strip_prefix(r"\\?\")) else {
        return Ok(dir);
    };
    if after_drive(plain).len() < plain.len() && resolve(Path::new(plain)).is_ok_and(|p| p == dir) {
        return Ok(PathBuf::from(plain));
    }
    Err(std::io::Error::other(format!(
        "{} has no drive path naming it",
        dir.display()
    )))
}

/// The uid and gid of the fixed non-root user a job's commands run as. The
/// work and log directories are presented to the VM as this user's.
pub const JOB_USER_ID: u32 = 1000;

/// The size of the disk a job's `/tmp` is on: the size of the root disk msb
/// gives a VM by default, which Branvil keeps.
const TMP_DISK: &str = "4G";

/// Where the transfer directory ([`JobRun::io`]) is mounted in the VMs that
/// prepare and collect `/work`.
pub const IO_MOUNT: &str = "/.branvil-io";
/// Where the collector mounts a new cache disk when saving.
const CACHE_MOUNT: &str = "/.branvil-cache";
/// The archives of the transfer directory: the checkout unpacked into
/// `/work`, and the artifacts collected from it.
pub const CHECKOUT: &str = "checkout.tar";
/// The names of the checkout's members, as the bytes of their headers, one
/// per line ([`super::workspace::list_members`]).
const MEMBERS: &str = "checkout.members";
pub const ARTIFACTS: &str = "artifacts.tar";
/// The marks the preparing VM leaves in the transfer directory when `/work`
/// could not keep the cache entry it started from, and when the image has
/// no `resize2fs` to grow the entry's file system to the disk.
const CACHE_DROPPED: &str = "cache.dropped";
const CACHE_UNGROWN: &str = "cache.ungrown";
/// The KiB used on the cache disk after the collecting VM copied the cached
/// paths and flushed the disk. Without this mark the disk is discarded.
const CACHE_KEPT: &str = "cache.kept";
/// The time limit of a VM that prepares or collects `/work`.
const HELPER_SECS: u64 = 3600;
/// How long a job stopped at its timeout may take to flush its disks.
const TIMEOUT_FLUSH: Duration = Duration::from_secs(30);

/// An ext4 block group: 32768 blocks of 4 KiB.
const GROUP_MIB: u64 = 128;

/// The suffix of the volume of a new `/work` ([`Microsandbox::fresh_volume`]).
const FRESH: &str = "-new";

/// The size of a job's `/work`, which the checkout's archive is unpacked
/// into: the quota, plus twice its size (a disk's blocks and inodes hold many
/// small files in more than the archive's bytes) and room for the file
/// system, plus the bytes of data the cache entry's image already holds,
/// rounded up to whole ext4 block groups: msb refuses a disk whose last,
/// partial group is too small for its metadata (16642 MiB, the default quota
/// with a small checkout, ends in a group of 2 MiB).
fn work_disk_mib(quota_mib: u64, checkout: u64, cached: u64) -> u64 {
    (quota_mib + 2 * checkout.div_ceil(1 << 20) + 256 + cached.div_ceil(1 << 20))
        .next_multiple_of(GROUP_MIB)
}

/// A shell loop over `paths`, quoted, that sets `q` to each without its
/// trailing `/` and `link` to the first of its components but the last that
/// is a symbolic link, if any; the commands `body` run for each, in the
/// current directory.
fn each_path(paths: &[&str], body: &str) -> String {
    let mut s = String::from("set -f\nfor p in");
    for p in paths {
        s.push_str(&format!(" '{}'", p.replace('\'', "'\\''")));
    }
    let check =
        "[ -n \"$link\" ] || [ -z \"$c\" ] || [ ! -h \"./$c\" ] || link=$c; c=${c:+$c/}$part";
    s.push_str(&format!(
        "; do\n  q=${{p%/}}; c=; link=\n  IFS=/; for part in $q; do {check}; done; unset IFS\n{body}\ndone\nset +f\n"
    ));
    s
}

/// How `/work` starts from a cache entry's image: whether its file system is
/// smaller than the disk, whether it leaves the quota without growing, and
/// its size.
struct Restore {
    grow: bool,
    ungrown_fits: bool,
    fs_mib: u64,
}

/// Unpacks the checkout into `work`, a disk msb gives to root. When the disk
/// is a cache entry's image (`restore`), which holds the cached paths only,
/// the checkout goes over them, after its file system was grown to the whole
/// disk when it is smaller (an image without `resize2fs` keeps the entry's
/// file system when that still leaves the quota, and [`CACHE_UNGROWN`] says
/// so). Should the growing fail, should a member of the checkout's archive
/// (`.git` included) be at or below a symbolic link of the entry (unpacking
/// would write through it, wherever it points: [`MEMBERS`] and the entry's
/// links, both as raw bytes and whatever their number, meet in `awk`; a
/// `find` that fails, which then prints no last `/`, and a link whose path
/// holds a newline anywhere, which a line cannot carry, count as links
/// there), or
/// should the checkout not unpack
/// over them, it stops there and [`CACHE_DROPPED`] says so: the runner then prepares a new, empty disk without the entry. The
/// entry's other links stay; a cached path through a link of the commit gets
/// no entry ([`super::workspace::Tree::link_on`]). Otherwise it hands `work`
/// to the job's user, and flushes: microsandbox stops the VM when its
/// command ends, and what the guest has not written to the disk can be lost.
fn prepare_script(work: &str, io: &str, restore: Option<&Restore>) -> String {
    let unpack = format!("tar -xf {io}/{CHECKOUT} -C {work}");
    let mut s = String::from("set -e\n");
    if let Some(r) = restore {
        let without = if r.ungrown_fits {
            format!("echo ungrown >{io}/{CACHE_UNGROWN}")
        } else {
            "false".into()
        };
        let grown = if r.grow {
            format!(
                "if command -v resize2fs >/dev/null; then resize2fs \"$(df -P {work} | awk 'NR == 2 {{print $1}}')\" >/dev/null; else {without}; fi"
            )
        } else {
            "true".into()
        };
        // Every component of every member against the entry's links.
        let below = format!(
            "(cd {work} && nl=$(printf '\\nx') && nl=${{nl%x}} && [ \"$(find . -type l -path \"*$nl*\" && echo /)\" = / ] && awk 'BEGIN {{ f = \"find . -type l && echo /\"; while ((f | getline l) > 0) {{ if (l == \"/\") done = 1; sub(/^\\.\\//, \"\", l); link[l] = 1 }} if (!done) bad = 1 }} {{ n = split($0, c, \"/\"); q = \"\"; for (i = 1; i <= n; i++) {{ if (c[i] == \"\") continue; q = (q == \"\") ? c[i] : (q \"/\" c[i]); if (q in link) bad = 1 }} }} END {{ exit bad }}' {io}/{MEMBERS})"
        );
        s.push_str(&format!(
            "grown() {{ {grown}; }}\nif ! grown || ! {below} || ! {unpack}; then\n  echo dropped >{io}/{CACHE_DROPPED}\n  exit 0\nfi\n"
        ));
    } else {
        s.push_str(&format!("{unpack}\n"));
    }
    s.push_str(&format!(
        "chown -R {JOB_USER_ID}:{JOB_USER_ID} {work}\nsync\n"
    ));
    s
}

/// Streams the selected paths into the new cache disk. The producer's
/// success mark and the extractor's exit status both gate CACHE_KEPT.
fn keep_script(disk: &str, io: &str, cached: &[&str]) -> String {
    let pick = pick_paths(cached);
    format!(
        "{pick}rm -f {packed}\nif [ $# -gt 0 ]; then\n  {{ tar -cf - \"$@\" && : >{packed}; }} | tar -xpf - -C {disk}\n  [ -f {packed} ]\nfi\nsync\nused=$(df -Pk {disk} | awk 'NR == 2 {{print $3}}')\n[ -n \"$used\" ]\necho \"$used\" >{io}/{CACHE_KEPT}\n",
        packed = format_args!("{io}/{CACHE_KEPT}.packed"),
    )
}

/// Selects paths relative to the working directory without following links.
fn pick_paths(paths: &[&str]) -> String {
    let pick = each_path(
        paths,
        "  if [ -z \"$link\" ] && { [ -e \"./$q\" ] || [ -h \"./$q\" ]; }; then set -- \"$@\" \"./$q\"; fi",
    );
    format!("set --\n{pick}")
}

/// Packs those of `paths` (relative to the current directory) that exist
/// and go through no link (one at the end is packed as a link) into the
/// archive `name` of the directory `io`, an empty archive when there are
/// none, written in large blocks (writes through a mount are slow one by
/// one) and flushed (`conv=fsync`: the VM is removed once it ended, and a
/// write that does not reach `io` fails `dd`). It is written as
/// `<name>.part` and gets its name only once `tar` (whose status the
/// pipeline loses, so it leaves `<name>.packed`) and `dd` succeeded: a
/// failure, of any of them or of the marks, leaves no archive by that name.
fn pack_script(io: &str, name: &str, paths: &[&str]) -> String {
    let pick = pick_paths(paths);
    format!(
        "{pick}rm -f {done}\nif [ $# -gt 0 ]; then\n  {{ tar -cf - \"$@\" && : >{done}; }} | dd of={part} obs=4194304 conv=fsync 2>/dev/null || rm -f {done}\nelse\n  dd if=/dev/zero of={part} bs=10240 count=1 conv=fsync 2>/dev/null && : >{done} || rm -f {done}\nfi\nif [ -f {done} ]; then mv {part} {io}/{name}; fi\n",
        done = format!("{io}/{name}.packed"),
        part = format!("{io}/{name}.part"),
    )
}

/// Packs `artifacts` of `work` into their archive in `io`, which is not
/// there when it failed, then saves the cached paths to the new disk
/// ([`keep_script`]) when requested, in a shell of their own: [`CACHE_KEPT`] says that
/// all of it succeeded (and what the disk holds), and a failure of it only
/// keeps the disk out of the cache. A failure of the script itself (`work` not there, a mark that
/// cannot be removed, the rename) fails it, and the host keeps nothing.
fn collect_script(
    work: &str,
    io: &str,
    artifacts: &[&str],
    cache: Option<(&str, &[&str])>,
) -> String {
    let mut s = format!("set -e\ncd {work}\n");
    if !artifacts.is_empty() {
        s.push_str(&pack_script(io, ARTIFACTS, artifacts));
    }
    if let Some((disk, cached)) = cache {
        s.push_str(&format!(
            // No cached path holds a `:` (`pipeline::check_relative`): none
            // ends the here-document.
            "sh -e <<'BRANVIL:KEEP' || :\n{}BRANVIL:KEEP\n",
            keep_script(disk, io, cached)
        ));
    }
    s
}

/// The shell script a job runs: a flush when it exits (as for the helpers,
/// the VM stops when it ends), its output into the log file, `HOME` moved
/// to `/tmp/home` when the user cannot write the one the image gives it (or
/// it has none), a start marker, then each command in its own `sh -c`,
/// stopping at the first failure with its exit code.
///
/// The output goes to a file of the log mount, not to `msb run`'s stdout:
/// msb 0.6.17 hands a command's stdout and stderr over only when the command
/// exits, so a running job's log would stay empty and a stopped job's output
/// (a timeout, a cancellation) would be lost.
pub fn script(commands: &[String]) -> String {
    job_script(commands, true)
}

fn job_script(commands: &[String], log_file: bool) -> String {
    let mut s = String::from("trap sync EXIT\n");
    if log_file {
        s.push_str(&format!("exec >>{LOG_MOUNT}/out 2>&1\n"));
    }
    s.push_str("[ -w \"$HOME\" ] || { export HOME=/tmp/home; mkdir -p \"$HOME\"; }\n");
    if log_file {
        s.push_str("printf '\\036branvil:started\\n'\n");
    }
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

struct JobDisk {
    name: String,
    mib: u64,
    /// The host file of the volume's image.
    path: PathBuf,
}

impl JobDisk {
    fn mount(&self, at: &str) -> String {
        format!("{}:{at}:kind=disk,size={}M", self.name, self.mib)
    }
}

struct HelperDisks<'a> {
    work: &'a JobDisk,
    cache: Option<&'a JobDisk>,
}

impl<'a> HelperDisks<'a> {
    fn mounts(&self) -> impl Iterator<Item = (&'static str, &'a JobDisk)> + use<'a> {
        [
            Some(("/work", self.work)),
            self.cache.map(|disk| (CACHE_MOUNT, disk)),
        ]
        .into_iter()
        .flatten()
    }
}

impl Executor for Microsandbox {
    fn machine(&self) -> Option<crate::ci::api::Machine> {
        let started = self.started.lock().unwrap();
        super::machine::sample(|| started.iter().cloned().collect())
    }

    fn needs_resolution(&self) -> bool {
        matches!(self.msb, Msb::Missing(_))
    }

    fn name(&self) -> &'static str {
        "microsandbox"
    }

    async fn synchronize(&self, api: &super::Api) -> Result<(), super::ApiError> {
        api.sync_images(self).await
    }
    async fn inventory(&self, _api: &super::Api) -> Result<Inventory, super::ApiError> {
        let mut images = Vec::new();
        let mut failures = Vec::new();
        for item in self.listed().await.map_err(super::ApiError::Other)? {
            if pinned(&item.reference) {
                continue;
            }
            match self.inspect_status(&item.reference, true).await {
                Ok(Some(i)) => images.push(crate::ci::images::Image {
                    reference: i.reference,
                    digest: i.config_digest,
                    architecture: crate::ci::images::architecture().into(),
                }),
                Ok(None) => {}
                Err(e) => failures.push(format!("{}: {e}", item.reference)),
            }
        }
        Ok(Inventory {
            images,
            detail: (!failures.is_empty())
                .then(|| format!("Cannot inspect cached images: {}", failures.join("; "))),
        })
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
        self.collect().await;
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
        mut job: JobRun<'_>,
        log: mpsc::Sender<Vec<u8>>,
        mut cancel: watch::Receiver<bool>,
    ) -> Outcome {
        if self.command().is_none() {
            return Outcome::StartupFailure(self.missing());
        }
        let log_dir = job.log_dir.to_string_lossy();
        let output = if self.uses_sdk() {
            None
        } else {
            let io = job.io.to_string_lossy();
            if let Some(dir) = [&io, &log_dir].into_iter().find(|d| {
                let body = after_drive(d);
                body.contains(':') || body.contains(',')
            }) {
                return Outcome::StartupFailure(format!(
                    "the work directory {dir} contains ':' or ',', which msb mount specifications cannot carry"
                ));
            }
            // Open before the VM starts. A path the job replaces is never reopened.
            match tokio::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .open(job.log_dir.join("out"))
                .await
            {
                Ok(file) => Some(file),
                Err(e) => {
                    return Outcome::StartupFailure(format!("cannot create the job log: {e}"));
                }
            }
        };
        let say = |text: String| {
            let log = log.clone();
            async move {
                let _ = log.send(format!("[branvil] {text}\n").into_bytes()).await;
            }
        };
        let checkout = std::fs::metadata(job.io.join(CHECKOUT)).map_or(0, |m| m.len());
        let cached_paths: Vec<&str> = job
            .job
            .cache
            .iter()
            .flat_map(|c| c.paths.iter().map(String::as_str))
            .collect();
        // A cached path through a link of the commit: unpacking the checkout
        // over an entry would write through it, so this run neither uses
        // nor keeps one.
        let linked = job.tree.link_on(cached_paths.iter().copied());
        if let Some(p) = linked {
            say(format!("the cached path {p} goes through a symbolic link of the commit, which unpacking the checkout over the cache entry would write through; the job runs without the cache")).await;
            let _ = std::fs::remove_file(job.cache_disk);
        }
        // The cache entry's image: its length, the bytes of data its file
        // system holds (what the copy takes when the entry does not say),
        // and the size of its file system.
        let image = std::fs::symlink_metadata(job.cache_disk)
            .ok()
            .filter(|m| m.is_file())
            .map(|m| {
                let data = job
                    .cache_data
                    .unwrap_or_else(|| super::disk::allocated(job.cache_disk).unwrap_or(m.len()));
                let fs = super::disk::ext4_bytes(job.cache_disk).ok();
                (m.len(), data, fs)
            });
        let needed_mib = |cached: u64| {
            job.work_quota_mib + 2 * checkout.div_ceil(1 << 20) + 256 + cached.div_ceil(1 << 20)
        };
        // No smaller than the entry's image, which an earlier job with more
        // data or a larger checkout left: its file is sparse, so the room it
        // keeps takes no blocks, and the entry stays usable.
        let disk_mib = work_disk_mib(job.work_quota_mib, checkout, image.map_or(0, |i| i.1))
            .max(image.map_or(0, |i| i.0.div_ceil(1 << 20).next_multiple_of(GROUP_MIB)));
        let name = self.work_volume(job.attempt_id);
        let mut disk = match self.create_disk(&name, disk_mib).await {
            Ok(path) => JobDisk {
                name,
                mib: disk_mib,
                path,
            },
            Err(e) => {
                let _ = std::fs::remove_file(job.cache_disk);
                self.remove_volumes(|n| n == name).await;
                return Outcome::StartupFailure(format!("Creating /work failed: {e}"));
            }
        };
        // The disk becomes the entry's image, grown to its size: a rename or
        // a clone, no copy of its data where the file system shares blocks.
        // An image larger than the disk, or with no file system to grow, is
        // not used: the disk keeps the size the quota gives.
        let mut restore = None;
        match image {
            Some((_, _, None)) => {
                say("the cache entry holds no ext4 file system; the job runs without it".into())
                    .await
            }
            Some((_, data, Some(fs))) => {
                let (from, to, size) = (
                    job.cache_disk.to_path_buf(),
                    disk.path.clone(),
                    disk_mib << 20,
                );
                let (archive, members) = (job.io.join(CHECKOUT), job.io.join(MEMBERS));
                match super::blocking(move || {
                    super::workspace::list_members(&archive, &members)?;
                    super::disk::grow(&from, size)?;
                    super::disk::place(&from, &to)
                })
                .await
                {
                    Ok(()) => {
                        restore = Some(Restore {
                            grow: fs < disk_mib << 20,
                            // Without growing, it still leaves the quota.
                            ungrown_fits: fs >= needed_mib(data) << 20,
                            fs_mib: fs >> 20,
                        })
                    }
                    Err(e) => {
                        say(format!(
                            "the cache entry could not become /work ({e}); the job runs without it"
                        ))
                        .await
                    }
                }
            }
            None => {}
        }
        let _ = std::fs::remove_file(job.cache_disk);
        // What the collecting VM may add to `io`: the artifacts' archive of
        // `/work`'s files.
        let collect_mib = disk_mib;
        let image_lock = std::mem::take(&mut job.image_lock);
        let preparing = Instant::now();
        let mut prepared = self
            .helper(
                &job,
                "prep",
                // It writes no more than a mark.
                1,
                HelperDisks {
                    work: &disk,
                    cache: None,
                },
                &prepare_script("/work", IO_MOUNT, restore.as_ref()),
                &mut cancel,
            )
            .await;
        // A disk the entry left unusable is not carried on: a new, empty
        // one of the size the quota gives, under a name of its own (the old
        // one may outlive its removal), prepared without the entry.
        let ungrown = std::fs::remove_file(job.io.join(CACHE_UNGROWN)).is_ok();
        let dropped = match &prepared {
            Ok(()) if std::fs::remove_file(job.io.join(CACHE_DROPPED)).is_ok() => {
                say("the cache entry was not kept (its file system could not grow to /work, a member of the checkout is at or below a symbolic link of the entry, or the checkout did not unpack over it); the job runs without it".into()).await;
                true
            }
            Err(Some(e)) if restore.is_some() => {
                say(format!(
                    "preparing /work from the cache entry failed ({e}); the job runs without it"
                ))
                .await;
                true
            }
            _ => false,
        };
        if dropped && *cancel.borrow() {
            prepared = Err(None);
        } else if dropped {
            self.remove_volumes(|n| n == disk.name).await;
            let (name, mib) = (
                Self::fresh_volume(&disk.name),
                work_disk_mib(job.work_quota_mib, checkout, 0),
            );
            prepared = match self.create_disk(&name, mib).await {
                Ok(path) => {
                    disk = JobDisk { name, mib, path };
                    // A sandbox of its own name too.
                    self.helper(
                        &job,
                        "prep-new",
                        1,
                        HelperDisks {
                            work: &disk,
                            cache: None,
                        },
                        &prepare_script("/work", IO_MOUNT, None),
                        &mut cancel,
                    )
                    .await
                }
                Err(e) => Err(Some(format!("creating a new /work: {e}"))),
            };
        } else if ungrown {
            let fs = restore.as_ref().map_or(0, |r| r.fs_mib);
            say(format!("the image has no resize2fs: /work keeps the cache entry's file system, {fs} MiB rather than {disk_mib} MiB")).await;
        }
        let used_exact_cache = job.exact_cache && restore.is_some() && !dropped && prepared.is_ok();
        let mut outcome = match prepared {
            Err(e) => {
                drop(image_lock);
                match e {
                    None => Outcome::Cancelled,
                    Some(e) => Outcome::StartupFailure(format!("Preparing /work failed: {e}")),
                }
            }
            Ok(()) => {
                say(format!(
                    "/work prepared in {} ms",
                    preparing.elapsed().as_millis()
                ))
                .await;
                match output {
                    Some(output) => {
                        self.run_job(
                            &mut job,
                            (&disk.mount("/work"), &log_dir),
                            output,
                            image_lock,
                            &log,
                            &mut cancel,
                        )
                        .await
                    }
                    #[cfg(feature = "host")]
                    None => {
                        self.sdk_job(&mut job, &disk, image_lock, &log, &mut cancel)
                            .await
                    }
                    #[cfg(not(feature = "host"))]
                    None => unreachable!("the client always uses the CLI executor"),
                }
            }
        };
        let removing = Instant::now();
        // The job's VM has ended; only then is `/work` collected, so that
        // nothing it left running changes what is packed.
        let name = Self::sandbox_name(job.attempt_id);
        self.clean("the job's sandbox", &["remove", "--force", "-q", &name])
            .await;
        let artifacts: Vec<&str> = match outcome {
            Outcome::Exited(_) | Outcome::TimedOut => job
                .job
                .artifacts
                .iter()
                .flat_map(|a| a.paths.iter().map(String::as_str))
                .collect(),
            _ => Vec::new(),
        };
        let cache_name = self.cache_volume(job.attempt_id);
        let cache = if outcome == Outcome::Exited(0)
            && job.save_cache
            && linked.is_none()
            && !cached_paths.is_empty()
        {
            if used_exact_cache {
                say("cache save skipped: /work used the exact key".into()).await;
                None
            } else {
                match self.create_disk(&cache_name, disk.mib).await {
                    Ok(path) => Some(JobDisk {
                        name: cache_name.clone(),
                        mib: disk.mib,
                        path,
                    }),
                    Err(e) => {
                        say(format!(
                            "cache save skipped: creating the cache volume failed: {e}"
                        ))
                        .await;
                        None
                    }
                }
            }
        } else {
            None
        };
        // Only what the collecting VM writes counts: nothing an earlier VM
        // with `io` left there.
        for name in [ARTIFACTS, CACHE_KEPT] {
            for suffix in ["", ".part", ".packed"] {
                let _ = std::fs::remove_file(job.io.join(format!("{name}{suffix}")));
            }
        }
        if !artifacts.is_empty() || cache.is_some() {
            let collecting = Instant::now();
            let script = collect_script(
                "/work",
                IO_MOUNT,
                &artifacts,
                cache
                    .as_ref()
                    .map(|_| (CACHE_MOUNT, cached_paths.as_slice())),
            );
            // As for the job's VM: the image the tag names stays put until
            // the VM has picked it.
            // A stop does not wait for a load that holds the cache.
            let locked = tokio::select! {
                l = self.lock_images(false, || {}) => l.map_err(Some),
                Ok(_) = cancel.wait_for(|stop| *stop) => Err(None),
            };
            let collected = match locked {
                Ok(_lock) => {
                    self.helper(
                        &job,
                        "collect",
                        collect_mib,
                        HelperDisks {
                            work: &disk,
                            cache: cache.as_ref(),
                        },
                        &script,
                        &mut cancel,
                    )
                    .await
                }
                Err(e) => Err(e),
            };
            match collected {
                Ok(()) => {
                    say(format!(
                        "/work collected in {} ms",
                        collecting.elapsed().as_millis()
                    ))
                    .await;
                    // An archive is there only once it was packed whole, and
                    // only as the regular file of that name. The link is not
                    // followed: the VM can leave one where the archive should
                    // be, and that is not an archive.
                    let packed = std::fs::symlink_metadata(job.io.join(ARTIFACTS))
                        .is_ok_and(|m| m.is_file());
                    if !artifacts.is_empty() && !packed {
                        say(format!("packing {ARTIFACTS} failed")).await;
                    }
                    // Keep the new cache disk by its link after the
                    // collecting VM has ended.
                    // The mark, read as what a VM returned: a regular file,
                    // a few bytes.
                    let kept =
                        crate::runner::workspace::open_returned(&job.io.join(CACHE_KEPT), 64)
                            .ok()
                            .flatten()
                            .and_then(|mut f| {
                                let mut text = String::new();
                                std::io::Read::read_to_string(&mut f, &mut text).ok()?;
                                text.trim().parse::<u64>().ok()
                            });
                    let _ = std::fs::remove_file(job.io.join(CACHE_KEPT));
                    match (cache, kept) {
                        (Some(disk), Some(kib)) => {
                            let to = job.cache_disk.to_path_buf();
                            let linked = super::blocking(move || {
                                super::disk::flush(&disk.path)?;
                                std::fs::hard_link(&disk.path, &to)
                            }).await;
                            match linked {
                                Ok(()) => *job.kept_data = Some(kib << 10),
                                Err(e) => say(format!("cache save skipped: keeping the cache image failed: {e}")).await,
                            }
                        }
                        (Some(_), None) => say("cache save skipped: copying the cached paths in the collecting VM failed".into()).await,
                        (None, _) => {}
                    }
                }
                Err(e) => {
                    // A stop while only the cache was collected leaves the
                    // job's end as it was.
                    if e.is_none() && !artifacts.is_empty() {
                        outcome = Outcome::Cancelled;
                    }
                    say(format!(
                        "collecting /work failed: {}",
                        e.as_deref().unwrap_or("stopped on request")
                    ))
                    .await;
                    // The artifacts' archive is named only once it was packed
                    // whole: what failed later (the cache's steps) leaves it.
                    let _ = std::fs::remove_file(job.io.join(CACHE_KEPT));
                }
            }
        }
        // By label and from the list: one that never came to be is not an
        // error.
        let label = format!("branvil.job={}", job.attempt_id);
        self.clean(
            "the job's sandbox",
            &["remove", "--force", "-q", "--label", &label],
        )
        .await;
        // The volumes outlive their sandboxes (msb refuses to remove one
        // while a running sandbox has it).
        let volumes = [
            self.tmp_volume(job.attempt_id),
            self.work_volume(job.attempt_id),
            Self::fresh_volume(&self.work_volume(job.attempt_id)),
            cache_name,
        ];
        self.remove_volumes(|n| volumes.iter().any(|v| v == n))
            .await;
        say(format!(
            "sandbox removed in {} ms",
            removing.elapsed().as_millis()
        ))
        .await;
        // The work directory holds what came back, and nothing of the
        // checkout: a path the job did not leave has no artifact.
        if !artifacts.is_empty() {
            // Off the async threads, where the other jobs' heartbeats run.
            let (work, returned) = (job.work.to_path_buf(), job.io.join(ARTIFACTS));
            let unpacked = super::blocking(move || {
                std::fs::remove_dir_all(&work)
                    .and_then(|_| std::fs::create_dir(&work))
                    .and_then(|_| {
                        crate::runner::workspace::unpack_returned(
                            &returned,
                            collect_mib << 20,
                            &work,
                        )
                    })
            })
            .await;
            match unpacked {
                Ok(notes) => {
                    for n in notes {
                        say(format!("artifacts: {n}")).await;
                    }
                }
                Err(e) => say(format!("artifacts: unpacking failed: {e}")).await,
            }
        }
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
        let prefixes = [
            self.tmp_volume(""),
            self.work_volume(""),
            self.cache_volume(""),
        ];
        self.remove_volumes(|n| {
            prefixes.iter().any(|p| {
                n.strip_prefix(p.as_str())
                    .map(|id| id.strip_suffix(FRESH).unwrap_or(id))
                    .is_some_and(crate::ci::files::valid_attempt_id)
            })
        })
        .await;
    }
}

impl Microsandbox {
    /// Runs the job's commands as its user in a VM with the prepared
    /// `/work` (`disk`) and the log directory; returns how they ended. The
    /// image lock goes once the commands started.
    async fn run_job(
        &self,
        job: &mut JobRun<'_>,
        (disk, log_dir): (&str, &str),
        mut output: tokio::fs::File,
        image_lock: ImageLock,
        log: &mpsc::Sender<Vec<u8>>,
        cancel: &mut watch::Receiver<bool>,
    ) -> Outcome {
        let Some(mut cmd) = self.command() else {
            return Outcome::StartupFailure(self.missing());
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
            .args(["--mount-named", disk])
            // What the job writes to the host is bounded as `/work` is.
            .args([
                "--mount-dir",
                &format!(
                    "{log_dir}:{LOG_MOUNT}:{owner},quota={}M",
                    job.work_quota_mib
                ),
            ])
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
            log,
        };
        let mut image_lock = Some(image_lock);
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
                    if job_log.started.is_some() && let Some(lock) = image_lock.take() {
                        drop(lock);
                        if cfg!(windows) {
                            self.started.lock().unwrap().insert(job.attempt_id.into());
                        }
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
        // Reserve the allocation again before stopping this VM or preparing
        // the next helper VM, which has not committed its memory yet.
        if cfg!(windows) {
            self.started.lock().unwrap().remove(job.attempt_id);
        }
        // Started or not, the VM is past picking an image.
        drop(image_lock);
        let mut unflushed = false;
        let outcome = match stop {
            Some(o) => {
                let o = if o == Outcome::TimedOut && !j.artifacts.is_empty() {
                    // Its artifacts are collected: flush what it wrote,
                    // which the kill would lose (the job's own flush runs
                    // only when its script exits). A stop does not wait.
                    let flush = ["exec", name.as_str(), "--", "sync"];
                    let flushed = tokio::select! {
                        out = self.output_within(&flush, TIMEOUT_FLUSH) => {
                            Some(out.is_ok_and(|out| out.status.success()))
                        }
                        Ok(_) = cancel.wait_for(|stop| *stop) => None,
                    };
                    match flushed {
                        Some(ok) => {
                            unflushed = !ok;
                            o
                        }
                        None => Outcome::Cancelled,
                    }
                } else {
                    o
                };
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
        if unflushed {
            let note = "[branvil] flushing the job at its timeout failed: its artifacts may miss what it wrote last\n";
            let _ = log.send(note.into()).await;
        }
        err_reader.abort();
        if let Some(at) = job_log.started {
            let note = format!(
                "[branvil] commands ran for {} ms\n",
                at.elapsed().as_millis()
            );
            let _ = log.send(note.into_bytes()).await;
        }
        *job.peak_memory_mib = log_peak_memory(log, peak_kib).await;
        outcome
    }
}

/// Reports memory only when a positive sample was collected. A short-lived
/// process can exit before a sample sees it; zero is not a measured peak.
async fn log_peak_memory(log: &mpsc::Sender<Vec<u8>>, peak_kib: u64) -> Option<u64> {
    let mib = (peak_kib > 0).then(|| peak_kib.div_ceil(1024))?;
    let note = format!("[branvil] peak memory: {mib} MiB\n");
    let _ = log.send(note.into_bytes()).await;
    Some(mib)
}

/// The resident memory of process `pid` and all its descendants, in KiB,
/// or none when the process has exited or the OS will not report it.
#[cfg(not(windows))]
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

#[cfg(windows)]
async fn tree_rss_kib(pid: Option<u32>) -> Option<u64> {
    let root = pid?;
    tokio::task::spawn_blocking(move || windows_tree_rss(root))
        .await
        .ok()
        .flatten()
}

#[cfg(windows)]
fn windows_tree_rss(root: u32) -> Option<u64> {
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use windows_sys::Win32::{
        Foundation::INVALID_HANDLE_VALUE,
        System::{
            Diagnostics::ToolHelp::{
                CreateToolhelp32Snapshot, PROCESSENTRY32W, Process32FirstW, Process32NextW,
                TH32CS_SNAPPROCESS,
            },
            ProcessStatus::{K32GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS},
            Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION},
        },
    };

    // ToolHelp returns an owned snapshot; the process rows are copied out.
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return None;
    }
    let snapshot = unsafe { OwnedHandle::from_raw_handle(snapshot) };
    let mut entry = PROCESSENTRY32W {
        dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
        ..Default::default()
    };
    let mut processes = Vec::new();
    let mut has_row = unsafe { Process32FirstW(snapshot.as_raw_handle(), &mut entry) };
    while has_row != 0 {
        processes.push((entry.th32ProcessID, entry.th32ParentProcessID));
        has_row = unsafe { Process32NextW(snapshot.as_raw_handle(), &mut entry) };
    }
    let mut total = 0;
    for pid in process_tree(&processes, root)? {
        let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
        if handle.is_null() {
            return None;
        }
        let handle = unsafe { OwnedHandle::from_raw_handle(handle) };
        let size = std::mem::size_of::<PROCESS_MEMORY_COUNTERS>() as u32;
        let mut counters = PROCESS_MEMORY_COUNTERS {
            cb: size,
            ..Default::default()
        };
        if unsafe { K32GetProcessMemoryInfo(handle.as_raw_handle(), &mut counters, size) } == 0 {
            return None;
        }
        total += (counters.WorkingSetSize as u64).div_ceil(1024);
    }
    Some(total)
}

/// Sums the `rss` of `root` and its descendants in `pid ppid rss` lines;
/// none when `root` is not listed.
#[cfg(any(not(windows), test))]
fn tree_rss(ps: &str, root: u32) -> Option<u64> {
    let procs: Vec<(u32, u32, u64)> = ps
        .lines()
        .filter_map(|l| {
            let mut f = l.split_whitespace();
            Some((
                f.next()?.parse().ok()?,
                f.next()?.parse().ok()?,
                f.next()?.parse().ok()?,
            ))
        })
        .collect();
    let parents = procs
        .iter()
        .map(|&(pid, parent, _)| (pid, parent))
        .collect::<Vec<_>>();
    let tree = process_tree(&parents, root)?;
    Some(
        procs
            .iter()
            .filter(|(id, _, _)| tree.contains(id))
            .map(|&(_, _, rss)| rss)
            .sum(),
    )
}

fn process_tree(processes: &[(u32, u32)], root: u32) -> Option<Vec<u32>> {
    if !processes.iter().any(|&(pid, _)| pid == root) {
        return None;
    }
    let mut tree = vec![root];
    let mut i = 0;
    while let Some(&p) = tree.get(i) {
        for &(pid, parent) in processes {
            if parent == p && !tree.contains(&pid) {
                tree.push(pid)
            }
        }
        i += 1;
    }
    Some(tree)
}

fn tail(bytes: &[u8], n: usize) -> String {
    let start = bytes.len().saturating_sub(n);
    String::from_utf8_lossy(&bytes[start..]).trim().to_string()
}

#[cfg(test)]
pub(crate) mod tests {
    #[test]
    fn the_embedding_host_supplies_its_runtime_explicitly() {
        use super::{HOST_RUNTIME, set_runtime_binary};
        let dir = tempfile::tempdir().unwrap();
        let binary = dir.path().join("host-msb");
        assert!(set_runtime_binary("relative/msb".into(), None).is_err());
        let firmware = dir.path().join("host-firmware");
        assert!(set_runtime_binary(binary.clone(), Some("relative/library".into())).is_err());
        set_runtime_binary(binary.clone(), Some(firmware.clone())).unwrap();
        assert_eq!(
            HOST_RUNTIME.get(),
            Some(&(binary.clone(), Some(firmware.clone())))
        );
        set_runtime_binary(binary.clone(), Some(firmware.clone())).unwrap();
        assert!(set_runtime_binary(dir.path().join("other-msb"), None).is_err());

        let command = |path| {
            Microsandbox::new(Msb::Found(path), dir.path().into(), "fixture")
                .command()
                .unwrap()
        };
        let configured = command(binary);
        let library = configured
            .as_std()
            .get_envs()
            .find(|(key, _)| *key == "MSB_LIBKRUNFW_PATH")
            .and_then(|(_, value)| value);
        assert_eq!(library, Some(firmware.as_os_str()));
        assert!(
            !command(dir.path().join("other-msb"))
                .as_std()
                .get_envs()
                .any(|(key, _)| key == "MSB_LIBKRUNFW_PATH")
        );
    }
    use super::*;

    #[test]
    fn windows_directories_reach_msb_as_plain_drive_paths() {
        // Windows' canonical form of a plain path: it drops a component's
        // trailing dots and spaces, which the verbatim form keeps.
        let windows = |p: &Path| -> std::io::Result<PathBuf> {
            let parts: Vec<&str> = p
                .to_str()
                .unwrap()
                .split('\\')
                .map(|c| c.trim_end_matches(['.', ' ']))
                .collect();
            Ok(PathBuf::from(format!(r"\\?\{}", parts.join(r"\"))))
        };
        let missing =
            |_: &Path| -> std::io::Result<PathBuf> { Err(std::io::ErrorKind::NotFound.into()) };
        let unused = |p: &Path| -> std::io::Result<PathBuf> {
            panic!("{} has no verbatim prefix to resolve", p.display())
        };
        type Resolve<'a> = &'a dyn Fn(&Path) -> std::io::Result<PathBuf>;
        let cases: [(&str, Resolve, Option<&str>); 6] = [
            (
                r"\\?\C:\Users\x\.branvil\runner",
                &windows,
                Some(r"C:\Users\x\.branvil\runner"),
            ),
            (r"\\?\C:\Review\runner.", &windows, None),
            (r"\\?\C:\Review\runner ", &windows, None),
            (r"\\?\C:\gone", &missing, None),
            (r"\\?\UNC\srv\share\runner", &windows, None),
            ("/var/lib/runner", &unused, Some("/var/lib/runner")),
        ];
        for (dir, resolve, expected) in cases {
            match (plain_path(PathBuf::from(dir), resolve), expected) {
                (Ok(plain), Some(want)) => assert_eq!(plain, PathBuf::from(want), "{dir}"),
                (Err(e), None) => assert!(e.to_string().contains("no drive path"), "{dir}: {e}"),
                (got, want) => panic!("{dir}: {got:?}, expected {want:?}"),
            }
        }
        // Only the drive's colon is part of a path; msb splits at any other.
        for (dir, body) in [
            (r"C:\runner\jobs\1\work", r"\runner\jobs\1\work"),
            (r"C:\a:b\work", r"\a:b\work"),
            (r"\\?\C:\work", r"\\?\C:\work"),
            ("C:relative", "C:relative"),
            ("/srv/C:/work", "/srv/C:/work"),
        ] {
            assert_eq!(after_drive(dir), body, "{dir}");
        }
    }

    #[tokio::test]
    async fn memory_logging_requires_a_positive_sample() {
        // Supply the collected peak directly: neither `ps` nor the lifetime
        // of a real process decides which branch this test exercises.
        for (peak_kib, expected) in [(0, None), (1, Some(1)), (1024, Some(1)), (1025, Some(2))] {
            let (log, mut logs) = mpsc::channel(2);
            assert_eq!(log_peak_memory(&log, peak_kib).await, expected);
            drop(log);
            assert_eq!(
                logs.recv().await,
                expected.map(|mib| format!("[branvil] peak memory: {mib} MiB\n").into_bytes()),
                "peak {peak_kib} KiB"
            );
            assert_eq!(logs.recv().await, None);
        }
    }

    #[test]
    fn the_memory_of_a_job_is_its_process_tree() {
        let ps = "    1     0  9000\n   40     1   100\n   41    40  2000\n   42    41  3000\n   43     1   700\n  junk\n";
        assert_eq!(tree_rss(ps, 40), Some(5100));
        assert_eq!(tree_rss(ps, 42), Some(3000));
        assert_eq!(tree_rss(ps, 99), None);
    }

    #[test]
    fn the_work_disk_is_whole_ext4_block_groups() {
        // The default quota with a checkout of a few bytes: msb refused
        // 16642 MiB, whose last group is 2 MiB.
        assert_eq!(work_disk_mib(16384, 1, 0), 16768);
        assert_eq!(work_disk_mib(2048, 0, 0), 2304);
        // The cache entry's data counts once: it is not unpacked.
        assert_eq!(work_disk_mib(2048, 0, 128 << 20), 2432);
        for quota in [0, 1, 127, 128, 2048, 16384, 16385] {
            for archive in [0u64, 1, 1 << 20, (1 << 20) + 1, 21_343_338, 2_132_153_879] {
                for cached in [0u64, 1, 5_740_625_920] {
                    let raw =
                        quota + 2 * archive.div_ceil(1 << 20) + 256 + cached.div_ceil(1 << 20);
                    let mib = work_disk_mib(quota, archive, cached);
                    let at = format!("{quota} MiB, {archive} + {cached} bytes: {mib}");
                    assert_eq!(mib % 128, 0, "{at}");
                    assert!((raw..raw + 128).contains(&mib), "{at}");
                }
            }
        }
    }

    #[test]
    fn the_script_quotes_every_command() {
        let s = script(&["echo 'a b'".into(), "exit 3".into()]);
        // What the job wrote reaches `/work` and the log before its VM is
        // removed, however the commands end.
        assert!(s.starts_with(
            "trap sync EXIT\nexec >>/.branvil-log/out 2>&1\n[ -w \"$HOME\" ] || { export HOME=/tmp/home; mkdir -p \"$HOME\"; }\nprintf '\\036branvil:started\\n'\n"
        ));
        assert!(s.contains("/bin/sh -c 'echo '\\''a b'\\''' || exit $?"));
        assert!(s.contains("/bin/sh -c 'exit 3' || exit $?"));
    }

    /// Writes an executable script. On Unix a child process writes it: a
    /// descriptor of it open for writing here could pass into a process that
    /// another test forks meanwhile, and running the script would then fail
    /// with "text file busy".
    pub use super::test_support::write_script;

    /// An msb whose cache holds `img:dev` as the digest in `$MSB_HOME/fake/tag`:
    /// `load` writes the archive's content there, `run` notes its arguments
    /// in `fake/args-<name>`. The job's VM notes the digest it started,
    /// writes the start marker, and runs until `fake/done` exists; the VM
    /// that collects `/work` copies `fake/returned/*` into the transfer
    /// directory, or, when `fake/archive-links` names them, leaves a
    /// symbolic link of each name there; the VM that prepares it drops the
    /// cache entry once when `fake/prep-drops` exists, and fails once when
    /// `fake/prep-fails` does; the others end at once. `volume
    /// create` makes an empty image `fake/disks/<name>` (noting its
    /// arguments in `fake/created`), whose path `volume inspect` tells
    /// (it fails when `fake/inspect-fails` exists);
    /// `volume list` answers `fake/volumes`; removals are noted in
    /// `fake/removed`, and so is an
    /// `exec` into a job's VM while its `run` is alive (it fails otherwise,
    /// and when `fake/exec-fails` exists), which then runs while
    /// `fake/exec-hangs` exists.
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
  prev=""
  for a in "$@"; do
    [ "$prev" = "--name" ] && name="$a"
    [ "$prev" = "--mount-dir" ] && case "$a" in
      *:/.branvil-log:*) log="${a%%:/.branvil-log:*}" ;;
      *:/.branvil-io:*) io="${a%%:/.branvil-io:*}" ;;
    esac
    prev="$a"
  done
  printf '%s\n' "$@" > "$state/args-$name"
  case "$name" in
  bv-collect-*)
    if [ -f "$state/archive-links" ]; then
      while IFS= read -r name; do
        [ -n "$name" ] || continue
        ln -sfn "$state/tag" "$io/$name"
      done < "$state/archive-links"
    else
      cp "$state"/returned/* "$io" 2>/dev/null
    fi
    if [ -e "$state/link-fails" ]; then mkdir "${io%/*}/cache-disk.raw"; fi
    while [ -e "$state/collect-hangs" ]; do sleep 0.05; done
    [ ! -e "$state/collect-fails" ]; exit $? ;;
  bv-job-*) echo $$ > "$state/pid-$name" ;;
  bv-prep-*)
    if [ -e "$state/prep-drops" ]; then rm -f "$state/prep-drops"; : > "$io/cache.dropped"; fi
    if [ -e "$state/prep-fails" ]; then rm -f "$state/prep-fails"; exit 1; fi
    exit 0 ;;
  *) exit 0 ;;
  esac
  cat "$state/tag" > "$state/ran"
  printf '\036branvil:started\n' >> "$log/out"
  while [ ! -f "$state/done" ]; do sleep 0.05; done
  [ ! -e "$state/job-fails" ] ;;
remove)
  echo "$*" >> "$state/removed" ;;
exec)
  [ ! -e "$state/exec-fails" ] || exit 1
  kill -0 "$(cat "$state/pid-$2")" 2>/dev/null || exit 1
  echo "$*" >> "$state/removed"
  while [ -e "$state/exec-hangs" ]; do sleep 0.05; done ;;
volume)
  case "$2" in
  create)
    mkdir -p "$state/disks"; : > "$state/disks/$3"; echo "$*" >> "$state/created"
    case "$3" in
      bv-cache-*)
        if [ -f "$state/cache-image" ]; then
          cp "$state/cache-image" "$state/disks/$3"
          ln "$state/disks/$3" "$state/cache-witness"
        fi ;;
    esac ;;
  inspect) [ ! -e "$state/inspect-fails" ] || exit 1
    case "$3" in bv-cache-*) [ ! -e "$state/cache-inspect-fails" ] || exit 1 ;; esac
    printf 'Name:           %s\nPath:           %s\n' "$3" "$state/disks/$3" ;;
  list)
    volumes=$(cat "$state/volumes")
    for disk in "$state"/disks/bv-cache-*; do
      [ -f "$disk" ] || continue
      volumes="${volumes%]}"
      [ "$volumes" = "[" ] || volumes="$volumes,"
      volumes="$volumes{\"name\":\"${disk##*/}\"}]"
    done
    printf '%s\n' "$volumes" ;;
  remove)
    echo "$*" >> "$state/removed"
    for name in "$@"; do
      case "$name" in bv-cache-*) rm -f "$state/disks/$name" ;; esac
    done ;;
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
        // The job's /tmp and /work volumes, and another runner's.
        std::fs::write(
            state.join("volumes"),
            r#"[{"name": "bv-tmp-t-a1"}, {"name": "bv-tmp-u-a1"}, {"name": "bv-work-t-a1"}]"#,
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

        let (work, log_dir, io) = (root.join("work"), root.join("log"), root.join("io"));
        for d in [&work, &log_dir, &io] {
            std::fs::create_dir_all(d).unwrap();
        }
        // 1 MiB and a byte, counted as 2 MiB, twice: 4 MiB of the disk's size.
        std::fs::write(io.join(CHECKOUT), vec![0; (1 << 20) + 1]).unwrap();
        let job = JobDef {
            verified_sha: false,
            paths: Vec::new(),
            name: "j".into(),
            image: "img:dev".into(),
            commands: vec![],
            timeout_secs: None,
            artifacts: vec![],
            cache: None,
        };
        let (_cancel, cancel) = watch::channel(false);
        let cache_disk = root.join("cache-disk.raw");
        let running = tokio::spawn(async move {
            let job = JobRun {
                attempt_id: "a1",
                peak_memory_mib: &mut None,
                job: &job,
                image_lock,
                cpus: 1,
                memory_mib: 256,
                work_quota_mib: 2048,
                timeout_secs: 60,
                io: &io,
                cache_disk: &cache_disk,
                tree: &Default::default(),
                cache_data: None,
                exact_cache: false,
                kept_data: &mut None,
                save_cache: true,
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
        // Timing is always logged. Memory is tested separately with supplied
        // samples: this short-lived fake may exit before `ps` sees it.
        let mut text = String::new();
        while let Ok(bytes) = logs.try_recv() {
            text.push_str(&String::from_utf8_lossy(&bytes));
        }
        for line in [
            "[branvil] /work prepared in ",
            "[branvil] sandbox started in ",
            "[branvil] commands ran for ",
            "[branvil] sandbox removed in ",
        ] {
            assert!(text.contains(line), "{line} in {text}");
        }
        // root prepared /work, a disk of the job's own sized for the quota
        // and the archives, from the transfer directory. The job ran as the
        // fixed user, on that disk, with its log directory presented as that
        // user's and its /tmp on a disk volume of its own, and never saw the
        // transfer directory. Nothing was collected (no artifacts, no cache);
        // the volumes are removed after the sandboxes.
        let args = |name: &str| std::fs::read_to_string(state.join(format!("args-{name}")));
        let has = |args: &str, flag: &str, value: &str| {
            let args: Vec<&str> = args.lines().collect();
            args.windows(2).any(|w| w == [flag, value])
        };
        let disk = "bv-work-t-a1:/work:kind=disk,size=2432M";
        // The runner made that disk before the VMs.
        assert_eq!(
            std::fs::read_to_string(state.join("created")).unwrap(),
            "volume create bv-work-t-a1 --kind disk --size 2432M -q\n"
        );
        let io_mount = format!("{}:/.branvil-io:quota=1M", root.join("io").display());
        let prep = args("bv-prep-a1").unwrap();
        for (flag, value) in [
            ("--user", "0:0"),
            ("--mount-named", disk),
            ("--mount-dir", &io_mount),
        ] {
            assert!(has(&prep, flag, value), "{flag} {value} in {prep}");
        }
        // Its writes reach the disk before its VM is removed.
        assert!(prep.contains("chown -R 1000:1000 /work\nsync\n"), "{prep}");
        let ran = args("bv-job-a1").unwrap();
        for (flag, value) in [
            ("--user", "1000:1000"),
            ("--mount-named", disk),
            (
                "--mount-dir",
                &format!(
                    "{}:/.branvil-log:uid=1000,gid=1000,quota=2048M",
                    root.join("log").display()
                ),
            ),
            ("--mount-named", "bv-tmp-t-a1:/tmp:kind=disk,size=4G"),
        ] {
            assert!(has(&ran, flag, value), "{flag} {value} in {ran}");
        }
        assert!(!ran.contains("/.branvil-io"), "{ran}");
        // Its only directory mount is the log's, with a quota.
        assert_eq!(ran.matches("--mount-dir").count(), 1, "{ran}");
        assert!(args("bv-collect-a1").is_err());
        assert_eq!(
            std::fs::read_to_string(state.join("removed")).unwrap(),
            "remove --force -q bv-prep-a1\nremove --force -q bv-job-a1\nremove --force -q --label branvil.job=a1\nvolume remove -q bv-tmp-t-a1 bv-work-t-a1\n"
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
            format!("bv-work-t-{id}"),
            format!("bv-work-t-{id}-new"),
            format!("bv-cache-t-{id}"),
            format!("bv-tmp-t2-{id}"),
            format!("bv-cache-t2-{id}"),
            format!("bv-work-t2-{id}-new"),
            format!("bv-work-t2-{id}"),
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
            format!(
                "remove --force -q --label branvil.runner=t\nvolume remove -q bv-tmp-t-{id} bv-work-t-{id} bv-work-t-{id}-new bv-cache-t-{id}\n"
            )
        );
    }

    /// The first 2 KiB of the image `path` (its first bytes and superblock),
    /// read without reading the rest: images are sparse and large.
    fn head(path: &Path) -> Vec<u8> {
        use std::io::Read;
        let mut head = Vec::new();
        std::fs::File::open(path)
            .unwrap()
            .take(2048)
            .read_to_end(&mut head)
            .unwrap();
        head
    }

    /// An image whose superblock says it holds an ext4 file system of
    /// `fs_mib` MiB, and whose first bytes are `head`.
    fn ext4_image(path: &Path, fs_mib: u64, head: &[u8]) {
        use std::io::{Seek, SeekFrom, Write};
        let mut sb = [0u8; 1024];
        let blocks = u32::try_from((fs_mib << 20) / 4096).unwrap();
        sb[0x04..0x08].copy_from_slice(&blocks.to_le_bytes());
        sb[0x18..0x1c].copy_from_slice(&2u32.to_le_bytes());
        sb[0x38..0x3a].copy_from_slice(&0xEF53u16.to_le_bytes());
        let mut f = std::fs::File::create(path).unwrap();
        f.write_all(head).unwrap();
        f.seek(SeekFrom::Start(1024)).unwrap();
        f.write_all(&sb).unwrap();
        f.set_len(fs_mib << 20).unwrap();
    }

    /// A job that asks for `out/` and a cache of `target`, with the
    /// artifacts' archive and the mark of a disk kept for the cache already
    /// written under `state/returned`.
    fn job_with_returned_archives(state: &std::path::Path) -> JobDef {
        let mut tar = tar::Builder::new(Vec::new());
        let mut h = tar::Header::new_gnu();
        h.set_size(2);
        h.set_mode(0o644);
        tar.append_data(&mut h, "out/report.txt", &b"ok"[..])
            .unwrap();
        std::fs::write(
            state.join("returned").join(ARTIFACTS),
            tar.into_inner().unwrap(),
        )
        .unwrap();
        std::fs::write(state.join("returned").join(CACHE_KEPT), "1024\n").unwrap();
        JobDef {
            verified_sha: false,
            paths: Vec::new(),
            name: "j".into(),
            image: "img:dev".into(),
            commands: vec![],
            timeout_secs: None,
            artifacts: vec![crate::ci::pipeline::ArtifactDef {
                name: "report".into(),
                paths: vec!["out/".into()],
            }],
            cache: Some(crate::ci::pipeline::CacheDef {
                paths: vec!["target".into()],
                key_files: vec![],
            }),
        }
    }

    /// `/work` starts from the cache entry's image it was given, grown to the
    /// disk's size, and is collected once the job's VM ended, by exit or by
    /// timeout, never after a stop: the artifacts' paths, and the cached
    /// paths when a successful job needs a new cache entry. The host stages
    /// the new disk by link, independently of artifact collection.
    #[cfg(unix)]
    #[tokio::test]
    async fn work_is_collected_after_the_jobs_vm_ended() {
        use crate::runner::workspace::{Cache, CacheOwner};
        use std::os::unix::fs::MetadataExt;

        let dir = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(dir.path()).unwrap();
        let msb = root.join("msb");
        write_script(&msb, FAKE_MSB);
        let home = root.join("home");
        let state = home.join("fake");
        std::fs::create_dir_all(state.join("returned")).unwrap();
        std::fs::write(state.join("tag"), format!("sha256:{}", "a".repeat(64))).unwrap();
        std::fs::write(state.join("volumes"), "[]").unwrap();
        ext4_image(&state.join("cache-image"), 128, b"collected");
        let job = job_with_returned_archives(&state);
        // A job that declares no artifacts collects nothing after a timeout.
        let bare = JobDef {
            artifacts: vec![],
            ..job.clone()
        };
        for (ending, collected, created_cache, mounted_cache, kept) in [
            ("fallback", true, true, true, true),
            ("miss", true, true, true, true),
            ("pending-only", true, true, true, true),
            ("exact", true, false, false, false),
            ("exact-invalid", true, true, true, true),
            ("exact-dropped", true, true, true, true),
            ("exact-prep-fails", true, true, true, true),
            ("no-cache-mark", true, true, true, false),
            ("cache-inspect-fails", true, true, false, false),
            ("link-fails", true, true, true, false),
            ("collect-fails", true, true, true, false),
            ("collect-hangs", true, true, true, false),
            ("job-fails", true, false, false, false),
            ("timeout", true, false, false, false),
            ("timeout-bare", false, false, false, false),
            ("timeout-unflushed", true, false, false, false),
            ("stop-while-flushing", false, false, false, false),
            ("stop-while-locked", false, true, false, false),
            ("stop-while-locked-bare", false, true, false, false),
            ("stop", false, false, false, false),
        ] {
            let at = root.join(ending);
            let (work, log_dir, io) = (at.join("work"), at.join("log"), at.join("io"));
            for d in [&work, &log_dir, &io] {
                std::fs::create_dir_all(d).unwrap();
            }
            std::fs::write(
                io.join(CHECKOUT),
                tar::Builder::new(Vec::new()).into_inner().unwrap(),
            )
            .unwrap();
            let cache_disk = at.join("cache-disk.raw");
            let cache = Cache::new(at.join("cache"));
            let owner = CacheOwner {
                repo: "r".into(),
                job: job.name.clone(),
                user: JOB_USER_ID,
                paths: vec!["target".into()],
            };
            let max = 1 << 30;
            if ending != "miss" {
                let seed = at.join("seed.raw");
                if ending == "exact-invalid" {
                    std::fs::write(&seed, "not ext4").unwrap();
                } else {
                    ext4_image(&seed, 128, b"restored");
                }
                let key = if ending.starts_with("exact") || ending == "pending-only" {
                    "requested"
                } else {
                    "older"
                };
                assert!(
                    cache
                        .stage("seed", key, &owner, (&seed, 1024), max)
                        .unwrap()
                );
                if ending != "pending-only" {
                    cache
                        .settle("seed", Some(std::time::UNIX_EPOCH), max)
                        .unwrap();
                }
            }
            let restored = cache
                .restore_generation("requested", &owner, &cache_disk)
                .unwrap();
            let exact_cache = restored.as_ref().is_some_and(|(key, _)| key == "requested");
            let cache_data = restored.as_ref().map(|(_, bytes)| *bytes);
            let head_of_entry = cache_disk.is_file().then(|| head(&cache_disk));
            // What an earlier VM with `io` left there counts for nothing.
            std::fs::write(io.join(CACHE_KEPT), "999").unwrap();
            let _ = std::fs::remove_file(state.join("returned").join(CACHE_KEPT));
            if ending != "no-cache-mark" {
                std::fs::write(state.join("returned").join(CACHE_KEPT), "1024\n").unwrap();
            }
            std::fs::write(work.join("README"), "checkout").unwrap();
            for file in [
                "done",
                "args-bv-collect-a1",
                "removed",
                "created",
                "cache-witness",
                "collect-fails",
                "collect-hangs",
                "cache-inspect-fails",
                "link-fails",
                "job-fails",
                "exec-hangs",
                "exec-fails",
            ] {
                let _ = std::fs::remove_file(state.join(file));
            }
            if ending == "timeout-unflushed" {
                std::fs::write(state.join("exec-fails"), "").unwrap();
            }
            if ending == "stop-while-flushing" {
                std::fs::write(state.join("exec-hangs"), "").unwrap();
            }
            if !ending.starts_with("timeout") && !matches!(ending, "stop" | "stop-while-flushing") {
                std::fs::write(state.join("done"), "").unwrap();
            }
            let flag = match ending {
                "exact-dropped" => Some("prep-drops"),
                "exact-prep-fails" => Some("prep-fails"),
                "cache-inspect-fails"
                | "link-fails"
                | "collect-fails"
                | "collect-hangs"
                | "job-fails" => Some(ending),
                _ => None,
            };
            if let Some(flag) = flag {
                std::fs::write(state.join(flag), "").unwrap();
            }
            let (stop, cancel) = watch::channel(false);
            let (log, mut logs) = mpsc::channel(256);
            // A load holds the image cache when the job's /work would be
            // collected.
            let load = if ending.starts_with("stop-while-locked") {
                let service = Microsandbox::new(Msb::Found(msb.clone()), home.clone(), "service");
                Some(service.lock_images(true, || {}).await.unwrap())
            } else {
                None
            };
            let runner = Microsandbox::new(Msb::Found(msb.clone()), home.clone(), "t");
            let job = if ending.ends_with("bare") {
                &bare
            } else {
                &job
            };
            let (work_dir, io_dir, kept_disk) = (work.clone(), io.clone(), cache_disk.clone());
            let running = async move {
                let mut kept = None;
                let outcome = runner
                    .run(
                        JobRun {
                            attempt_id: "a1",
                            peak_memory_mib: &mut None,
                            job,
                            image_lock: ImageLock::default(),
                            cpus: 1,
                            memory_mib: 256,
                            work_quota_mib: 2048,
                            timeout_secs: if ending.starts_with("timeout")
                                || ending == "stop-while-flushing"
                            {
                                1
                            } else {
                                60
                            },
                            io: &io_dir,
                            cache_disk: &kept_disk,
                            tree: &Default::default(),
                            cache_data,
                            exact_cache,
                            kept_data: &mut kept,
                            save_cache: true,
                            work: &work_dir,
                            log_dir: &log_dir,
                            network: false,
                            env: vec![],
                        },
                        log,
                        cancel,
                    )
                    .await;
                (outcome, kept)
            };
            let removed_file = state.join("removed");
            let stopping = async {
                if ending == "stop" {
                    tokio::time::sleep(Duration::from_millis(500)).await;
                    stop.send(true).unwrap();
                }
                if ending.starts_with("stop-while-locked") {
                    // The job's VM is removed right before /work is collected.
                    while !std::fs::read_to_string(&removed_file)
                        .is_ok_and(|r| r.contains("remove --force -q bv-job-a1"))
                    {
                        tokio::time::sleep(Duration::from_millis(50)).await;
                    }
                    stop.send(true).unwrap();
                }
                if ending == "stop-while-flushing" {
                    // Stopped while the timeout's flush runs.
                    while !std::fs::read_to_string(&removed_file).is_ok_and(|r| r.contains("exec"))
                    {
                        tokio::time::sleep(Duration::from_millis(50)).await;
                    }
                    stop.send(true).unwrap();
                }
                if ending == "collect-hangs" {
                    while !io.join(ARTIFACTS).is_file() {
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                    stop.send(true).unwrap();
                }
            };
            let started = Instant::now();
            let ((outcome, kept_data), ()) = tokio::join!(running, stopping);
            drop(load);
            let _ = std::fs::remove_file(state.join("exec-hangs"));
            assert_eq!(
                outcome,
                match ending {
                    // A stop while only the cache is collected leaves the
                    // job's end as it was.
                    "job-fails" => Outcome::Exited(1),
                    "timeout" | "timeout-bare" | "timeout-unflushed" => Outcome::TimedOut,
                    "collect-hangs" | "stop-while-flushing" | "stop-while-locked" | "stop" =>
                        Outcome::Cancelled,
                    _ => Outcome::Exited(0),
                }
            );
            // A stop waits neither for the flush nor for the load.
            assert!(started.elapsed() < TIMEOUT_FLUSH / 3, "{ending}");
            // A flush that fails is said.
            let mut text = String::new();
            while let Ok(bytes) = logs.try_recv() {
                text.push_str(&String::from_utf8_lossy(&bytes));
            }
            assert_eq!(
                text.contains("flushing the job at its timeout failed"),
                ending == "timeout-unflushed",
                "{ending}: {text}"
            );
            // The cache source for /work and the disk it returns are distinct.
            let disk = state.join("disks/bv-work-t-a1");
            let prep = std::fs::read_to_string(state.join("args-bv-prep-a1")).unwrap();
            assert!(!prep.contains(CACHE_MOUNT), "{prep}");
            let used_image = restored.is_some() && ending != "exact-invalid";
            assert_eq!(prep.contains("resize2fs"), used_image, "{prep}");
            if used_image {
                assert_eq!(
                    std::fs::metadata(&disk).unwrap().len(),
                    2432 << 20,
                    "{ending}"
                );
                assert_eq!(Some(head(&disk)), head_of_entry, "{ending}");
            }
            for message in [
                ("exact", "cache save skipped: /work used the exact key"),
                (
                    "no-cache-mark",
                    "cache save skipped: copying the cached paths",
                ),
                (
                    "cache-inspect-fails",
                    "cache save skipped: creating the cache volume failed",
                ),
                (
                    "link-fails",
                    "cache save skipped: keeping the cache image failed",
                ),
            ] {
                assert_eq!(
                    text.contains(message.1),
                    ending == message.0,
                    "{ending}: {text}"
                );
            }
            assert_eq!(cache_disk.is_file(), kept, "{ending}");
            assert_eq!(kept_data, kept.then_some(1 << 20), "{ending}");
            if let Some(data) = kept_data {
                let witness = std::fs::metadata(state.join("cache-witness")).unwrap();
                assert_eq!(std::fs::metadata(&cache_disk).unwrap().ino(), witness.ino());
                assert_eq!(&head(&cache_disk)[..9], b"collected");
                assert!(
                    cache
                        .stage("a1", "requested", &owner, (&cache_disk, data), max)
                        .unwrap()
                );
                cache
                    .settle("a1", Some(std::time::SystemTime::now()), max)
                    .unwrap();
                assert_eq!(
                    cache
                        .restore_generation("requested", &owner, &cache_disk)
                        .unwrap(),
                    Some(("requested".into(), data))
                );
                assert_eq!(
                    super::super::disk::ext4_bytes(&cache_disk).unwrap(),
                    128 << 20
                );
                assert_eq!(&head(&cache_disk)[..9], b"collected");
            } else {
                assert!(
                    !cache.pending().unwrap().iter().any(|id| id == "a1"),
                    "{ending}"
                );
            }
            let created = std::fs::read_to_string(state.join("created")).unwrap();
            let create = "volume create bv-cache-t-a1 --kind disk --size 2432M -q";
            assert_eq!(
                created.lines().any(|line| line == create),
                created_cache,
                "{ending}: {created}"
            );
            // A job stopped at its timeout is flushed before it is killed
            // when it declares artifacts: they are collected from what
            // reached the disk. One that exits flushes itself; a cancelled
            // one, and one with nothing to collect, are not flushed.
            let flushed = ending == "timeout" || ending == "stop-while-flushing";
            let removed = std::fs::read_to_string(&removed_file).unwrap();
            assert_eq!(
                removed
                    .lines()
                    .any(|l| l.starts_with("volume remove") && l.contains("bv-cache-t-a1")),
                created_cache,
                "{ending}: {removed}"
            );
            assert!(!state.join("disks/bv-cache-t-a1").exists(), "{ending}");
            assert_eq!(
                removed.contains("\nexec bv-job-a1 -- sync\nremove --force -q bv-job-a1\n"),
                flushed,
                "{ending}: {removed}"
            );
            assert_eq!(removed.contains("exec"), flushed, "{ending}: {removed}");
            let script = std::fs::read_to_string(state.join("args-bv-collect-a1"));
            assert_eq!(script.is_ok(), collected, "{ending}");
            if let Ok(script) = script {
                // It may add to `io` what `/work` holds.
                let io_mount = format!("{}:/.branvil-io:quota=2432M", io.display());
                assert!(script.lines().any(|l| l == io_mount), "{script}");
                assert!(script.contains("'out/'"), "{script}");
                assert_eq!(script.contains("'target'"), mounted_cache, "{script}");
                assert_eq!(
                    script.contains(
                        "--mount-named\nbv-cache-t-a1:/.branvil-cache:kind=disk,size=2432M"
                    ),
                    mounted_cache,
                    "{script}"
                );
                assert!(!script.contains("--user\n1000"), "{script}");
                assert!(script.contains("\nset -e\ncd /work\n"), "{script}");
                // Nothing of the checkout is taken for an artifact.
                assert!(!work.join("README").exists(), "{ending}");
                // The artifacts' archive is named only once it was packed
                // whole: a collection that failed after it (the cache's
                // steps) leaves it, and the disk is not kept for the cache.
                assert_eq!(
                    std::fs::read_to_string(work.join("out/report.txt")).unwrap(),
                    "ok",
                    "{ending}"
                );
            } else if ending == "stop-while-locked" {
                // Its artifacts were asked for: nothing of the checkout is
                // left, and nothing was collected.
                assert!(!work.join("README").exists(), "{ending}");
                assert!(!work.join("out").exists(), "{ending}");
            } else {
                assert!(work.join("README").exists(), "{ending}");
            }
        }
    }

    /// A `/work` that cannot be made ends the attempt before any VM, its
    /// volume removed and the cache entry it was given gone; an entry larger
    /// than the disk the quota gives (saved when it held more, or the
    /// checkout was larger) is used, on a disk of its size; one for a cached
    /// path through a link of the commit is not used; an entry the preparing VM
    /// drops, or fails on, takes its disk with it: the job gets a new, empty
    /// one of its own name.
    #[cfg(unix)]
    #[tokio::test]
    async fn how_work_starts_from_the_disk_and_the_entry_it_is_given() {
        let dir = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(dir.path()).unwrap();
        let msb = root.join("msb");
        write_script(&msb, FAKE_MSB);
        let home = root.join("home");
        let state = home.join("fake");
        std::fs::create_dir_all(state.join("returned")).unwrap();
        std::fs::write(state.join("tag"), format!("sha256:{}", "a".repeat(64))).unwrap();
        std::fs::write(
            state.join("volumes"),
            r#"[{"name": "bv-work-t-a1"}, {"name": "bv-work-t-a1-new"}]"#,
        )
        .unwrap();
        std::fs::write(state.join("done"), "").unwrap();
        let job = job_with_returned_archives(&state);
        for case in [
            "inspect-fails",
            "larger",
            "dropped",
            "prep-fails",
            "commit-link",
        ] {
            let at = root.join(case);
            let (work, log_dir, io) = (at.join("work"), at.join("log"), at.join("io"));
            for d in [&work, &log_dir, &io] {
                std::fs::create_dir_all(d).unwrap();
            }
            std::fs::write(
                io.join(CHECKOUT),
                tar::Builder::new(Vec::new()).into_inner().unwrap(),
            )
            .unwrap();
            let cache_disk = at.join("cache-disk.raw");
            ext4_image(&cache_disk, 4096, b"restored");
            let _ = std::fs::remove_file(state.join("removed"));
            let _ = std::fs::remove_file(state.join("created"));
            let _ = std::fs::remove_file(state.join("args-bv-prep-a1"));
            let _ = std::fs::remove_file(state.join("args-bv-prep-new-a1"));
            let _ = std::fs::remove_file(state.join("args-bv-collect-a1"));
            if case == "commit-link" {
                ext4_image(&cache_disk, 128, b"restored");
            }
            if case == "dropped" || case == "prep-fails" {
                ext4_image(&cache_disk, 512, b"restored");
                let mark = if case == "dropped" {
                    "prep-drops"
                } else {
                    "prep-fails"
                };
                std::fs::write(state.join(mark), "").unwrap();
            }
            if case == "inspect-fails" {
                std::fs::write(state.join("inspect-fails"), "").unwrap();
            } else {
                let _ = std::fs::remove_file(state.join("inspect-fails"));
            }
            let (log, mut logs) = mpsc::channel(64);
            let (_stop, cancel) = watch::channel(false);
            let runner = Microsandbox::new(Msb::Found(msb.clone()), home.clone(), "t");
            let outcome = runner
                .run(
                    JobRun {
                        attempt_id: "a1",
                        peak_memory_mib: &mut None,
                        job: &job,
                        image_lock: ImageLock::default(),
                        cpus: 1,
                        memory_mib: 256,
                        work_quota_mib: 2048,
                        timeout_secs: 60,
                        io: &io,
                        cache_disk: &cache_disk,
                        tree: &super::super::workspace::Tree::from_index(
                            if case == "commit-link" {
                                // A link of the commit, which the host may not
                                // have made one.
                                &b"120000 aa 0\ttarget\0"[..]
                            } else {
                                // A directory of the commit below a cached path.
                                &b"100644 bb 0\ttarget/debug/x\0"[..]
                            },
                        )
                        .unwrap(),
                        // What the entry says its file system holds counts, though the
                        // file does not take it: 2306 MiB and 200 MiB, rounded up.
                        cache_data: matches!(case, "dropped" | "prep-fails" | "commit-link")
                            .then_some(200 << 20),
                        exact_cache: false,
                        kept_data: &mut None,
                        save_cache: case == "commit-link",
                        work: &work,
                        log_dir: &log_dir,
                        network: false,
                        env: vec![],
                    },
                    log,
                    cancel,
                )
                .await;
            let mut text = String::new();
            while let Ok(bytes) = logs.try_recv() {
                text.push_str(&String::from_utf8_lossy(&bytes));
            }
            let removed = std::fs::read_to_string(state.join("removed")).unwrap_or_default();
            assert!(
                removed.contains("volume remove -q bv-work-t-a1"),
                "{case}: {removed}"
            );
            // The new one too, whichever exists, once the job ran.
            assert!(
                case == "inspect-fails"
                    || removed
                        .lines()
                        .last()
                        .is_some_and(|l| l.ends_with("bv-work-t-a1 bv-work-t-a1-new")),
                "{case}: {removed}"
            );
            // Nothing is left for the cache: the entry was used or dropped,
            // and a run that does not save keeps nothing.
            assert!(!cache_disk.exists(), "{case}");
            if case == "inspect-fails" {
                assert!(
                    matches!(&outcome, Outcome::StartupFailure(m) if m.starts_with("Creating /work failed")),
                    "{outcome:?}"
                );
                assert!(!state.join("args-bv-prep-a1").exists());
            } else if case == "dropped" || case == "prep-fails" {
                assert_eq!(outcome, Outcome::Exited(0));
                // Each says why, and that the job runs without the entry.
                let why = if case == "dropped" {
                    "the cache entry was not kept ("
                } else {
                    "preparing /work from the cache entry failed ("
                };
                assert!(text.contains(why), "{text}");
                assert_eq!(text.matches("the job runs without it").count(), 1, "{text}");
                // Removed before the job, and made again; the job's disk is
                // the new one, prepared without the entry.
                let at = |line: &str| removed.find(line).unwrap();
                assert!(at("volume remove -q bv-work-t-a1") < at("remove --force -q bv-job-a1"));
                let created = std::fs::read_to_string(state.join("created")).unwrap();
                assert!(
                    created.contains("volume create bv-work-t-a1 --kind disk --size 2560M"),
                    "{created}"
                );
                assert!(
                    created.contains("volume create bv-work-t-a1-new "),
                    "{created}"
                );
                let new = state.join("disks/bv-work-t-a1-new");
                assert_eq!(std::fs::metadata(new).unwrap().len(), 0);
                let first = std::fs::read_to_string(state.join("args-bv-prep-a1")).unwrap();
                // The checkout's archive was checked against the entry's
                // links before it was unpacked.
                let checked = first.find(MEMBERS).unwrap();
                assert!(first.contains("find . -type l"), "{first}");
                assert!(checked < first.find("tar -xf").unwrap(), "{first}");
                // The runner listed the members for it.
                assert!(io.join(MEMBERS).is_file());
                assert!(
                    first.contains("bv-work-t-a1:/work:kind=disk,size=2560M"),
                    "{first}"
                );
                let prep = std::fs::read_to_string(state.join("args-bv-prep-new-a1")).unwrap();
                assert!(!prep.contains("resize2fs"), "{prep}");
                assert!(
                    removed.contains("remove --force -q bv-prep-new-a1"),
                    "{removed}"
                );
                let ran = std::fs::read_to_string(state.join("args-bv-job-a1")).unwrap();
                assert!(ran.contains("bv-work-t-a1-new:/work:kind=disk"), "{ran}");
            } else if case == "commit-link" {
                assert_eq!(outcome, Outcome::Exited(0));
                assert!(
                    text.contains(
                        "the cached path target goes through a symbolic link of the commit"
                    ),
                    "{text}"
                );
                assert_eq!(text.matches("the job runs without").count(), 1, "{text}");
                // As if there were no entry: the disk the quota gives, not
                // grown for the entry, prepared once without it.
                let created = std::fs::read_to_string(state.join("created")).unwrap();
                assert_eq!(
                    created,
                    "volume create bv-work-t-a1 --kind disk --size 2432M -q\n"
                );
                let disk = state.join("disks/bv-work-t-a1");
                assert_eq!(std::fs::metadata(disk).unwrap().len(), 0);
                let prep = std::fs::read_to_string(state.join("args-bv-prep-a1")).unwrap();
                assert!(
                    !prep.contains("resize2fs") && !prep.contains(CACHE_DROPPED),
                    "{prep}"
                );
                assert!(!state.join("args-bv-prep-new-a1").exists());
                assert!(!cache_disk.exists());
                // Nor is anything kept for the cache: an entry of this commit
                // would never be used.
                let collect = std::fs::read_to_string(state.join("args-bv-collect-a1")).unwrap();
                assert!(!collect.contains("BRANVIL:KEEP"), "{collect}");
            } else {
                // The disk is the entry's size, the entry its content, and its
                // file system fills it: nothing to grow, nothing dropped.
                assert_eq!(outcome, Outcome::Exited(0));
                let created = std::fs::read_to_string(state.join("created")).unwrap();
                assert_eq!(
                    created,
                    "volume create bv-work-t-a1 --kind disk --size 4096M -q\n"
                );
                let disk = state.join("disks/bv-work-t-a1");
                assert_eq!(std::fs::metadata(&disk).unwrap().len(), 4096 << 20);
                assert_eq!(&head(&disk)[..8], b"restored");
                let prep = std::fs::read_to_string(state.join("args-bv-prep-a1")).unwrap();
                assert!(
                    !prep.contains("resize2fs") && prep.contains(MEMBERS),
                    "{prep}"
                );
                assert!(!text.contains("runs without"), "{text}");
                assert!(!state.join("args-bv-prep-new-a1").exists());
            }
        }
    }

    /// A name in the return directory is an archive only as a regular file.
    /// A symbolic link of that name, even one whose target is a regular
    /// file, is logged as `packing <name> failed`. A regular file of that
    /// name is not.
    #[cfg(unix)]
    #[tokio::test]
    async fn a_symbolic_link_is_not_counted_as_a_packed_archive() {
        let dir = tempfile::tempdir().unwrap();
        let root = std::fs::canonicalize(dir.path()).unwrap();
        let msb = root.join("msb");
        write_script(&msb, FAKE_MSB);
        let home = root.join("home");
        let state = home.join("fake");
        std::fs::create_dir_all(state.join("returned")).unwrap();
        std::fs::write(state.join("tag"), format!("sha256:{}", "a".repeat(64))).unwrap();
        std::fs::write(state.join("volumes"), "[]").unwrap();
        std::fs::write(state.join("done"), "").unwrap();
        let job = job_with_returned_archives(&state);
        for link in [false, true] {
            if link {
                std::fs::write(state.join("archive-links"), format!("{ARTIFACTS}\n")).unwrap();
            }
            let at = root.join(if link { "link" } else { "file" });
            let (work, log_dir, io) = (at.join("work"), at.join("log"), at.join("io"));
            for d in [&work, &log_dir, &io] {
                std::fs::create_dir_all(d).unwrap();
            }
            let (log, mut logs) = mpsc::channel(64);
            let (_stop, cancel) = watch::channel(false);
            let runner = Microsandbox::new(Msb::Found(msb.clone()), home.clone(), "t");
            let outcome = runner
                .run(
                    JobRun {
                        attempt_id: "a1",
                        peak_memory_mib: &mut None,
                        job: &job,
                        image_lock: ImageLock::default(),
                        cpus: 1,
                        memory_mib: 256,
                        work_quota_mib: 2048,
                        timeout_secs: 60,
                        io: &io,
                        cache_disk: &at.join("cache-disk.raw"),
                        tree: &Default::default(),
                        cache_data: None,
                        exact_cache: false,
                        kept_data: &mut None,
                        save_cache: true,
                        work: &work,
                        log_dir: &log_dir,
                        network: false,
                        env: vec![],
                    },
                    log,
                    cancel,
                )
                .await;
            assert_eq!(outcome, Outcome::Exited(0), "link={link}");
            let mut text = String::new();
            while let Ok(bytes) = logs.try_recv() {
                text.push_str(&String::from_utf8_lossy(&bytes));
            }
            let line = format!("[branvil] packing {ARTIFACTS} failed\n");
            assert_eq!(text.contains(&line), link, "{line} in {text}");
            let meta = std::fs::symlink_metadata(io.join(ARTIFACTS)).unwrap();
            assert_eq!(meta.file_type().is_symlink(), link);
            assert_eq!(meta.is_file(), !link);
            if link {
                assert!(text.contains("artifacts: unpacking failed:"), "{text}");
                assert!(!work.join("out/report.txt").exists());
            } else {
                assert_eq!(
                    std::fs::read_to_string(work.join("out/report.txt")).unwrap(),
                    "ok"
                );
            }
        }
    }

    /// The script that packs `/work` packs the paths that exist and go
    /// through no link (a link at the end goes as a link), an empty archive
    /// when there is none, and names none it could not write; with the system's
    /// `/bin/sh`, `tar`, and `dd`.
    #[cfg(unix)]
    #[test]
    fn the_pack_script_packs_existing_paths_through_no_link() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let (work, io) = (dir.path().join("work"), dir.path().join("io"));
        for d in ["out/sub", "secret", "it's $here"] {
            std::fs::create_dir_all(work.join(d)).unwrap();
        }
        std::fs::create_dir(&io).unwrap();
        for f in ["out/report.txt", "out/sub/x", "secret/s", "it's $here/f"] {
            std::fs::write(work.join(f), "x").unwrap();
        }
        symlink("secret", work.join("linked")).unwrap();
        symlink("../secret/s", work.join("out/l")).unwrap();
        let io_path = io.to_str().unwrap();
        let pack = |name: &str, paths: &[&str]| {
            let out = std::process::Command::new("/bin/sh")
                .args(["-c", &pack_script(io_path, name, paths)])
                .current_dir(&work)
                // The script runs in a Linux VM; macOS's tar would add an
                // AppleDouble `._` entry for each file's extended attributes.
                .env("COPYFILE_DISABLE", "1")
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
        };
        pack(
            "a.tar",
            &["out/", "linked/s", "it's $here", "missing", "linked/"],
        );
        let mut archive = tar::Archive::new(std::fs::File::open(io.join("a.tar")).unwrap());
        let mut names: Vec<(String, bool)> = archive
            .entries()
            .unwrap()
            .map(|e| e.unwrap())
            .filter(|e| e.header().entry_type() != tar::EntryType::Directory)
            .map(|e| {
                let link = e.header().entry_type() == tar::EntryType::Symlink;
                (e.path().unwrap().display().to_string(), link)
            })
            .collect();
        names.sort();
        assert_eq!(
            names,
            [
                ("./it's $here/f".to_string(), false),
                ("./linked".to_string(), true),
                ("./out/l".to_string(), true),
                ("./out/report.txt".to_string(), false),
                ("./out/sub/x".to_string(), false),
            ]
        );
        assert!(!io.join("a.tar.part").exists());
        pack("e.tar", &["missing"]);
        let mut empty = tar::Archive::new(std::fs::File::open(io.join("e.tar")).unwrap());
        assert_eq!(empty.entries().unwrap().count(), 0);
        // An archive that cannot be written gets no name.
        std::fs::create_dir(io.join("d.tar.part")).unwrap();
        pack("d.tar", &["out/"]);
        assert!(!io.join("d.tar").exists());
    }

    /// The collecting VM is removed once its script ends, and the host keeps
    /// an archive only when it is there by its name: every archive is
    /// flushed by the `dd` that writes it (`conv=fsync`) and named only once
    /// it was packed whole, whatever failed (`tar`, whose status the pipeline
    /// loses; `dd`; a mark); a failure of the script itself (a `/work` that is
    /// not there, a mark it cannot remove) fails it. With the system's
    /// `/bin/sh`, and a `tar` and a `dd` that fail on request (the `dd` noting
    /// its arguments).
    #[cfg(unix)]
    #[test]
    fn the_collect_script_flushes_and_fails_on_any_failure() {
        let dir = tempfile::tempdir().unwrap();
        let (work, io, bin) = (
            dir.path().join("work"),
            dir.path().join("io"),
            dir.path().join("bin"),
        );
        for d in [&work.join("out"), &io, &bin] {
            std::fs::create_dir_all(d).unwrap();
        }
        std::fs::write(work.join("out/report.txt"), "x").unwrap();
        let calls = dir.path().join("dd-calls");
        write_script(
            &bin.join("dd"),
            &format!(
                "#!/bin/sh\necho \"$*\" >> '{}'\n[ ! -e '{}' ] || {{ cat >/dev/null; exit 1; }}\nexec /bin/dd \"$@\"\n",
                calls.display(),
                dir.path().join("dd-fails").display()
            ),
        );
        // A `tar` that fails midway on request.
        write_script(
            &bin.join("tar"),
            &format!(
                "#!/bin/sh\nif [ -e '{}' ]; then printf partial; exit 1; fi\nexec /usr/bin/tar \"$@\"\n",
                dir.path().join("tar-fails").display()
            ),
        );
        let path = format!("{}:{}", bin.display(), std::env::var("PATH").unwrap());
        let collect = |work: &Path, artifacts: &[&str]| {
            let _ = std::fs::remove_file(&calls);
            let _ = std::fs::remove_file(io.join(ARTIFACTS));
            std::process::Command::new("/bin/sh")
                .args([
                    "-c",
                    &collect_script(
                        work.to_str().unwrap(),
                        io.to_str().unwrap(),
                        artifacts,
                        None,
                    ),
                ])
                .env("PATH", &path)
                .env("COPYFILE_DISABLE", "1")
                .output()
                .unwrap()
                .status
                .success()
        };
        // The archive is written and flushed, and so is an empty one.
        let calls_of = || std::fs::read_to_string(&calls).unwrap();
        let named = || io.join(ARTIFACTS).is_file();
        for paths in [&["out/"][..], &["missing"]] {
            assert!(collect(&work, paths));
            assert_eq!(calls_of().lines().count(), 1, "{}", calls_of());
            assert!(calls_of().contains("conv=fsync"), "{}", calls_of());
            assert!(named());
        }
        // A `dd` that fails once it read all (`tar` succeeded), of the empty
        // archive or one of paths, leaves none, and the script goes on.
        std::fs::write(dir.path().join("dd-fails"), "").unwrap();
        for paths in [&["out/"][..], &["missing"]] {
            assert!(collect(&work, paths));
            assert!(!named());
        }
        std::fs::remove_file(dir.path().join("dd-fails")).unwrap();
        // A `tar` that fails midway leaves no archive, though `dd` wrote
        // what it got.
        std::fs::write(dir.path().join("tar-fails"), "").unwrap();
        assert!(collect(&work, &["out/"]));
        assert!(!named());
        std::fs::remove_file(dir.path().join("tar-fails")).unwrap();
        // A mark that cannot be removed fails the script, and names nothing.
        let mark = io.join(format!("{ARTIFACTS}.packed"));
        std::fs::create_dir(&mark).unwrap();
        assert!(!collect(&work, &["out/"]));
        assert!(!named());
        std::fs::remove_dir(&mark).unwrap();
        // `/work` not there: nothing is packed from elsewhere.
        assert!(!collect(&dir.path().join("gone"), &["out/"]));
    }

    #[cfg(unix)]
    #[test]
    fn the_cache_script_streams_only_selected_paths_and_checks_both_tars() {
        use std::os::unix::fs::symlink;
        let dir = tempfile::tempdir().unwrap();
        let (work, io, disk, bin) = (
            dir.path().join("work"),
            dir.path().join("io"),
            dir.path().join("disk"),
            dir.path().join("bin"),
        );
        let cached_files = [
            "target/debug/a.o",
            ".ci/cargo-home/registry/index/i",
            "star*/x/f",
            "it's/f",
        ];
        let other_files = [
            "out/report.txt",
            "src/main.rs",
            "starry/x/f",
            "secret/inner/x",
        ];
        for file in cached_files.into_iter().chain(other_files) {
            let path = work.join(file);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, file).unwrap();
        }
        for d in [&io, &bin] {
            std::fs::create_dir(d).unwrap();
        }
        symlink("secret", work.join("linked")).unwrap();
        symlink("../secret", work.join("target/alias")).unwrap();
        let cached = [
            "target/",
            ".ci/cargo-home/registry",
            "linked/inner",
            "star*/x",
            "it's",
            "missing",
            "missing\nBRANVIL_KEEP\nBRANVIL\nx",
        ];
        // Both faults let tar copy all its bytes before returning an error.
        write_script(
            &bin.join("tar"),
            r#"#!/bin/sh
/usr/bin/tar "$@" || exit $?
case "$1:$3:$TAR_FAIL" in
  -cf:./target:producer | -xpf:-C:extractor) exit 1 ;;
esac
"#,
        );
        for fault in ["producer", "extractor", ""] {
            std::fs::create_dir(&disk).unwrap();
            let out = std::process::Command::new("/bin/sh")
                .args([
                    "-c",
                    &collect_script(
                        work.to_str().unwrap(),
                        io.to_str().unwrap(),
                        &["out/"],
                        Some((disk.to_str().unwrap(), &cached)),
                    ),
                ])
                .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
                .env("TAR_FAIL", fault)
                .env("COPYFILE_DISABLE", "1")
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
            assert!(io.join(ARTIFACTS).is_file());
            assert_eq!(io.join(CACHE_KEPT).is_file(), fault.is_empty(), "{fault}");
            for file in cached_files {
                assert_eq!(std::fs::read_to_string(disk.join(file)).unwrap(), file);
            }
            assert_eq!(
                std::fs::read_link(disk.join("target/alias")).unwrap(),
                Path::new("../secret")
            );
            assert!(!disk.join("linked").exists());
            for file in other_files {
                assert!(!disk.join(file).exists());
                assert_eq!(std::fs::read_to_string(work.join(file)).unwrap(), file);
            }
            assert!(work.join("linked").is_symlink());
            std::fs::remove_dir_all(&disk).unwrap();
        }
        let kib: u64 = std::fs::read_to_string(io.join(CACHE_KEPT))
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        assert!(kib > 0);
    }

    /// `/work` starting from a cache entry's image gets the checkout over the
    /// cached paths once its file system grew to the disk; without
    /// `resize2fs`, the entry is kept only when its file system leaves the
    /// quota; a failed growing, a member of the checkout at or below a link
    /// of the entry (whatever bytes its name holds), or a `find` that fails
    /// drops the entry before anything is unpacked. With the system's
    /// `/bin/sh`, `tar`, `df`, `awk`, and `find`, a `resize2fs` that notes
    /// its argument and succeeds on request, and a `chown` that notes its
    /// arguments.
    #[cfg(unix)]
    #[test]
    fn the_prepare_script_keeps_the_entry_only_on_a_disk_it_fills() {
        let dir = tempfile::tempdir().unwrap();
        let (io, bin) = (dir.path().join("io"), dir.path().join("bin"));
        for d in [&io, &bin] {
            std::fs::create_dir(d).unwrap();
        }
        // The checkout: `src/main.rs` and `more` (a name ending in `/` is a
        // directory, as a submodule's is), and the list of its members the
        // runner writes.
        let checkout = |more: &[String]| {
            let mut tar = tar::Builder::new(Vec::new());
            for name in std::iter::once("src/main.rs").chain(more.iter().map(String::as_str)) {
                let mut h = tar::Header::new_gnu();
                if let Some(dir) = name.strip_suffix('/') {
                    h.set_entry_type(tar::EntryType::Directory);
                    h.set_size(0);
                    h.set_mode(0o755);
                    tar.append_data(&mut h, dir, &b""[..]).unwrap();
                } else {
                    h.set_size(2);
                    h.set_mode(0o644);
                    tar.append_data(&mut h, name, &b"ok"[..]).unwrap();
                }
            }
            std::fs::write(io.join(CHECKOUT), tar.into_inner().unwrap()).unwrap();
            super::super::workspace::list_members(&io.join(CHECKOUT), &io.join(MEMBERS)).unwrap();
        };
        let resized = dir.path().join("resized");
        let tool = bin.join("resize2fs");
        let tool_script = format!(
            "#!/bin/sh\necho \"$1\" >> '{}'\n[ ! -e '{}' ]\n",
            resized.display(),
            dir.path().join("resize-fails").display()
        );
        write_script(
            &bin.join("chown"),
            &format!(
                "#!/bin/sh\necho \"$*\" >> '{}'\n",
                dir.path().join("chowned").display()
            ),
        );
        let prepare = |name: &str, restore: Option<Restore>, resize2fs: bool| {
            // The entry: cached paths, one of them a link to another, and a
            // link inside the Git metadata.
            let work = dir.path().join(name);
            std::fs::create_dir_all(work.join("target/debug")).unwrap();
            std::fs::write(work.join("target/debug/a.o"), "cached").unwrap();
            std::os::unix::fs::symlink("target", work.join("target-link")).unwrap();
            std::fs::create_dir_all(work.join(".git")).unwrap();
            std::os::unix::fs::symlink("../target", work.join(".git/hooks")).unwrap();
            // Links whose names `tar -t` would escape, and one where the
            // commit has a directory member.
            if name == "newline-link" {
                std::os::unix::fs::symlink("/tmp", work.join("p\nq")).unwrap();
            }
            if name == "newline-parent" {
                std::fs::create_dir(work.join("p\nq")).unwrap();
                std::os::unix::fs::symlink("/tmp", work.join("p\nq/l")).unwrap();
            }
            for link in ["build/日本", "a\\b", "deps/module"] {
                let at = work.join(link);
                std::fs::create_dir_all(at.parent().unwrap()).unwrap();
                std::os::unix::fs::symlink("/tmp", at).unwrap();
            }
            let _ = std::fs::remove_file(&tool);
            if resize2fs {
                write_script(&tool, &tool_script);
            }
            let out = std::process::Command::new("/bin/sh")
                .args([
                    "-c",
                    &prepare_script(
                        work.to_str().unwrap(),
                        io.to_str().unwrap(),
                        restore.as_ref(),
                    ),
                ])
                .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
                .output()
                .unwrap();
            assert!(
                out.status.success(),
                "{name}: {}",
                String::from_utf8_lossy(&out.stderr)
            );
            let marks =
                [CACHE_UNGROWN, CACHE_DROPPED].map(|m| std::fs::remove_file(io.join(m)).is_ok());
            (work.join("target/debug/a.o").exists(), marks, work)
        };
        let grows = |fits| {
            Some(Restore {
                grow: true,
                ungrown_fits: fits,
                fs_mib: 128,
            })
        };
        // Many tracked directories, none below a link of the entry.
        checkout(
            &(0..4000)
                .map(|n| format!("vendor/d{n}/f"))
                .collect::<Vec<_>>(),
        );
        // Grown: the cached paths stay under the checkout, the entry's own
        // links among them; the device is the file system's.
        let (kept, marks, work) = prepare("grown", grows(false), true);
        assert!(kept && marks == [false, false]);
        assert!(std::fs::read_link(work.join("target-link")).unwrap() == Path::new("target"));
        assert_eq!(
            std::fs::read_to_string(work.join("src/main.rs")).unwrap(),
            "ok"
        );
        assert_eq!(
            std::fs::read_to_string(&resized).unwrap().lines().count(),
            1
        );
        assert!(
            std::fs::read_to_string(dir.path().join("chowned"))
                .unwrap()
                .contains(&format!("-R 1000:1000 {}", work.display()))
        );
        // No growing is needed: no resize2fs either.
        let (kept, marks, _) = prepare(
            "full",
            Some(Restore {
                grow: false,
                ungrown_fits: false,
                fs_mib: 128,
            }),
            false,
        );
        assert!(kept && marks == [false, false]);
        // No resize2fs: kept as it is when that leaves the quota, said.
        let (kept, marks, _) = prepare("fits", grows(true), false);
        assert!(kept && marks == [true, false]);
        // Otherwise, when growing fails, and when a member of the checkout
        // is below a link of the entry (which unpacking would write
        // through; the Git metadata the archive carries too), it stops
        // there and says so, before anything is unpacked: that disk never
        // goes to the job's user (the runner prepares a new one).
        let chowned = || std::fs::read_to_string(dir.path().join("chowned")).unwrap();
        for (name, resize2fs, member) in [
            ("small", false, None),
            ("failing", true, None),
            ("through", true, Some("target-link/x")),
            ("through-git", true, Some(".git/hooks/pre-commit")),
            ("through-unicode", true, Some("build/日本/a.txt")),
            ("through-backslash", true, Some("a\\b/x")),
            ("at-a-link", true, Some("deps/module/")),
            ("no-find", true, None),
            ("newline-link", true, None),
            ("newline-parent", true, None),
            ("no-path-find", true, None),
        ] {
            if name == "failing" {
                std::fs::write(dir.path().join("resize-fails"), "").unwrap();
            }
            // A `find` that fails lists no link.
            if name == "no-find" {
                write_script(&bin.join("find"), "#!/bin/sh\nexit 1\n");
            }
            // One that fails only to look for newlines.
            if name == "no-path-find" {
                write_script(
                    &bin.join("find"),
                    "#!/bin/sh\ncase \"$*\" in *-path*) exit 1 ;; esac\nexec /usr/bin/find \"$@\"\n",
                );
            }
            checkout(&member.map(String::from).into_iter().collect::<Vec<_>>());
            let (_, marks, work) = prepare(name, grows(false), resize2fs);
            let _ = std::fs::remove_file(bin.join("find"));
            assert!(!work.join("src").exists(), "{name}");
            let _ = std::fs::remove_file(dir.path().join("resize-fails"));
            assert_eq!(marks, [false, true], "{name}");
            assert!(!chowned().contains(&work.display().to_string()), "{name}");
        }
        // Without an entry, the checkout is unpacked as it is.
        checkout(&[]);
        let (kept, marks, work) = prepare("plain", None, false);
        assert!(kept && marks == [false, false]);
        assert!(work.join("src/main.rs").exists());
    }

    /// A pull, and a load's pin, name the version the tag now points to by
    /// the manifest digest, unless it has that name already; when they
    /// cannot (no such tag, a digest name, a failed pin), the transfer still
    /// succeeds. The versions no tag points to
    /// anymore go; one a tag points to stays, and one msb refuses (a job runs
    /// on it) waits for the next time. The pins are no images a job can name.
    #[cfg(unix)]
    #[tokio::test]
    async fn loads_and_pulls_pin_their_version_and_replaced_versions_go() {
        let dir = tempfile::tempdir().unwrap();
        let msb = dir.path().join("msb");
        write_script(
            &msb,
            r#"#!/bin/sh
state="$MSB_HOME/fake"
echo "$*" >> "$state/calls"
case "$1 $2" in
"image list") cat "$state/list" ;;
"image inspect")
  case "$5" in missing:dev) exit 1 ;; *@*) grep -q "\"$5\"" "$state/list" || exit 1 ;; esac
  printf '{"reference":"%s","digest":"sha256:new","config":{"digest":"sha256:c"}}' "$5" ;;
"image remove") [ "$4" != "reg:5000/img@sha256:busy" ] ;;
"pull --quiet") [ "$3" != "offline@sha256:new" ] ;;
esac
"#,
        );
        let home = dir.path().join("home");
        let state = home.join("fake");
        std::fs::create_dir_all(&state).unwrap();
        let listed = serde_json::json!([
            {"reference": "reg:5000/img:dev", "digest": "sha256:new"},
            {"reference": "reg:5000/img@sha256:new", "digest": "sha256:new"},
            {"reference": "reg:5000/img@sha256:old", "digest": "sha256:old"},
            {"reference": "reg:5000/img@sha256:busy", "digest": "sha256:busy"},
            {"reference": "alpine:3.20", "digest": "sha256:alpine"},
            {"reference": "docker.io/library/alpine@sha256:alpine", "digest": "sha256:alpine"},
        ]);
        std::fs::write(state.join("list"), listed.to_string()).unwrap();
        let ms = Microsandbox::new(Msb::Found(msb), home, "t");
        let archive = dir.path().join("a.tar");

        ms.pin("reg:5000/img:dev", Some(&archive)).await;
        ms.pull("alpine:3.20").await.unwrap();
        ms.pin("missing:dev", Some(&archive)).await;
        ms.pin("img@sha256:x", Some(&archive)).await;
        ms.pull("offline:1").await.unwrap();
        ms.collect().await;
        assert_eq!(
            std::fs::read_to_string(state.join("calls")).unwrap(),
            "image inspect --format json reg:5000/img:dev\n\
                 image inspect --format json reg:5000/img@sha256:new\n\
                 pull --quiet alpine:3.20\n\
                 image inspect --format json alpine:3.20\n\
                 image inspect --format json alpine@sha256:new\n\
                 pull --quiet alpine@sha256:new\n\
                 image inspect --format json missing:dev\n\
                 pull --quiet offline:1\n\
                 image inspect --format json offline:1\n\
                 image inspect --format json offline@sha256:new\n\
                 pull --quiet offline@sha256:new\n\
                 image list --format json\n\
                 image remove -q reg:5000/img@sha256:old\n\
                 image remove -q reg:5000/img@sha256:busy\n"
        );
        let images: Vec<String> = ms
            .images()
            .await
            .unwrap()
            .into_iter()
            .map(|i| i.reference)
            .collect();
        assert_eq!(images, ["reg:5000/img:dev", "alpine:3.20"]);
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
            assert!(
                !ms.needs_resolution(),
                "missing Git does not discard the executor"
            );
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

#[cfg(any(test, feature = "test-support"))]
pub mod test_support {
    use std::{path::Path, process::Stdio};
    pub fn write_script(path: &Path, body: &str) {
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
}
