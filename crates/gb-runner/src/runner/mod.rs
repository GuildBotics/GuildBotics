//! `guildbotics runner`: the CI runner, a separate process from the service. It
//! claims attempts over the runner API, checks the commit out of a Git bundle
//! made by the service (it never touches the service's repositories or the
//! database), runs the job in a microVM, streams the log, uploads artifacts,
//! and reports the conclusion, renewing its lease while it works. See
//! `docs/ci.md`.

mod disk;
pub mod executor;
mod machine;
pub mod workspace;
pub mod zip;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use clap::{Args, Subcommand};
use serde::Deserialize;
use tokio::sync::{mpsc, watch};

use crate::ci::api::{ExecutionLimits, ExecutorState, RunnerHealth, RunnerReport};
use crate::ci::pipeline::JobDef;
use crate::ci::queue::{Basis, ResultInfo, Reuse};
use executor::{Executor, Health, JobRun, Microsandbox, Outcome};

/// The runner's command line: run it, or one of its subcommands.
#[derive(Args)]
#[command(args_conflicts_with_subcommands = true)]
pub struct RunnerArgs {
    #[command(subcommand)]
    command: Option<RunnerCommand>,
    /// Per-job work directories and the dependency cache [default: ci.executor.work_dir of the configuration, else <machine root>/runner/work]
    #[arg(long)]
    work_dir: Option<PathBuf>,
    #[arg(skip)]
    default_work_dir: Option<PathBuf>,
    #[cfg(feature = "host")]
    #[arg(skip)]
    vm_home: Option<PathBuf>,
    /// Base URL of the server [default: the server on this machine]
    #[arg(long, env = "GUILDBOTICS_RUNNER_URL")]
    url: Option<String>,
    /// The runner's token
    #[arg(long, env = "GUILDBOTICS_RUNNER_TOKEN", hide_env_values = true)]
    token: Option<String>,
    /// Configuration file for this machine [default: the one of the data directory]
    #[arg(long)]
    config: Option<PathBuf>,
    /// Data directory of the configuration [default: the server's]
    #[arg(long, env = "GUILDBOTICS_CODE_HOST_DATA_DIR")]
    data_dir: Option<PathBuf>,
}

impl RunnerArgs {
    pub fn default_work(mut self, path: PathBuf) -> Self {
        self.default_work_dir = Some(path);
        self
    }
    #[cfg(feature = "host")]
    pub fn vm_home(mut self, path: PathBuf) -> Self {
        self.vm_home = Some(gb_vm::home(&path));
        self
    }
    pub fn is_join(&self) -> bool {
        matches!(self.command, Some(RunnerCommand::Join))
    }
    /// Only serving claims needs a runner credential; maintenance commands do not.
    pub fn needs_token(&self) -> bool {
        self.command.is_none() && self.token.is_none()
    }
    pub fn default_token(mut self, token: Option<String>) -> Self {
        if self.token.is_none() {
            self.token = token;
        }
        self
    }
}

/// Runs the runner command `args`: `url` and `data_dir` are the server's on
/// this machine, used when `args` names none, and `owner_token` gives the
/// owner's token for `register`.
pub async fn main(
    args: RunnerArgs,
    url: String,
    data_dir: PathBuf,
    owner_token: impl Future<Output = anyhow::Result<String>>,
) -> anyhow::Result<u8> {
    let data_dir = args.data_dir.unwrap_or(data_dir);
    // A configuration named explicitly (the supervised runner gets the
    // service's) has to be read for its machine settings.
    let config = match &args.config {
        Some(file) => Some(crate::config::Config::load(Some(file), &data_dir)?),
        None => crate::config::Config::load(None, &data_dir).ok(),
    };
    let url = args.url.unwrap_or(url);
    #[allow(unused_mut)]
    let mut settings = Settings::new(
        url,
        args.token,
        args.work_dir,
        config.as_ref(),
        args.default_work_dir,
    )?;
    #[cfg(feature = "host")]
    {
        settings.vm_home = args.vm_home;
    }
    match args.command {
        Some(RunnerCommand::Join) => bail!("Join requires the GuildBotics client"),
        None => run(settings).await.map(|_| 0),
        Some(RunnerCommand::Register { name }) => {
            register(&settings.url, &owner_token.await?, &name)
                .await
                .map(|_| 0)
        }
        Some(RunnerCommand::Pull { image }) => pull(&settings, &image).await.map(|_| 0),
        Some(RunnerCommand::Doctor) => doctor(&settings).await,
        Some(RunnerCommand::InstallMsb { from }) => install_msb(&from).map(|_| 0),
    }
}

#[derive(Subcommand)]
enum RunnerCommand {
    /// Join this machine as a runner through the owner consent screen
    Join,
    /// Create a runner member and print its token once (needs the owner's token)
    Register {
        /// Name of the runner member, e.g. runner1
        name: String,
    },
    /// Pull an image into the runner's image cache (jobs never pull)
    Pull {
        /// Image reference by tag, [registry/]name[:tag], as pipelines name it
        image: String,
    },
    /// Show which msb is used and whether the executor is ready
    Doctor,
    /// Copy an msb binary (and the libkrunfw next to it) into <machine root>/runner/msb
    InstallMsb {
        /// Path of an msb binary that reports the pinned version
        #[arg(long)]
        from: PathBuf,
    },
}

/// What the runner process needs, resolved from flags, environment, and the
/// optional configuration.
pub struct Settings {
    pub url: String,
    pub token: Option<String>,
    pub work_dir: PathBuf,
    pub msb_path: Option<PathBuf>,
    pub msb_home: PathBuf,
    #[cfg(feature = "host")]
    pub vm_home: Option<PathBuf>,
}

impl Settings {
    pub fn new(
        url: String,
        token: Option<String>,
        work_dir: Option<PathBuf>,
        config: Option<&crate::config::Config>,
        default_work_dir: Option<PathBuf>,
    ) -> anyhow::Result<Self> {
        let ci = config.map(|c| c.ci.clone()).unwrap_or_default();
        Ok(Self {
            url: crate::config::normalize_base_url(&url)?,
            token,
            work_dir: work_dir
                .or(ci.executor.work_dir)
                .or(default_work_dir)
                .unwrap_or_else(|| executor::runner_home().join("work")),
            msb_path: ci.microsandbox.msb_path.clone(),
            #[cfg(feature = "host")]
            vm_home: None,
            msb_home: ci
                .microsandbox
                .home
                .clone()
                .unwrap_or_else(|| executor::runner_home().join("msb-home")),
        })
    }

    fn microsandbox(&self, runner: &str) -> Microsandbox {
        #[cfg(feature = "host")]
        if let Some(home) = &self.vm_home {
            return Microsandbox::hub(home.clone(), runner);
        }
        Microsandbox::new(
            executor::resolve_msb(self.msb_path.as_deref()),
            self.msb_home.clone(),
            runner,
        )
    }

    fn api(&self) -> anyhow::Result<Api> {
        let token = self
            .token
            .clone()
            .context("a runner token is needed: pass --token or set GUILDBOTICS_RUNNER_TOKEN (`guildbotics runner register`)")?;
        Ok(Api {
            url: self.url.clone(),
            token,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(600))
                .build()?,
        })
    }
}

// ---------------------------------------------------------------- service client

pub struct Api {
    url: String,
    token: String,
    http: reqwest::Client,
}

#[derive(Debug)]
pub enum ApiError {
    /// The lease is not current any more: stop, report nothing.
    Lost,
    /// The token is not valid (revoked): the runner stops.
    Unauthorized,
    Other(String),
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ApiError::Lost => write!(f, "the lease is no longer current"),
            ApiError::Unauthorized => write!(f, "the runner token is not valid (revoked?)"),
            ApiError::Other(m) => write!(f, "{m}"),
        }
    }
}

#[derive(Deserialize, Debug, Clone)]
pub struct Claim {
    #[serde(flatten)]
    pub execution: ExecutionLimits,
    pub attempt_id: String,
    pub lease: String,
    pub lease_secs: u64,
    pub reuse_only: bool,
    pub repo: String,
    pub sha: String,
    pub attempt: u64,
    pub job: JobDef,
    pub verified_sha: Option<String>,
    pub cache_write: bool,
    pub network: bool,
    pub queued_secs: u64,
    pub reuse: Option<Reuse>,
}

impl Api {
    pub fn new(url: &str, token: &str) -> Self {
        Self {
            url: url.trim_end_matches('/').to_string(),
            token: token.to_string(),
            http: reqwest::Client::new(),
        }
    }

    fn at(&self, path: &str) -> String {
        format!("{}/api/v1{path}", self.url)
    }

    async fn check(resp: reqwest::Response) -> Result<reqwest::Response, ApiError> {
        let status = resp.status();
        if status.is_success() {
            return Ok(resp);
        }
        let body: serde_json::Value = resp.json().await.unwrap_or_default();
        if status == reqwest::StatusCode::CONFLICT {
            return Err(ApiError::Lost);
        }
        if status == reqwest::StatusCode::UNAUTHORIZED {
            return Err(ApiError::Unauthorized);
        }
        Err(ApiError::Other(format!(
            "{} ({})",
            body["error"]["message"]
                .as_str()
                .unwrap_or("request failed"),
            status.as_u16()
        )))
    }

    fn net(e: reqwest::Error) -> ApiError {
        ApiError::Other(format!("Branvil is not reachable: {e}"))
    }

    pub async fn health(&self) -> Result<RunnerHealth, ApiError> {
        let reply = Self::check(
            self.http
                .get(self.at("/runner/health"))
                .bearer_auth(&self.token)
                .timeout(Duration::from_secs(10))
                .send()
                .await
                .map_err(Self::net)?,
        )
        .await?;
        reply.json().await.map_err(Self::net)
    }

    pub async fn me(&self) -> Result<serde_json::Value, ApiError> {
        let resp = self
            .http
            .get(self.at("/me"))
            .bearer_auth(&self.token)
            .send()
            .await
            .map_err(Self::net)?;
        Self::check(resp).await?.json().await.map_err(Self::net)
    }

    /// Reconcile only this CPU's Hub archives, comparing configuration digests.
    pub async fn sync_images(&self, ms: &Microsandbox) -> Result<(), ApiError> {
        use crate::ci::images::{Archive, architecture};
        use tokio::io::AsyncWriteExt;
        let snapshot = ms.inventory(self).await?.images;
        let resp = self
            .http
            .get(self.at(&format!("/runner/images?architecture={}", architecture())))
            .bearer_auth(&self.token)
            .send()
            .await
            .map_err(Self::net)?;
        let archives: Vec<Archive> = Self::check(resp).await?.json().await.map_err(Self::net)?;
        let mut failures = Vec::new();
        for archive in archives {
            let reference = archive
                .images
                .iter()
                .map(|i| i.reference.as_str())
                .collect::<Vec<_>>()
                .join(", ");
            let result = async {
                let mut needed = Vec::new();
                for image in &archive.images {
                    if image.architecture != architecture() {
                        continue;
                    }
                    let before = snapshot
                        .iter()
                        .find(|i| i.supports(&image.reference))
                        .map(|i| i.digest.clone());
                    if before.as_deref() != Some(&image.digest) {
                        needed.push((image, before));
                    }
                }
                if needed.is_empty() {
                    return Ok::<_, ApiError>(());
                }
                std::fs::create_dir_all(ms.home()).map_err(|e| ApiError::Other(e.to_string()))?;
                let temp = tempfile::NamedTempFile::new_in(ms.home())
                    .map_err(|e| ApiError::Other(e.to_string()))?;
                let mut file = tokio::fs::File::create(temp.path())
                    .await
                    .map_err(|e| ApiError::Other(e.to_string()))?;
                let mut resp = Self::check(
                    self.http
                        .get(self.at(&format!("/runner/images/{}", archive.id)))
                        .bearer_auth(&self.token)
                        .send()
                        .await
                        .map_err(Self::net)?,
                )
                .await?;
                use sha2::{Digest, Sha256};
                let mut hash = Sha256::new();
                while let Some(bytes) = resp.chunk().await.map_err(Self::net)? {
                    hash.update(&bytes);
                    file.write_all(&bytes)
                        .await
                        .map_err(|e| ApiError::Other(e.to_string()))?;
                }
                file.flush()
                    .await
                    .map_err(|e| ApiError::Other(e.to_string()))?;
                drop(file);
                if crate::util::hex(&hash.finalize()) != archive.id {
                    return Err(ApiError::Other("CI archive digest mismatch".into()));
                }
                let _lock = ms.lock_images(true, || {}).await.map_err(ApiError::Other)?;
                for (image, before) in needed {
                    let held = ms
                        .inspect(&image.reference)
                        .await
                        .map_err(ApiError::Other)?
                        .map(|i| i.config_digest);
                    // A concurrent writer owns any version installed during the download.
                    if held != before || held.as_deref() == Some(&image.digest) {
                        continue;
                    }
                    ms.load_as(temp.path(), &image.reference)
                        .await
                        .map_err(ApiError::Other)?;
                    let held = ms
                        .inspect(&image.reference)
                        .await
                        .map_err(ApiError::Other)?;
                    if held.is_none_or(|i| i.config_digest != image.digest) {
                        return Err(ApiError::Other(format!(
                            "Loaded image {} has an unexpected digest",
                            image.reference
                        )));
                    }
                    ms.pin(&image.reference, Some(temp.path())).await;
                }
                ms.collect().await;
                Ok(())
            }
            .await;
            match result {
                Ok(()) => {}
                Err(ApiError::Unauthorized) => return Err(ApiError::Unauthorized),
                Err(e) => failures.push(format!("{reference}: {e}")),
            }
        }
        if failures.is_empty() {
            Ok(())
        } else {
            Err(ApiError::Other(failures.join("; ")))
        }
    }

    async fn claim(&self, report: &RunnerReport) -> Result<Option<Claim>, ApiError> {
        let resp = self
            .http
            .post(self.at("/runner/claim"))
            .bearer_auth(&self.token)
            .json(report)
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .map_err(Self::net)?;
        let resp = Self::check(resp).await?;
        if resp.status() == reqwest::StatusCode::NO_CONTENT {
            return Ok(None);
        }
        resp.json().await.map(Some).map_err(Self::net)
    }

    async fn cache_decisions(
        &self,
        ids: &[String],
    ) -> Result<std::collections::BTreeMap<String, crate::ci::api::CacheDecision>, ApiError> {
        let response = self
            .http
            .post(self.at("/runner/cache"))
            .bearer_auth(&self.token)
            .json(ids)
            .timeout(Duration::from_secs(10))
            .send()
            .await
            .map_err(Self::net)?;
        Self::check(response).await?.json().await.map_err(Self::net)
    }

    /// Writes the commit's Git bundle to `dest`; returns its size.
    pub async fn source(&self, c: &Claim, dest: &Path) -> Result<u64, ApiError> {
        use tokio::io::AsyncWriteExt;
        let resp = self
            .http
            .get(self.at(&format!("/runner/attempts/{}/source", c.attempt_id)))
            .bearer_auth(&self.token)
            .header(crate::ci::api::LEASE_HEADER, &c.lease)
            .send()
            .await
            .map_err(Self::net)?;
        let mut resp = Self::check(resp).await?;
        let io = |e: std::io::Error| ApiError::Other(format!("cannot write the source: {e}"));
        let mut file = tokio::fs::File::create(dest).await.map_err(io)?;
        let mut bytes = 0;
        while let Some(chunk) = resp.chunk().await.map_err(Self::net)? {
            file.write_all(&chunk).await.map_err(io)?;
            bytes += chunk.len() as u64;
        }
        file.flush().await.map_err(io)?;
        Ok(bytes)
    }

    /// Returns whether the service asks the runner to stop.
    pub async fn heartbeat(&self, c: &Claim) -> Result<bool, ApiError> {
        let resp = self
            .http
            .post(self.at(&format!("/runner/attempts/{}/heartbeat", c.attempt_id)))
            .bearer_auth(&self.token)
            .json(&serde_json::json!({"lease": c.lease}))
            .send()
            .await
            .map_err(Self::net)?;
        let v: serde_json::Value = Self::check(resp).await?.json().await.map_err(Self::net)?;
        Ok(v["cancel"].as_bool().unwrap_or(false))
    }

    /// Returns whether the service truncated the log.
    pub async fn log(&self, c: &Claim, bytes: Vec<u8>) -> Result<bool, ApiError> {
        let resp = self
            .http
            .post(self.at(&format!("/runner/attempts/{}/log", c.attempt_id)))
            .bearer_auth(&self.token)
            .header(crate::ci::api::LEASE_HEADER, &c.lease)
            .body(bytes)
            .send()
            .await
            .map_err(Self::net)?;
        let v: serde_json::Value = Self::check(resp).await?.json().await.map_err(Self::net)?;
        Ok(v["truncated"].as_bool().unwrap_or(false))
    }

    pub async fn artifact(&self, c: &Claim, name: &str, data: Vec<u8>) -> Result<(), ApiError> {
        let resp = self
            .http
            .put(self.at(&format!(
                "/runner/attempts/{}/artifacts/{name}",
                c.attempt_id
            )))
            .bearer_auth(&self.token)
            .header(crate::ci::api::LEASE_HEADER, &c.lease)
            .body(data)
            .send()
            .await
            .map_err(Self::net)?;
        Self::check(resp).await.map(|_| ())
    }

    pub async fn finish(
        &self,
        c: &Claim,
        conclusion: &str,
        details: Option<String>,
        info: ResultInfo,
    ) -> Result<(), ApiError> {
        let resp = self
            .http
            .post(self.at(&format!("/runner/attempts/{}/finish", c.attempt_id)))
            .bearer_auth(&self.token)
            .json(
                &serde_json::json!({"lease": c.lease, "conclusion": conclusion, "details": details,
                "image_digest": info.image_digest, "peak_memory_mib": info.peak_memory_mib,
                "reused_from": info.reused_from, "cache_key": info.cache_key}),
            )
            .send()
            .await
            .map_err(Self::net)?;
        Self::check(resp).await.map(|_| ())
    }
}

// ---------------------------------------------------------------- commands

pub async fn register(url: &str, owner_token: &str, name: &str) -> anyhow::Result<()> {
    let http = reqwest::Client::new();
    let call = |method: reqwest::Method, path: String, body: serde_json::Value| {
        http.request(method, format!("{url}/api/v1{path}"))
            .bearer_auth(owner_token)
            .json(&body)
            .send()
    };
    let resp = call(
        reqwest::Method::POST,
        "/members".into(),
        serde_json::json!({"name": name, "kind": "runner"}),
    )
    .await
    .with_context(|| format!("the server is not reachable at {url}"))?;
    if !resp.status().is_success() {
        let v: serde_json::Value = resp.json().await.unwrap_or_default();
        bail!(
            "creating runner member {name}: {}",
            v["error"]["message"].as_str().unwrap_or("failed")
        );
    }
    let resp = call(
        reqwest::Method::POST,
        format!("/members/{}/tokens", crate::util::encode_segment(name)),
        serde_json::json!({"label": "runner"}),
    )
    .await?;
    let v: serde_json::Value = resp.json().await.unwrap_or_default();
    let token = v["token"]
        .as_str()
        .context("the service did not return a token")?;
    println!("{token}");
    Ok(())
}

pub async fn pull(settings: &Settings, image: &str) -> anyhow::Result<()> {
    if !crate::ci::pipeline::valid_image(image) {
        bail!("'{image}' is not an image reference by tag ([registry/]name[:tag])");
    }
    let ms = settings.microsandbox("pull");
    let Some(mut cmd) = ms.command() else {
        bail!(
            "{}",
            match ms.msb() {
                executor::Msb::Missing(m) => m.clone(),
                executor::Msb::Found(_) => String::new(),
            }
        );
    };
    let _lock = ms
        .lock_images(true, || {
            eprintln!("waiting for another image pull or load to finish")
        })
        .await
        .map_err(anyhow::Error::msg)?;
    // Bounded like the GUI's pulls: jobs wait for the lock it holds.
    let status = tokio::time::timeout(
        executor::IMAGE_TRANSFER,
        cmd.args(["pull", image])
            .stdout(std::process::Stdio::inherit())
            .stderr(std::process::Stdio::inherit())
            .status(),
    )
    .await
    .with_context(|| format!("msb pull {image} timed out"))??;
    if !status.success() {
        bail!("msb pull {image} failed ({status})");
    }
    ms.register_pull(image).await.map_err(anyhow::Error::msg)?;
    ms.collect().await;
    Ok(())
}

pub async fn doctor(settings: &Settings) -> anyhow::Result<u8> {
    let ms = settings.microsandbox("doctor");
    match ms.msb() {
        executor::Msb::Found(p) => println!("msb: {} (msb {})", p.display(), executor::MSB_VERSION),
        executor::Msb::Missing(m) => println!("msb: {m}"),
    }
    println!("MSB_HOME: {}", ms.home().display());
    println!("work directory: {}", settings.work_dir.display());
    let h = ms.health().await;
    println!(
        "executor: {}{}",
        if h.ready { "ready" } else { "unavailable" },
        h.detail.map(|d| format!(" ({d})")).unwrap_or_default()
    );
    Ok(if h.ready { 0 } else { 1 })
}

/// Copies an msb binary that reports the pinned version, and the libkrunfw
/// library next to it (`../lib/`), into `<machine root>/runner/msb`.
pub fn install_msb(from: &Path) -> anyhow::Result<()> {
    match executor::resolve_msb(Some(from)) {
        executor::Msb::Found(p) if p == from => {}
        _ => bail!(
            "{} is not an msb {} binary",
            from.display(),
            executor::MSB_VERSION
        ),
    }
    let dest = executor::runner_home().join("msb");
    let lib_src = from
        .parent()
        .and_then(Path::parent)
        .map(|p| p.join("lib"))
        .context("cannot find the lib directory next to msb")?;
    std::fs::create_dir_all(dest.join("bin"))?;
    std::fs::create_dir_all(dest.join("lib"))?;
    let bin = dest
        .join("bin")
        .join(from.file_name().context("msb file name")?);
    std::fs::copy(from, &bin)?;
    let mut libs = 0;
    for e in std::fs::read_dir(&lib_src)
        .with_context(|| format!("reading {}", lib_src.display()))?
        .flatten()
    {
        if e.file_name().to_string_lossy().starts_with("libkrunfw") && e.path().is_file() {
            std::fs::copy(e.path(), dest.join("lib").join(e.file_name()))?;
            libs += 1;
        }
    }
    if libs == 0 {
        bail!("no libkrunfw found in {}", lib_src.display());
    }
    println!("installed {} and {libs} libkrunfw file(s)", bin.display());
    Ok(())
}

/// Runs the runner until the process is stopped.
pub async fn run(settings: Settings) -> anyhow::Result<()> {
    let api = settings.api()?;
    let me = api
        .me()
        .await
        .map_err(|e| anyhow::anyhow!("cannot reach Branvil at {}: {e}", settings.url))?;
    if me["kind"] != "runner" {
        bail!(
            "the token belongs to a {} member; the runner needs a runner member's token (`guildbotics runner register`)",
            me["kind"]
        );
    }
    let name = me["name"].as_str().unwrap_or("runner").to_string();
    std::fs::create_dir_all(&settings.work_dir)?;
    let lock = std::fs::File::create(settings.work_dir.join("runner.lock"))?;
    if lock.try_lock().is_err() {
        bail!(
            "another runner uses {}; give each runner its own --work-dir",
            settings.work_dir.display()
        );
    }
    let runner = Runner::new(api, settings.work_dir.clone());
    tracing::info!(
        "runner {name} serving {} (work directory {})",
        settings.url,
        settings.work_dir.display()
    );
    Arc::new(runner)
        .serve(move || Arc::new(settings.microsandbox(&name)))
        .await
}

/// Signals that the service should restart after all held attempts have drained.
#[derive(Debug)]
pub struct VersionChanged;
impl std::fmt::Display for VersionChanged {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("runner_version_changed")
    }
}
impl std::error::Error for VersionChanged {}

pub struct Runner {
    pub api: Api,
    pub work_dir: PathBuf,
    cache: Arc<workspace::Cache>,
    sync_detail: Mutex<Option<String>>,
}

impl Runner {
    pub fn new(api: Api, work_dir: PathBuf) -> Self {
        Self {
            cache: Arc::new(workspace::Cache::new(work_dir.join("cache"))),
            sync_detail: Mutex::new(None),
            api,
            work_dir,
        }
    }

    #[cfg(feature = "test-support")]
    pub fn test_cache(&self) -> &workspace::Cache {
        &self.cache
    }

    /// Sweeps leftovers, then claims attempts and runs up to `max_jobs` of
    /// them at once, until its token is refused. It claims only when it can
    /// start the job, so waiting attempts stay queued in the service; with
    /// the token refused, the running attempts end on their own (each
    /// removing its microVM) before it returns.
    pub async fn serve<E: Executor + 'static>(
        self: Arc<Self>,
        executor: impl Fn() -> Arc<E> + Send,
    ) -> anyhow::Result<()> {
        let mut current = executor();
        current.sweep().await;
        let _ = std::fs::remove_dir_all(self.work_dir.join("jobs"));
        if let Err(e) = self.cache.remove_earlier_forms() {
            tracing::warn!("removing cache entries of an earlier form failed: {e}");
        }
        let mut running = tokio::task::JoinSet::new();
        let mut syncing = tokio::task::JoinSet::new();
        let mut next_sync = Instant::now();
        let mut backoff = 1;
        let mut version_changed = false;
        'claiming: loop {
            let mut wait = Duration::from_secs(2);
            let result = async {
                let hub = self.api.health().await?;
                version_changed = hub.version != gb_base::BUILD_VERSION;
                if !version_changed && let Some(max) = hub.cache_max_mib {
                    self.reconcile_cache(max << 20).await?;
                }
                let Some(max_jobs) = hub.max_jobs else {
                    if version_changed {
                        return Ok(None);
                    }
                    return Err(ApiError::Other("Hub did not report runner capacity".into()));
                };
                // Retry only unresolved runtimes, retaining the health cache
                // of a runtime that has already been resolved.
                if current.needs_resolution() {
                    current = executor();
                }
                if !version_changed
                    && syncing.is_empty()
                    && Instant::now() >= next_sync
                    && current.health().await.ready
                {
                    if self.sync_detail.lock().unwrap().is_none() {
                        *self.sync_detail.lock().unwrap() =
                            Some("CI image synchronization in progress".into());
                    }
                    let (runner, executor) = (self.clone(), current.clone());
                    syncing.spawn(async move { executor.synchronize(&runner.api).await });
                }
                self.claim(&*current, !version_changed, max_jobs, running.len())
                    .await
            }
            .await;
            if version_changed {
                if let Err(e) = result {
                    tracing::warn!("could not report the runner version: {e}");
                }
                break 'claiming;
            }
            match result {
                Ok(Some((claim, health))) => {
                    backoff = 1;
                    let (runner, executor) = (self.clone(), current.clone());
                    running.spawn(async move { runner.run(&*executor, &health, claim).await });
                    continue;
                }
                Ok(None) => backoff = 1,
                Err(ApiError::Unauthorized) => break 'claiming,
                Err(e) => {
                    tracing::warn!("{e}");
                    wait = Duration::from_secs(backoff);
                    backoff = (backoff * 2).min(30);
                }
            }
            tokio::select! {
                Some(ended) = running.join_next() => match ended {
                    Ok(Err(ApiError::Unauthorized)) => break 'claiming,
                    Ok(Err(e)) => tracing::warn!("{e}"),
                    Ok(Ok(())) => {}
                    Err(e) => tracing::error!("an attempt's task failed: {e}"),
                },
                Some(ended) = syncing.join_next() => {
                    next_sync = Instant::now() + Duration::from_secs(30);
                    match ended {
                        Ok(Err(ApiError::Unauthorized)) => break 'claiming,
                        Ok(Err(e)) => *self.sync_detail.lock().unwrap() = Some(format!("CI image synchronization failed: {e}")),
                        Ok(Ok(())) => *self.sync_detail.lock().unwrap() = None,
                        Err(e) => *self.sync_detail.lock().unwrap() = Some(format!("CI image synchronization failed: {e}")),
                    }
                },
                _ = tokio::time::sleep(wait) => {}
            }
        }
        syncing.abort_all();
        while syncing.join_next().await.is_some() {}
        while running.join_next().await.is_some() {}
        if version_changed {
            Err(VersionChanged.into())
        } else {
            bail!("{}", ApiError::Unauthorized)
        }
    }

    /// Claims an attempt, telling the service how the executor is.
    async fn claim<E: Executor>(
        &self,
        executor: &E,
        accept: bool,
        max_jobs: u32,
        running_jobs: usize,
    ) -> Result<Option<(Claim, Health)>, ApiError> {
        let mut health = executor.health().await;
        let images = if health.ready {
            match tokio::time::timeout(Duration::from_secs(10), executor.inventory(&self.api))
                .await
                .unwrap_or_else(|_| Err(ApiError::Other("Image cache inventory timed out".into())))
            {
                Ok(inventory) => {
                    health.detail = inventory.detail;
                    inventory.images
                }
                Err(ApiError::Unauthorized) => return Err(ApiError::Unauthorized),
                Err(e) => {
                    health.ready = false;
                    health.detail = Some(format!("Cannot read the image cache: {e}"));
                    Vec::new()
                }
            }
        } else {
            Vec::new()
        };
        if health.ready
            && let Some(sync) = self.sync_detail.lock().unwrap().clone()
        {
            health.detail = Some(
                health
                    .detail
                    .map_or(sync.clone(), |cache| format!("{sync}; {cache}")),
            );
        }
        let claim = self
            .api
            .claim(&RunnerReport {
                version: gb_base::BUILD_VERSION.into(),
                max_jobs,
                architecture: crate::ci::images::architecture().into(),
                executor: executor.name().into(),
                executor_state: if health.ready {
                    ExecutorState::Ready
                } else {
                    ExecutorState::Unavailable
                },
                executor_detail: health.detail.clone(),
                images,
                accept,
                running_jobs,
                machine: executor.machine().map(|mut machine| {
                    if !accept {
                        machine.commit = None;
                    }
                    machine
                }),
            })
            .await?;
        Ok(claim.map(|c| (c, health)))
    }

    /// Claims and runs at most one attempt. Returns whether there was one.
    #[cfg(any(test, feature = "test-support"))]
    pub async fn once<E: Executor>(&self, executor: &E) -> Result<bool, ApiError> {
        let hub = self.api.health().await?;
        let max_bytes = hub
            .cache_max_mib
            .filter(|_| hub.version == gb_base::BUILD_VERSION)
            .map(|mib| mib << 20);
        if let Some(max) = max_bytes {
            self.reconcile_cache(max).await?;
        }
        let max_jobs = hub
            .max_jobs
            .ok_or_else(|| ApiError::Other("Hub did not report runner capacity".into()))?;
        let Some((claim, health)) = self
            .claim(executor, hub.version == gb_base::BUILD_VERSION, max_jobs, 0)
            .await?
        else {
            return Ok(false);
        };
        self.run(executor, &health, claim).await?;
        if let Some(max) = max_bytes {
            self.reconcile_cache(max).await?;
        }
        Ok(true)
    }

    async fn reconcile_cache(&self, max_bytes: u64) -> Result<(), ApiError> {
        use crate::ci::api::{CACHE_DECISION_BATCH, CacheDecision};
        let cache = self.cache.clone();
        let ids = match blocking(move || cache.pending()).await {
            Ok(ids) => ids,
            Err(e) => {
                tracing::warn!("cannot list pending cache images: {e}");
                return Ok(());
            }
        };
        for ids in ids.chunks(CACHE_DECISION_BATCH) {
            let mut decisions = match self.api.cache_decisions(ids).await {
                Ok(decisions) => decisions,
                Err(ApiError::Unauthorized) => return Err(ApiError::Unauthorized),
                Err(e) => {
                    tracing::warn!("cannot reconcile pending cache images: {e}");
                    return Ok(());
                }
            };
            let ids = ids.to_vec();
            let cache = self.cache.clone();
            blocking(move || {
                for id in ids {
                    let publish = match decisions.remove(&id) {
                        Some(CacheDecision::Publish(at)) => Some(at.into()),
                        Some(CacheDecision::Discard) => None,
                        _ => continue,
                    };
                    if let Err(e) = cache.settle(&id, publish, max_bytes) {
                        tracing::warn!("{id}: cannot settle pending cache image: {e}");
                    }
                }
                Ok(())
            })
            .await
            .map_err(|e| ApiError::Other(e.to_string()))?;
        }
        Ok(())
    }

    /// Runs a claimed attempt with its Hub-provided resources and removes its directory.
    async fn run<E: Executor>(
        &self,
        executor: &E,
        health: &Health,
        claim: Claim,
    ) -> Result<(), ApiError> {
        tracing::info!(
            "attempt {} ({} #{} of {} {})",
            claim.attempt_id,
            claim.job.name,
            claim.attempt,
            claim.repo,
            claim.sha
        );
        let result = self.attempt(executor, health, &claim).await;
        let _ = std::fs::remove_dir_all(self.job_dir(&claim));
        match result {
            Err(ApiError::Lost) => {
                tracing::warn!(
                    "attempt {}: the lease was lost; result dropped",
                    claim.attempt_id
                );
                Ok(())
            }
            r => r,
        }
    }

    pub fn job_dir(&self, c: &Claim) -> PathBuf {
        self.work_dir.join("jobs").join(&c.attempt_id)
    }

    async fn attempt<E: Executor>(
        &self,
        executor: &E,
        health: &Health,
        c: &Claim,
    ) -> Result<(), ApiError> {
        let (cancel_tx, cancel_rx) = watch::channel(false);
        let lost = Arc::new(AtomicBool::new(false));
        let beat = tokio::spawn(heartbeats(
            Api::new(&self.api.url, &self.api.token),
            c.clone(),
            cancel_tx,
            lost.clone(),
        ));
        let (log_tx, log_rx) = mpsc::channel::<Vec<u8>>(256);
        let uploader = tokio::spawn(upload_log(
            Api::new(&self.api.url, &self.api.token),
            c.clone(),
            log_rx,
        ));
        let result = self
            .work(executor, health, c, log_tx, cancel_rx, &lost)
            .await;
        let _ = uploader.await;
        beat.abort();
        if lost.load(Ordering::SeqCst) {
            return Err(ApiError::Lost);
        }
        let (conclusion, details, info) = result;
        self.api.finish(c, conclusion, details, info).await
    }

    /// Everything between the claim and the finish. Returns the conclusion,
    /// its details, and the configuration digest of the image the job ran on
    /// (none when it did not get that far).
    #[allow(clippy::too_many_arguments)]
    async fn work<E: Executor>(
        &self,
        executor: &E,
        health: &Health,
        c: &Claim,
        log: mpsc::Sender<Vec<u8>>,
        cancel: watch::Receiver<bool>,
        lost: &AtomicBool,
    ) -> (&'static str, Option<String>, ResultInfo) {
        let say = |text: String| {
            let log = log.clone();
            async move {
                let _ = log.send(format!("[branvil] {text}\n").into_bytes()).await;
            }
        };
        if !health.ready {
            let why = health
                .detail
                .clone()
                .unwrap_or_else(|| "the executor is not usable".into());
            say(format!("executor {} unavailable: {why}", executor.name())).await;
            return (
                "startup_failure",
                Some(format!("The runner's executor is unavailable: {why}")),
                ResultInfo::default(),
            );
        }
        let job = &c.job;
        let limits = &c.execution.limits;
        let (cpus, memory_mib) = (limits.max_cpus, limits.max_memory_mib);
        let max = limits.max_timeout_secs;
        let timeout_secs = job.timeout_secs.map_or(max, |t| t.min(max));
        say(format!(
            "attempt {} of job {} on {} {} (executor {}, image {}, {cpus} cpu, {memory_mib} MiB, timeout {timeout_secs} s, network {})",
            c.attempt,
            job.name,
            c.repo,
            c.sha,
            executor.name(),
            job.image,
            if c.network { "public only" } else { "off" }
        ))
        .await;
        say(format!("queued for {} s", c.queued_secs)).await;
        if let Some(t) = job.timeout_secs.filter(|t| *t > max) {
            say(format!(
                "timeout_secs {t} exceeds the runner limit {max}; using {max}"
            ))
            .await;
        }
        // Checked before the source is fetched; the lock that comes with the
        // check keeps a load or a pull from re-pointing the checked tag until
        // the VM has picked the image.
        let mut stopping = cancel.clone();
        let checked = tokio::select! {
            checked = executor.resolve_image(&job.image, &log) => checked.map_err(Some),
            Ok(_) = stopping.wait_for(|stop| *stop) => Err(None),
        };
        let (held, image_lock) = match checked {
            Ok(checked) => checked,
            Err(None) => {
                say("job ended: cancelled while waiting for the image cache".into()).await;
                return (
                    "cancelled",
                    Some("Stopped on request.".to_string()),
                    ResultInfo::default(),
                );
            }
            Err(Some(e)) => {
                let why = format!("Checking the image failed: {e}");
                say(why.clone()).await;
                return ("startup_failure", Some(why), ResultInfo::default());
            }
        };
        let Some(digest) = held else {
            if c.reuse_only {
                return ("deferred", None, ResultInfo::default());
            }
            let why = format!(
                "The image {} is not in the runner's image cache (jobs never pull); pull or load it under Settings → CI for the runner on the Hub, or run `guildbotics runner pull {}` on the runner's machine.",
                job.image, job.image
            );
            say(why.clone()).await;
            return ("startup_failure", Some(why), ResultInfo::default());
        };
        say(format!("image {} is {digest}", job.image)).await;
        let mut info = ResultInfo {
            image_digest: Some(digest.clone()),
            ..Default::default()
        };
        if let Some(source) = &c.reuse {
            let reason = if source.image_digest != digest {
                Some(format!(
                    "image digest differs (source {}, current {digest})",
                    source.image_digest
                ))
            } else {
                None
            };
            if let Some(reason) = reason {
                if c.reuse_only {
                    say(format!("reuse deferred: {reason}")).await;
                    return ("deferred", None, ResultInfo::default());
                }
                say(format!(
                    "reuse declined for {} ({}): {reason}; executing the job",
                    source.attempt_id, source.sha
                ))
                .await;
            } else {
                let same = match source.basis {
                    Basis::Tree => "the same tree",
                    Basis::Paths => "the same contents of paths",
                };
                say(format!(
                    "reused result of {} ({}, {same}), image {digest}",
                    source.attempt_id, source.sha
                ))
                .await;
                say("job ended: success (reused)".into()).await;
                info.reused_from = Some(source.attempt_id.clone());
                return ("success", None, info);
            }
        } else {
            say("reuse unavailable: no reuse offered; executing the job".into()).await;
        }
        // The job's own steps; a `return` in the block ends them.
        let (conclusion, details) = async {
            // The work directory as the VM will see it: canonical (the runner's
            // directory may sit behind system links such as macOS's /var),
            // spelled as Git and msb take it on Windows (the names joined below
            // it never end in a dot or space, which Windows would drop), and
            // with no link anywhere below the runner's directory.
            let root = match std::fs::create_dir_all(&self.work_dir)
                .and_then(|_| std::fs::canonicalize(&self.work_dir))
                .and_then(|dir| executor::plain_path(dir, |p| std::fs::canonicalize(p)))
            {
                Ok(r) => r,
                Err(e) => {
                    return (
                        "startup_failure",
                        Some(format!("Cannot use the runner directory: {e}")),
                    );
                }
            };
            let rel = format!("jobs/{}/work", c.attempt_id);
            let log_rel = format!("jobs/{}/log", c.attempt_id);
            let io_rel = format!("jobs/{}/io", c.attempt_id);
            let dir = root.join("jobs").join(&c.attempt_id);
            let work = root.join(&rel);
            let log_dir = root.join(&log_rel);
            let io = root.join(&io_rel);
            let _ = std::fs::remove_dir_all(&dir);
            if let Err(e) = std::fs::create_dir_all(&work)
                .and_then(|_| std::fs::create_dir(&log_dir))
                .and_then(|_| std::fs::create_dir(&io))
            {
                return (
                    "startup_failure",
                    Some(format!("Cannot create the work directory: {e}")),
                );
            }
            let bundle = dir.join("source.bundle");
            let tree: workspace::Tree;
            let fetching = Instant::now();
            match self.api.source(c, &bundle).await {
                Ok(bytes) => {
                    say(format!(
                        "source fetched in {} ({bytes} bytes)",
                        ms(fetching)
                    ))
                    .await;
                    let checking_out = Instant::now();
                    let checked_out = workspace::checkout(
                        &bundle,
                        &work,
                        &dir.join("gitconfig"),
                        &io.join(executor::CHECKOUT),
                    )
                    .await;
                    let _ = std::fs::remove_file(&bundle);
                    match checked_out {
                        Ok((_, checked)) => tree = checked,
                        Err(e) => {
                            return (
                                "startup_failure",
                                Some(format!("Checking out the source failed: {e}")),
                            );
                        }
                    }
                    say(format!("checked out in {}", ms(checking_out))).await;
                }
                Err(ApiError::Lost) => {
                    lost.store(true, Ordering::SeqCst);
                    return ("cancelled", None);
                }
                Err(e) => {
                    return (
                        "startup_failure",
                        Some(format!("Fetching the source failed: {e}")),
                    );
                }
            }
            // Re-check right before the VM gets the directories.
            if let Err(e) = workspace::no_links_below(&root, &rel)
                .and_then(|_| workspace::no_links_below(&root, &log_rel))
                .and_then(|_| workspace::no_links_below(&root, &io_rel))
            {
                return (
                    "startup_failure",
                    Some(format!("Unsafe work directory: {e}")),
                );
            }
            let cache_entry = job.cache.as_ref().map(|cache| {
                let key = workspace::cache_key(
                    &c.repo,
                    job,
                    &digest,
                    cache,
                    limits.work_quota_mib,
                    &work,
                );
                let owner = workspace::CacheOwner {
                    repo: c.repo.clone(),
                    job: job.name.clone(),
                    user: executor::JOB_USER_ID,
                    paths: cache.paths.clone(),
                };
                (key, owner)
            });
            let cache_base = dir.join(disk::CACHE_BASE);
            let cache_disk = dir.join("cache-disk.raw");
            let (mut cache_data, mut kept_data) = (None, None);
            let mut exact_cache = false;
            if let Some((key, owner)) = &cache_entry {
                let restoring = Instant::now();
                let (store, into, lookup, owner) = (
                    self.cache.clone(),
                    cache_base.clone(),
                    key.clone(),
                    owner.clone(),
                );
                match blocking(move || store.restore_generation(&lookup, &owner, &into)).await {
                    Ok(Some((generation, bytes))) => {
                        cache_data = Some(bytes);
                        exact_cache = &generation == key;
                        let from = if exact_cache {
                            format!("exact key {key}")
                        } else {
                            format!("latest generation {generation} (requested {key})")
                        };
                        say(format!(
                            "cache restored in {} ({bytes} bytes) from {from}",
                            ms(restoring),
                        ))
                        .await
                    }
                    Ok(None) => say("no cache entry yet".into()).await,
                    Err(e) => {
                        say(format!(
                            "cache restore failed: {e}; the job runs without it"
                        ))
                        .await
                    }
                }
            }
            let env = job_environment(c);
            info.cache_key = cache_entry.as_ref().map(|(key, _)| key.clone());
            let outcome = executor
                .run(
                    JobRun {
                        attempt_id: &c.attempt_id,
                        peak_memory_mib: &mut info.peak_memory_mib,
                        job,
                        image_lock,
                        cpus,
                        memory_mib,
                        work_quota_mib: limits.work_quota_mib,
                        timeout_secs,
                        io: &io,
                        cache_base: cache_data.map(|_| cache_base.as_path()),
                        cache_disk: &cache_disk,
                        tree: &tree,
                        cache_data,
                        exact_cache,
                        kept_data: &mut kept_data,
                        save_cache: c.cache_write,
                        work: &work,
                        log_dir: &log_dir,
                        network: c.network,
                        env,
                    },
                    log.clone(),
                    cancel,
                )
                .await;
            if lost.load(Ordering::SeqCst) {
                return ("cancelled", None);
            }
            let (conclusion, details) = match &outcome {
                Outcome::Exited(0) => ("success", None),
                Outcome::Exited(code) => (
                    "failure",
                    Some(format!("A command exited with code {code}.")),
                ),
                Outcome::TimedOut => (
                    "timed_out",
                    Some(format!(
                        "The job ran longer than {timeout_secs} s and was stopped."
                    )),
                ),
                Outcome::Cancelled => ("cancelled", Some("Stopped on request.".to_string())),
                Outcome::StartupFailure(m) => ("startup_failure", Some(m.clone())),
            };
            say(format!(
                "job ended: {conclusion}{}",
                details
                    .as_ref()
                    .map(|d| format!(" ({d})"))
                    .unwrap_or_default()
            ))
            .await;
            if matches!(outcome, Outcome::Exited(_) | Outcome::TimedOut)
                && !job.artifacts.is_empty()
            {
                let collecting = Instant::now();
                let mut budget = c.execution.artifacts_max_mib << 20;
                for a in &job.artifacts {
                    let mut notes = Vec::new();
                    let packed = workspace::pack(&work, a, &mut budget, &mut notes);
                    for n in notes {
                        say(n).await;
                    }
                    match packed {
                        Ok(Some(p)) => match self.api.artifact(c, &p.name, p.data).await {
                            Ok(()) => {
                                say(format!(
                                    "artifact {} uploaded ({} file(s))",
                                    p.name, p.files
                                ))
                                .await
                            }
                            Err(ApiError::Lost) => {
                                lost.store(true, Ordering::SeqCst);
                                return ("cancelled", None);
                            }
                            Err(e) => say(format!("artifact {}: upload failed: {e}", p.name)).await,
                        },
                        Ok(None) => {}
                        Err(e) => say(format!("artifact {}: {e}", a.name)).await,
                    }
                }
                say(format!("artifacts handled in {}", ms(collecting))).await;
            }
            if conclusion == "success"
                && let Some((key, owner)) = cache_entry
                && let Some(data) = kept_data
            {
                let saving = Instant::now();
                let (store, attempt) = (self.cache.clone(), c.attempt_id.clone());
                let max = limits.cache_max_mib << 20;
                match blocking(move || {
                    store.stage(&attempt, &key, &owner, (&cache_disk, data), max)
                })
                .await
                {
                    Ok(true) => say(format!("cache staged in {} ({data} bytes)", ms(saving))).await,
                    Ok(false) => {
                        say("cache not staged: pending cache budget exhausted".into()).await
                    }
                    Err(e) => say(format!("cache staging failed: {e}")).await,
                }
            }
            (conclusion, details)
        }
        .await;
        (conclusion, details, info)
    }
}

/// Runs the cache's copies off the async threads, where the heartbeats and
/// logs of the other jobs run.
async fn blocking<T: Send + 'static>(
    f: impl FnOnce() -> std::io::Result<T> + Send + 'static,
) -> std::io::Result<T> {
    tokio::task::spawn_blocking(f)
        .await
        .unwrap_or_else(|e| Err(std::io::Error::other(e)))
}

/// The time since `since`, for the log.
fn ms(since: Instant) -> String {
    format!("{} ms", since.elapsed().as_millis())
}

/// Renews the lease every third of its length. The service asking to stop,
/// or the lease turning out stale (or not renewable before it expires),
/// stops the job.
async fn heartbeats(api: Api, c: Claim, cancel: watch::Sender<bool>, lost: Arc<AtomicBool>) {
    let every = Duration::from_secs((c.lease_secs / 3).clamp(1, 15));
    let mut last_ok = tokio::time::Instant::now();
    loop {
        tokio::time::sleep(every).await;
        match api.heartbeat(&c).await {
            Ok(stop) => {
                last_ok = tokio::time::Instant::now();
                if stop {
                    let _ = cancel.send(true);
                }
            }
            Err(ApiError::Lost) => {
                lost.store(true, Ordering::SeqCst);
                let _ = cancel.send(true);
                return;
            }
            Err(e) => {
                tracing::warn!("heartbeat of {}: {e}", c.attempt_id);
                if last_ok.elapsed() > Duration::from_secs(c.lease_secs) {
                    // The lease has expired by now: nobody will accept the result.
                    lost.store(true, Ordering::SeqCst);
                    let _ = cancel.send(true);
                    return;
                }
            }
        }
    }
}

/// Sends the log in batches (at most every 500 ms or 64 KiB) until every
/// sender is gone or the service says the log is full.
async fn upload_log(api: Api, c: Claim, mut rx: mpsc::Receiver<Vec<u8>>) {
    let mut buf: Vec<u8> = Vec::new();
    let mut full = false;
    loop {
        let more = tokio::time::timeout(Duration::from_millis(500), rx.recv()).await;
        let closed = matches!(more, Ok(None));
        if let Ok(Some(bytes)) = more {
            if !full {
                buf.extend_from_slice(&bytes);
            }
            if buf.len() < 64 << 10 {
                continue;
            }
        }
        while !buf.is_empty() && !full {
            let take = buf.len().min(512 << 10);
            let chunk: Vec<u8> = buf.drain(..take).collect();
            match api.log(&c, chunk).await {
                Ok(truncated) => full = truncated,
                Err(e) => {
                    tracing::warn!("log of {}: {e}", c.attempt_id);
                    full = true;
                }
            }
        }
        buf.clear();
        if closed {
            return;
        }
    }
}

/// Environment supplied to a job, including only its declared inputs.
pub fn job_environment(c: &Claim) -> Vec<(String, String)> {
    let mut env = vec![
        ("CI".to_string(), "true".to_string()),
        ("GB_CI_REPOSITORY".to_string(), c.repo.clone()),
        ("GB_CI_SHA".to_string(), c.sha.clone()),
        ("GB_CI_JOB".to_string(), c.job.name.clone()),
        ("GB_CI_ATTEMPT_ID".to_string(), c.attempt_id.clone()),
        ("GB_CI_ATTEMPT".to_string(), c.attempt.to_string()),
    ];
    if c.job.verified_sha {
        env.push((
            "GB_CI_VERIFIED_SHA".into(),
            c.verified_sha.clone().unwrap_or_default(),
        ));
    }
    env
}

/// Runs the test `test` (its full path) again in a child process with `env`
/// added and `GUILDBOTICS_TEST_CHILD` set, for what a test cannot change in its
/// own process: Git and the runner read the environment. Fails with the
/// child's output unless it passed.
#[cfg(test)]
pub(crate) fn pass_in_child(test: &str, env: &[(&str, &std::ffi::OsStr)]) {
    let (_, test) = test.split_once("::").expect("a test path");
    let out = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", test, "--nocapture"])
        .env("GUILDBOTICS_TEST_CHILD", "1")
        .envs(env.iter().copied())
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success() && text.contains("1 passed"),
        "{text}{}",
        String::from_utf8_lossy(&out.stderr)
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn machine_work_dir_precedes_the_clients_default() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("branvil.toml");
        std::fs::write(&file, "[ci.executor]\nwork_dir = 'local'\n").unwrap();
        let config = crate::config::Config::load(Some(&file), root.path()).unwrap();
        for (flag, configured, expected) in [
            (Some("flag"), Some(&config), "flag"),
            (None, Some(&config), "local"),
            (None, None, "default"),
        ] {
            let settings = Settings::new(
                "http://127.0.0.1:7878".into(),
                None,
                flag.map(PathBuf::from),
                configured,
                Some(PathBuf::from("default")),
            )
            .unwrap();
            assert_eq!(settings.work_dir, PathBuf::from(expected));
        }
    }
    #[tokio::test]
    async fn mismatch_stops_without_claiming() {
        use axum::{
            Json, Router,
            http::StatusCode,
            routing::{get, post},
        };
        let (reported, mut reports) = mpsc::channel(1);
        let app = Router::new()
            .route(
                "/api/v1/runner/health",
                get(|| async {
                    Json(RunnerHealth {
                        version: "different-build".into(),
                        max_jobs: Some(2),
                        cache_max_mib: None,
                    })
                }),
            )
            .route(
                "/api/v1/runner/claim",
                post(move |Json(report): Json<RunnerReport>| async move {
                    reported.send(report).await.unwrap();
                    StatusCode::NO_CONTENT
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let temp = tempfile::tempdir().unwrap();
        let runner = Arc::new(Runner::new(
            Api::new(&url, "fixture"),
            temp.path().join("work"),
        ));
        let home = temp.path().join("msb");
        let result = runner
            .serve(move || {
                Arc::new(Microsandbox::new(
                    executor::Msb::Missing("fixture".into()),
                    home.clone(),
                    "fixture",
                ))
            })
            .await;
        assert!(result.unwrap_err().is::<VersionChanged>());
        let report = reports.try_recv().unwrap();
        assert_eq!(report.version, gb_base::BUILD_VERSION);
        assert_eq!(report.max_jobs, 2);
        assert!(!report.accept);
        server.abort();
    }
}
