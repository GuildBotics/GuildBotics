//! `branvil runner`: the CI runner, a separate process from the service. It
//! claims attempts over the runner API, checks the commit out of a Git bundle
//! made by the service (it never touches the service's repositories or the
//! database), runs the job in a microVM, streams the log, uploads artifacts,
//! and reports the conclusion, renewing its lease while it works. See
//! `docs/ci.md`.

pub mod executor;
pub mod workspace;
pub mod zip;

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, bail};
use clap::Subcommand;
use serde::Deserialize;
use tokio::sync::{mpsc, watch};

use crate::ci::config::ExecutorConfig;
use crate::ci::pipeline::JobDef;
use crate::ci::queue::{ResultInfo, Reuse};
use executor::{Executor, Health, JobRun, Microsandbox, Outcome};

#[derive(Subcommand)]
pub enum RunnerCommand {
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
    /// Copy an msb binary (and the libkrunfw next to it) into ~/.branvil/msb
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
    pub executor: ExecutorConfig,
    /// The configuration file and data directory the limits are read from
    /// again before every claim, while the file exists.
    pub config: Option<(PathBuf, PathBuf)>,
    pub msb_path: Option<PathBuf>,
    pub msb_home: PathBuf,
}

impl Settings {
    pub fn new(
        url: Option<String>,
        token: Option<String>,
        work_dir: Option<PathBuf>,
        config: Option<&crate::config::Config>,
    ) -> anyhow::Result<Self> {
        let ci = config.map(|c| c.ci.clone()).unwrap_or_default();
        let url = url
            .or_else(|| config.and_then(|c| c.base_url.clone()))
            .unwrap_or_else(|| format!("http://{}", crate::config::DEFAULT_LISTEN));
        Ok(Self {
            url: crate::config::normalize_base_url(&url)?,
            token,
            work_dir: work_dir
                .or(ci.executor.work_dir.clone())
                .unwrap_or_else(|| executor::branvil_home().join("runner")),
            msb_path: ci.microsandbox.msb_path.clone(),
            msb_home: ci
                .microsandbox
                .home
                .clone()
                .unwrap_or_else(|| executor::branvil_home().join("msb-home")),
            executor: ci.executor,
            config: config.map(|c| (c.file.clone(), c.data_dir.clone())),
        })
    }

    fn microsandbox(&self, runner: &str) -> Microsandbox {
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
            .context("a runner token is needed: pass --token or set BRANVIL_TOKEN (`branvil runner register`)")?;
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
    pub attempt_id: String,
    pub lease: String,
    pub lease_secs: u64,
    pub repo: String,
    pub sha: String,
    pub attempt: u64,
    pub job: JobDef,
    pub cache_scope: String,
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

    pub async fn claim(&self, executor: &str, health: &Health) -> Result<Option<Claim>, ApiError> {
        let body = serde_json::json!({
            "executor": executor,
            "executor_state": if health.ready { "ready" } else { "unavailable" },
            "executor_detail": health.detail,
        });
        let resp = self
            .http
            .post(self.at("/runner/claim"))
            .bearer_auth(&self.token)
            .json(&body)
            .send()
            .await
            .map_err(Self::net)?;
        let resp = Self::check(resp).await?;
        if resp.status() == reqwest::StatusCode::NO_CONTENT {
            return Ok(None);
        }
        resp.json().await.map(Some).map_err(Self::net)
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
            .json(&serde_json::json!({"lease": c.lease, "conclusion": conclusion, "details": details,
                "image_digest": info.image_digest, "reused_from": info.reused_from, "cache_key": info.cache_key, "refresh_cache": info.refresh_cache}))
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
    .with_context(|| format!("Branvil is not reachable at {url}"))?;
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
/// library next to it (`../lib/`), into `~/.branvil/msb`.
pub fn install_msb(from: &Path) -> anyhow::Result<()> {
    match executor::resolve_msb(Some(from)) {
        executor::Msb::Found(p) if p == from => {}
        _ => bail!(
            "{} is not an msb {} binary",
            from.display(),
            executor::MSB_VERSION
        ),
    }
    let dest = executor::branvil_home().join("msb");
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
            "the token belongs to a {} member; the runner needs a runner member's token (`branvil runner register`)",
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
    let executor = settings.microsandbox(&name);
    let runner = Runner::new(
        api,
        settings.work_dir.clone(),
        settings.config.clone(),
        settings.executor.clone(),
    );
    tracing::info!(
        "runner {name} serving {} (work directory {})",
        settings.url,
        settings.work_dir.display()
    );
    Arc::new(runner).serve(Arc::new(executor)).await
}

pub struct Runner {
    pub api: Api,
    pub work_dir: PathBuf,
    /// Where `limits` are read from again before every claim, so that new
    /// limits apply to the next job and leave the running ones alone.
    config: Option<(PathBuf, PathBuf)>,
    /// The limits last read, and why the file last could not be read (said
    /// once, not at every claim).
    limits: Mutex<(ExecutorConfig, Option<String>)>,
    cache: Arc<workspace::Cache>,
}

impl Runner {
    pub fn new(
        api: Api,
        work_dir: PathBuf,
        config: Option<(PathBuf, PathBuf)>,
        limits: ExecutorConfig,
    ) -> Self {
        Self {
            cache: Arc::new(workspace::Cache::new(work_dir.join("cache"))),
            api,
            work_dir,
            config,
            limits: Mutex::new((limits, None)),
        }
    }

    /// The limits for the next job: the configuration file's as it reads
    /// now (the last ones read when it cannot be read).
    fn limits(&self) -> ExecutorConfig {
        let mut last = self.limits.lock().unwrap();
        let (limits, problem) = &mut *last;
        if let Some((file, data_dir)) = self.config.as_ref().filter(|(f, _)| f.is_file()) {
            match crate::config::Config::load(Some(file), Some(data_dir), None, None) {
                Ok(c) => (*limits, *problem) = (c.ci.executor, None),
                Err(e) => {
                    let e = format!("{e:#}");
                    if problem.as_ref() != Some(&e) {
                        tracing::warn!("keeping the runner's limits: {e}");
                    }
                    *problem = Some(e);
                }
            }
        }
        limits.clone()
    }

    /// Sweeps leftovers, then claims attempts and runs up to `max_jobs` of
    /// them at once, until its token is refused. It claims only when it can
    /// start the job, so waiting attempts stay queued in the service; with
    /// the token refused, the running attempts end on their own (each
    /// removing its microVM) before it returns.
    pub async fn serve<E: Executor + 'static>(
        self: Arc<Self>,
        executor: Arc<E>,
    ) -> anyhow::Result<()> {
        executor.sweep().await;
        let _ = std::fs::remove_dir_all(self.work_dir.join("jobs"));
        let mut running = tokio::task::JoinSet::new();
        let mut backoff = 1;
        'claiming: loop {
            let limits = self.limits();
            let mut wait = Duration::from_secs(2);
            if running.len() < limits.max_jobs as usize {
                match self.claim(&*executor).await {
                    Ok(Some((claim, health))) => {
                        backoff = 1;
                        let (runner, executor) = (self.clone(), executor.clone());
                        running.spawn(async move {
                            runner.run(&*executor, &health, claim, &limits).await
                        });
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
            }
            tokio::select! {
                Some(ended) = running.join_next() => match ended {
                    Ok(Err(ApiError::Unauthorized)) => break 'claiming,
                    Ok(Err(e)) => tracing::warn!("{e}"),
                    Ok(Ok(())) => {}
                    Err(e) => tracing::error!("an attempt's task failed: {e}"),
                },
                _ = tokio::time::sleep(wait) => {}
            }
        }
        while running.join_next().await.is_some() {}
        bail!("{}", ApiError::Unauthorized)
    }

    /// Claims an attempt, telling the service how the executor is.
    async fn claim<E: Executor>(&self, executor: &E) -> Result<Option<(Claim, Health)>, ApiError> {
        let health = executor.health().await;
        let claim = self.api.claim(executor.name(), &health).await?;
        Ok(claim.map(|c| (c, health)))
    }

    /// Claims and runs at most one attempt. Returns whether there was one.
    #[cfg(test)]
    pub async fn once<E: Executor>(&self, executor: &E) -> Result<bool, ApiError> {
        let limits = self.limits();
        let Some((claim, health)) = self.claim(executor).await? else {
            return Ok(false);
        };
        self.run(executor, &health, claim, &limits).await?;
        Ok(true)
    }

    /// Runs a claimed attempt with `limits` and removes its directory.
    async fn run<E: Executor>(
        &self,
        executor: &E,
        health: &Health,
        claim: Claim,
        limits: &ExecutorConfig,
    ) -> Result<(), ApiError> {
        tracing::info!(
            "attempt {} ({} #{} of {} {})",
            claim.attempt_id,
            claim.job.name,
            claim.attempt,
            claim.repo,
            claim.sha
        );
        let result = self.attempt(executor, health, &claim, limits).await;
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

    fn job_dir(&self, c: &Claim) -> PathBuf {
        self.work_dir.join("jobs").join(&c.attempt_id)
    }

    async fn attempt<E: Executor>(
        &self,
        executor: &E,
        health: &Health,
        c: &Claim,
        limits: &ExecutorConfig,
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
            .work(executor, health, c, limits, log_tx, cancel_rx, &lost)
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
        limits: &ExecutorConfig,
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
            let why = format!(
                "The image {} is not in the runner's image cache (jobs never pull); pull or load it under Settings → CI for the runner on the Branvil machine, or run `branvil runner pull {}` on the runner's machine.",
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
                if job.cache.is_some()
                    && c.cache_scope == "protected"
                    && let Some(key) = &source.cache_key
                {
                    info.refresh_cache = match self
                        .cache
                        .saved_age(key, std::time::SystemTime::now())
                    {
                        Ok(Some(age))
                            if age <= Duration::from_secs(workspace::CACHE_REFRESH_SECS) =>
                        {
                            say(format!(
                                "reuse cache {key}: saved {} s ago (within {} s)",
                                age.as_secs(),
                                workspace::CACHE_REFRESH_SECS
                            ))
                            .await;
                            false
                        }
                        Ok(Some(age)) => {
                            say(format!("cache {key} last saved {} s ago (older than {} s); requesting refresh", age.as_secs(), workspace::CACHE_REFRESH_SECS)).await;
                            true
                        }
                        Ok(None) => {
                            say(format!(
                                "cache {key} has no complete save; requesting refresh"
                            ))
                            .await;
                            true
                        }
                        Err(e) => {
                            say(format!(
                                "cache {key} save cannot be checked: {e}; requesting refresh"
                            ))
                            .await;
                            true
                        }
                    };
                }
                None
            };
            if let Some(reason) = reason {
                say(format!(
                    "reuse declined for {} ({}): {reason}; executing the job",
                    source.attempt_id, source.sha
                ))
                .await;
            } else {
                say(format!(
                    "reused result of {} ({}, the same tree), image {digest}",
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
            // directory may sit behind system links such as macOS's /var), and
            // with no link anywhere below the runner's directory.
            let root = match std::fs::create_dir_all(&self.work_dir)
                .and_then(|_| std::fs::canonicalize(&self.work_dir))
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
            let dir = root.join("jobs").join(&c.attempt_id);
            let work = root.join(&rel);
            let log_dir = root.join(&log_rel);
            let _ = std::fs::remove_dir_all(&dir);
            if let Err(e) =
                std::fs::create_dir_all(&work).and_then(|_| std::fs::create_dir(&log_dir))
            {
                return (
                    "startup_failure",
                    Some(format!("Cannot create the work directory: {e}")),
                );
            }
            let bundle = dir.join("source.bundle");
            let fetching = Instant::now();
            match self.api.source(c, &bundle).await {
                Ok(bytes) => {
                    say(format!(
                        "source fetched in {} ({bytes} bytes)",
                        ms(fetching)
                    ))
                    .await;
                    let checking_out = Instant::now();
                    let checked_out =
                        workspace::checkout(&bundle, &work, &dir.join("gitconfig")).await;
                    let _ = std::fs::remove_file(&bundle);
                    if let Err(e) = checked_out {
                        return (
                            "startup_failure",
                            Some(format!("Checking out the source failed: {e}")),
                        );
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
            {
                return (
                    "startup_failure",
                    Some(format!("Unsafe work directory: {e}")),
                );
            }
            let key = job
                .cache
                .as_ref()
                .map(|cache| workspace::cache_key(&c.repo, job, &digest, cache, &work));
            if let (Some(cache), Some(key)) = (&job.cache, &key) {
                let restoring = Instant::now();
                let (store, def, into, key) =
                    (self.cache.clone(), cache.clone(), work.clone(), key.clone());
                match blocking(move || store.restore(&key, &def, &into)).await {
                    Ok(Some(bytes)) => {
                        say(format!(
                            "cache restored in {} ({bytes} bytes)",
                            ms(restoring)
                        ))
                        .await
                    }
                    Ok(None) => say("no cache entry yet".into()).await,
                    Err(e) => say(format!("cache restore failed: {e}")).await,
                }
            }
            let env = vec![
                ("CI".to_string(), "true".to_string()),
                ("BRANVIL_CI".to_string(), "true".to_string()),
                ("BRANVIL_REPOSITORY".to_string(), c.repo.clone()),
                ("BRANVIL_SHA".to_string(), c.sha.clone()),
                ("BRANVIL_JOB".to_string(), job.name.clone()),
                ("BRANVIL_ATTEMPT_ID".to_string(), c.attempt_id.clone()),
                ("BRANVIL_ATTEMPT".to_string(), c.attempt.to_string()),
            ];
            info.cache_key = key.clone();
            let outcome = executor
                .run(
                    JobRun {
                        attempt_id: &c.attempt_id,
                        job,
                        image_lock,
                        cpus,
                        memory_mib,
                        timeout_secs,
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
                let mut budget = limits.artifacts_max_mib << 20;
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
            // Only the runs of protected branches write the cache, which every
            // run reads: a pull request plants nothing, and pushes none of
            // its entries out.
            if conclusion == "success" && job.cache.is_some() && c.cache_scope != "protected" {
                say("cache not saved (only a protected branch's run saves it)".into()).await;
            } else if conclusion == "success"
                && let (Some(cache), Some(key)) = (&job.cache, &key)
            {
                let saving = Instant::now();
                let (store, def, from, key) =
                    (self.cache.clone(), cache.clone(), work.clone(), key.clone());
                let max = limits.cache_max_mib << 20;
                let owner = workspace::CacheOwner {
                    repo: c.repo.clone(),
                    job: job.name.clone(),
                };
                match blocking(move || store.save(&key, &owner, &def, &from, max)).await {
                    Ok(bytes) => {
                        say(format!("cache saved in {} ({bytes} bytes)", ms(saving))).await
                    }
                    Err(e) => say(format!("cache save failed: {e}")).await,
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

/// Runs the test `test` (its full path) again in a child process with `env`
/// added and `BRANVIL_TEST_CHILD` set, for what a test cannot change in its
/// own process: Git and the runner read the environment. Fails with the
/// child's output unless it passed.
#[cfg(test)]
pub(crate) fn pass_in_child(test: &str, env: &[(&str, &std::ffi::OsStr)]) {
    let (_, test) = test.split_once("::").expect("a test path");
    let out = std::process::Command::new(std::env::current_exe().unwrap())
        .args(["--exact", test, "--nocapture"])
        .env("BRANVIL_TEST_CHILD", "1")
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
    //! The runner loop against an in-process service, with a fake executor
    //! that interprets a few words (`echo`, `write`, `exit`, `wait`, and
    //! `git`, which runs the host's Git in the work directory) and never
    //! starts a VM.

    use super::*;
    use executor::ImageLock;
    use std::sync::atomic::AtomicBool;

    struct Fake {
        ready: AtomicBool,
        /// The image cache is held (by a pull or a load): checks wait.
        hold_images: AtomicBool,
        changed_image: AtomicBool,
        /// What every run got: (image, cpus, memory MiB, timeout s).
        ran: std::sync::Mutex<Vec<(String, u32, u64, u64)>>,
        /// Jobs that reached `wait`, after source fetching and checkout.
        waiting: std::sync::atomic::AtomicUsize,
        /// `wait`s that saw the stop (an aborted run never does).
        stopped: std::sync::atomic::AtomicUsize,
    }

    impl Fake {
        fn new() -> Self {
            Self {
                ready: AtomicBool::new(true),
                hold_images: AtomicBool::new(false),
                changed_image: AtomicBool::new(false),
                ran: Default::default(),
                waiting: Default::default(),
                stopped: Default::default(),
            }
        }
    }

    /// The configuration digest the fake cache holds `image` as.
    fn digest_of(image: &str) -> String {
        format!(
            "sha256:{}",
            crate::util::hex(&crate::util::sha256(image.as_bytes()))
        )
    }

    impl Executor for Fake {
        fn name(&self) -> &'static str {
            "fake"
        }
        async fn health(&self) -> Health {
            let ready = self.ready.load(Ordering::SeqCst);
            Health {
                ready,
                detail: (!ready).then(|| "fake executor switched off".to_string()),
            }
        }
        async fn resolve_image(
            &self,
            image: &str,
            _: &mpsc::Sender<Vec<u8>>,
        ) -> Result<(Option<String>, ImageLock), String> {
            if self.hold_images.load(Ordering::SeqCst) {
                std::future::pending::<()>().await;
            }
            let digest = if self.changed_image.load(Ordering::SeqCst) {
                digest_of(&format!("updated-{image}"))
            } else {
                digest_of(image)
            };
            Ok((Some(digest), ImageLock::default()))
        }
        async fn run(
            &self,
            job: JobRun<'_>,
            log: mpsc::Sender<Vec<u8>>,
            mut cancel: watch::Receiver<bool>,
        ) -> Outcome {
            self.ran.lock().unwrap().push((
                job.job.image.clone(),
                job.cpus,
                job.memory_mib,
                job.timeout_secs,
            ));
            for c in &job.job.commands {
                let words: Vec<&str> = c.split_whitespace().collect();
                match words.as_slice() {
                    ["echo", rest @ ..] => {
                        let _ = log.send(format!("{}\n", rest.join(" ")).into_bytes()).await;
                    }
                    ["write", path, content] => {
                        let p = job.work.join(path);
                        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
                        std::fs::write(p, content).unwrap();
                    }
                    ["git", args @ ..] => {
                        let _ = log.send(git(job.work, args).into_bytes()).await;
                    }
                    ["exit", code] => return Outcome::Exited(code.parse().unwrap()),
                    ["wait"] => {
                        self.waiting.fetch_add(1, Ordering::SeqCst);
                        loop {
                            if cancel.changed().await.is_err() || *cancel.borrow() {
                                self.stopped.fetch_add(1, Ordering::SeqCst);
                                return Outcome::Cancelled;
                            }
                        }
                    }
                    _ => return Outcome::Exited(127),
                }
            }
            Outcome::Exited(0)
        }
        async fn sweep(&self) {}
    }

    /// Runs Git isolated from the user's configuration; returns its output.
    fn git(dir: &Path, args: &[&str]) -> String {
        let home = dir.join("..").join("git-home");
        std::fs::create_dir_all(&home).unwrap();
        let global = home.join("gitconfig");
        std::fs::write(&global, "").unwrap();
        let out = std::process::Command::new("git")
            .current_dir(dir)
            .args(["-c", "credential.helper=", "-c", "core.askPass="])
            .args(args)
            .env("HOME", &home)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", &global)
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_ASKPASS", "/usr/bin/false")
            .env("GIT_AUTHOR_NAME", "Test")
            .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
            .env("GIT_COMMITTER_NAME", "Test")
            .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&out.stderr)
        );
        String::from_utf8(out.stdout).unwrap()
    }

    /// An in-process service with the owner alice, the runner member runner1,
    /// and the repository acme/demo, whose main (tagged v1; the tag `later`
    /// is of a commit it does not reach) has `pipeline`, recorded so that its
    /// run is queued.
    struct Service {
        dir: tempfile::TempDir,
        app: crate::app::Shared,
        url: String,
        owner: String,
        runner_token: String,
        http: reqwest::Client,
        repo: crate::model::Repo,
        bare: PathBuf,
    }

    impl Service {
        async fn revoke_runner_token(&self) {
            let tokens: serde_json::Value = self
                .http
                .get(format!("{}/api/v1/members/runner1/tokens", self.url))
                .bearer_auth(&self.owner)
                .send()
                .await
                .unwrap()
                .json()
                .await
                .unwrap();
            for t in tokens["items"].as_array().unwrap() {
                let resp = self
                    .http
                    .delete(format!(
                        "{}/api/v1/members/runner1/tokens/{}",
                        self.url,
                        t["id"].as_str().unwrap()
                    ))
                    .bearer_auth(&self.owner)
                    .send()
                    .await
                    .unwrap();
                assert!(resp.status().is_success(), "{}", resp.status());
            }
        }
    }

    /// Hold source requests before authentication, so revocation reliably
    /// makes them fail with 401 rather than depending on checkout speed.
    struct SourceGate {
        requested: mpsc::Sender<tokio::sync::oneshot::Sender<()>>,
        refused: Arc<tokio::sync::Notify>,
        source_refusals: Arc<std::sync::atomic::AtomicUsize>,
    }

    async fn service(pipeline: &str) -> Service {
        service_with_source_gate(pipeline, None).await
    }

    async fn service_with_source_gate(pipeline: &str, gate: Option<SourceGate>) -> Service {
        let dir = tempfile::tempdir().unwrap();
        let data = dir.path().join("data");
        std::fs::create_dir_all(&data).unwrap();
        std::fs::write(data.join("branvil.toml"), "[ci]\nlease_secs = 3\n").unwrap();
        let config =
            crate::config::Config::load(None, Some(&data), Some("127.0.0.1:0"), None).unwrap();
        let layout = config.layout();
        layout.create().unwrap();
        let store = crate::store::Store::open(&layout.db(), 1 << 20, 2).unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let (owner, runner_token) = store
            .write_sync(|w| {
                use crate::domain::members::{NewMember, insert_member};
                use crate::model::MemberKind;
                for (name, kind) in [
                    ("alice", MemberKind::Owner),
                    ("runner1", MemberKind::Runner),
                ] {
                    insert_member(
                        w,
                        &NewMember {
                            name: name.into(),
                            display_name: None,
                            kind,
                        },
                    )?;
                }
                Ok((
                    crate::auth::issue_token(w, "alice", "t")?.1,
                    crate::auth::issue_token(w, "runner1", "t")?.1,
                ))
            })
            .unwrap();
        let key = layout.load_cursor_key().unwrap();
        let app = crate::app::App::new(config, url.clone(), store, key, url.clone());
        let mut router = crate::http::router(app.clone());
        if let Some(gate) = gate {
            router = router.layer(axum::middleware::from_fn(
                move |request: axum::extract::Request, next: axum::middleware::Next| {
                    let (requested, refused) = (gate.requested.clone(), gate.refused.clone());
                    let source_refusals = gate.source_refusals.clone();
                    async move {
                        let path = request.uri().path();
                        let source = path.ends_with("/source");
                        let claim = path.ends_with("/runner/claim");
                        if source {
                            let (resume, held) = tokio::sync::oneshot::channel();
                            requested.send(resume).await.unwrap();
                            held.await.unwrap();
                        }
                        let response = next.run(request).await;
                        if response.status() == axum::http::StatusCode::UNAUTHORIZED {
                            if claim {
                                refused.notify_one();
                            }
                            if source {
                                source_refusals.fetch_add(1, Ordering::SeqCst);
                            }
                        }
                        response
                    }
                },
            ));
        }
        tokio::spawn(axum::serve(listener, router).into_future());
        let http = reqwest::Client::new();
        for (path, body) in [
            ("workspaces", serde_json::json!({"name": "acme"})),
            ("repos", serde_json::json!({"path": "acme/demo"})),
        ] {
            let resp = http
                .post(format!("{url}/api/v1/{path}"))
                .bearer_auth(&owner)
                .json(&body)
                .send()
                .await
                .unwrap();
            assert_eq!(resp.status(), 201);
        }

        // A commit of main with a pipeline, pushed into the bare repository
        // directly (no hooks: the hooks directory is empty) and recorded.
        let work = dir.path().join("work");
        std::fs::create_dir_all(work.join(".branvil")).unwrap();
        std::fs::write(work.join(".branvil/ci.toml"), pipeline).unwrap();
        git(&work, &["init", "-q", "-b", "main"]);
        git(&work, &["add", "."]);
        git(&work, &["commit", "-qm", "pipeline"]);
        // A tag of the commit, and one of a commit it does not reach.
        git(&work, &["tag", "-a", "v1", "-m", "v1"]);
        git(&work, &["checkout", "-qb", "side"]);
        git(&work, &["commit", "-q", "--allow-empty", "-m", "later"]);
        git(&work, &["tag", "later"]);
        git(&work, &["checkout", "-q", "main"]);
        let bare = layout.repo_dir("acme/demo");
        git(
            &work,
            &["push", "-q", bare.to_str().unwrap(), "main", "v1", "later"],
        );
        let repo = app
            .store
            .read(|r| crate::domain::repo_by_path(r, "acme/demo"))
            .await
            .unwrap();
        crate::refs::record_refs(&app, &repo).await.unwrap();
        Service {
            dir,
            app,
            url,
            owner,
            runner_token,
            http,
            repo,
            bare,
        }
    }

    async fn empty_main(s: &Service, message: &str) -> String {
        let work = s.dir.path().join("work");
        git(&work, &["commit", "-q", "--allow-empty", "-m", message]);
        git(&work, &["push", "-q", s.bare.to_str().unwrap(), "main"]);
        crate::refs::record_refs(&s.app, &s.repo).await.unwrap();
        s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone()
    }

    async fn result_of(s: &Service, sha: &str) -> crate::ci::queue::Attempt {
        let (repo, sha) = (s.repo.id.clone(), sha.to_string());
        s.app
            .store
            .read(move |r| {
                Ok(crate::ci::queue::attempts_of(r, &repo, &sha)?
                    .into_iter()
                    .rfind(|a| a.purpose == crate::ci::queue::Purpose::Check)
                    .unwrap())
            })
            .await
            .unwrap()
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn reuse_skips_source_and_vm_but_digest_changes_and_retry_execute() {
        let s = service("[[jobs]]\nname = \"build\"\nimage = \"alpine\"\ncommands = [\"write out/report yes\"]\nartifacts = [{name = \"report\", paths = [\"out/\"]}]\n").await;
        let runner = Runner::new(
            Api::new(&s.url, &s.runner_token),
            s.dir.path().join("runner"),
            None,
            ExecutorConfig::default(),
        );
        let fake = Fake::new();
        let first_sha = s.app.git.branch_tips(&s.bare).await.unwrap()["main"].clone();
        runner.once(&fake).await.unwrap();
        let first = result_of(&s, &first_sha).await;
        let sha = empty_main(&s, "same tree").await;
        runner.once(&fake).await.unwrap();
        let reused = result_of(&s, &sha).await;
        assert_eq!(reused.reused_from.as_deref(), Some(first.id.as_str()));
        assert_eq!(reused.image_digest, first.image_digest);
        assert!(reused.cache_key.is_none());
        assert_eq!(fake.ran.lock().unwrap().len(), 1);
        let log = s.app.ci.log_tail(&reused.id, 1 << 20).await.unwrap().0;
        assert!(
            log.contains(&format!(
                "reused result of {} ({first_sha}, the same tree)",
                first.id
            )),
            "{log}"
        );
        assert!(
            !log.contains("source fetched") && !log.contains("sandbox started"),
            "{log}"
        );
        assert!(reused.details.is_none());
        let checks = s.app.ci.head_checks(&s.repo, &sha).await.unwrap().checks;
        assert!(checks[0].succeeded());
        assert!(checks[0].details.is_none());
        assert_eq!(checks[0].artifact_names, vec!["report"]);
        assert_eq!(checks[0].reused_from.as_ref().unwrap().attempt_id, first.id);
        let response = s
            .http
            .get(format!(
                "{}/api/v1/repos/acme%2Fdemo/commits/{sha}/artifacts/report",
                s.url
            ))
            .bearer_auth(&s.owner)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 200);
        assert_eq!(
            response.bytes().await.unwrap().as_ref(),
            std::fs::read(s.app.ci.artifact_path(&first.id, "report")).unwrap()
        );
        let view: serde_json::Value = s
            .http
            .get(format!(
                "{}/api/v1/repos/acme%2Fdemo/jobs/{}",
                s.url, reused.id
            ))
            .bearer_auth(&s.owner)
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(view["reused_from"]["sha"], first_sha);
        assert_eq!(
            view["reused_from"]["url"],
            format!("{}/acme/demo/-/jobs/{}", s.url, first.id)
        );
        assert_eq!(view["artifact_names"], serde_json::json!(["report"]));
        assert!(view["details"].is_null());

        fake.changed_image.store(true, Ordering::SeqCst);
        let changed_sha = empty_main(&s, "new digest").await;
        runner.once(&fake).await.unwrap();
        let changed = result_of(&s, &changed_sha).await;
        assert!(changed.reused_from.is_none());
        assert_eq!(fake.ran.lock().unwrap().len(), 2);
        let log = s.app.ci.log_tail(&changed.id, 1 << 20).await.unwrap().0;
        assert!(
            log.contains("image digest differs") && log.contains("source fetched"),
            "{log}"
        );
        let next_sha = empty_main(&s, "reuse new digest").await;
        runner.once(&fake).await.unwrap();
        let next = result_of(&s, &next_sha).await;
        assert_eq!(next.reused_from.as_deref(), Some(changed.id.as_str()));
        let id = next.id.clone();
        s.app
            .store
            .write(move |w| crate::ci::queue::retry(w, &id, 10, &crate::util::now()))
            .await
            .unwrap();
        runner.once(&fake).await.unwrap();
        let retried = result_of(&s, &next_sha).await;
        assert!(retried.reused_from.is_none());
        let retry_log = s.app.ci.log_tail(&retried.id, 1 << 20).await.unwrap().0;
        assert!(
            retry_log.contains("reuse unavailable: no reuse offered"),
            "{retry_log}"
        );
        assert!(
            !retry_log.contains("no matching successful execution"),
            "{retry_log}"
        );
        assert_eq!(fake.ran.lock().unwrap().len(), 3);

        // Reusing the result never extends the original files' retention.
        // Prune every actual source, so an old tree cannot reuse another one.
        let sources = [first.clone(), changed, retried];
        s.app
            .store
            .write(move |w| {
                for mut expired in sources {
                    expired.finished_at =
                        Some(crate::ci::queue::earlier(&crate::util::now(), 31 * 86_400));
                    w.put(
                        crate::store::CI_ATTEMPTS,
                        &crate::ci::queue::Attempt::key(&expired.id),
                        &expired,
                    )?;
                }
                Ok(())
            })
            .await
            .unwrap();
        crate::ci::files::apply_retention(&s.app).await.unwrap();
        assert!(
            s.app.ci.head_checks(&s.repo, &sha).await.unwrap().checks[0]
                .artifact_names
                .is_empty()
        );
        let view: serde_json::Value = s
            .http
            .get(format!(
                "{}/api/v1/repos/acme%2Fdemo/jobs/{}",
                s.url, reused.id
            ))
            .bearer_auth(&s.owner)
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        assert_eq!(view["artifact_names"], serde_json::json!([]));
        let response = s
            .http
            .get(format!(
                "{}/api/v1/repos/acme%2Fdemo/commits/{sha}/artifacts/report",
                s.url
            ))
            .bearer_auth(&s.owner)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 404);
        let sha = empty_main(&s, "same tree after retention").await;
        runner.once(&fake).await.unwrap();
        let fresh = result_of(&s, &sha).await;
        assert!(fresh.reused_from.is_none());
        assert_eq!(fake.ran.lock().unwrap().len(), 4);
        assert_eq!(
            s.app.ci.head_checks(&s.repo, &sha).await.unwrap().checks[0].artifact_names,
            vec!["report"]
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn protected_reuse_refreshes_separately_from_checks() {
        let s = service("[[jobs]]\nname = \"build\"\nimage = \"alpine\"\ncommands = [\"write target/out built\"]\ncache = { paths = [\"target\"] }\n").await;
        let runner = Runner::new(
            Api::new(&s.url, &s.runner_token),
            s.dir.path().join("runner"),
            None,
            ExecutorConfig::default(),
        );
        let fake = Fake::new();
        runner.once(&fake).await.unwrap();
        let sha = empty_main(&s, "fresh cache").await;
        runner.once(&fake).await.unwrap();
        assert!(result_of(&s, &sha).await.reused_from.is_some());
        assert_eq!(fake.ran.lock().unwrap().len(), 1);
        let original_sha = git(&s.dir.path().join("work"), &["rev-parse", "HEAD^"])
            .trim()
            .to_string();
        let original = result_of(&s, &original_sha).await;
        let key = original.cache_key.unwrap();
        let entry = runner.work_dir.join("cache").join(&key);
        let saved = entry.join(".branvil-saved");
        std::fs::File::options()
            .write(true)
            .open(&saved)
            .unwrap()
            .set_modified(
                std::time::SystemTime::now()
                    - Duration::from_secs(workspace::CACHE_REFRESH_SECS + 60),
            )
            .unwrap();
        let restore = s.dir.path().join("restored");
        std::fs::create_dir(&restore).unwrap();
        runner
            .cache
            .restore(&key, original.job.cache.as_ref().unwrap(), &restore)
            .unwrap();
        let sha = empty_main(&s, "old save despite fresh restore").await;
        runner.once(&fake).await.unwrap();
        let reused = result_of(&s, &sha).await;
        assert!(reused.reused_from.is_some());
        assert_eq!(
            fake.ran.lock().unwrap().len(),
            1,
            "reused check starts no VM"
        );
        let refresh_id = reused.cache_refresh.unwrap();
        assert!(
            s.app
                .ci
                .head_checks(&s.repo, &sha)
                .await
                .unwrap()
                .checks
                .iter()
                .all(|c| c.succeeded())
        );
        runner.once(&fake).await.unwrap();
        let refreshed = s
            .app
            .store
            .read(move |r| crate::ci::queue::get_attempt(r, &refresh_id))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(refreshed.purpose, crate::ci::queue::Purpose::CacheRefresh);
        assert!(refreshed.reused_from.is_none());
        assert_eq!(refreshed.cache_key.as_deref(), Some(key.as_str()));
        let log = s.app.ci.log_tail(&refreshed.id, 1 << 20).await.unwrap().0;
        assert!(log.contains("cache saved"), "{log}");
        let mut timed = refreshed.clone();
        timed.started_at = Some("2026-10-14T10:00:00.000Z".into());
        timed.finished_at = Some("2026-10-14T10:00:42.000Z".into());
        s.app
            .store
            .write(move |w| {
                w.put(
                    crate::store::CI_ATTEMPTS,
                    &crate::ci::queue::Attempt::key(&timed.id),
                    &timed,
                )
            })
            .await
            .unwrap();
        let mut estimates = vec![crate::ci::Check {
            name: "build".into(),
            status: "in_progress".into(),
            attempt_id: Some("running".into()),
            ..Default::default()
        }];
        s.app.ci.estimate(&s.repo, &sha, &mut estimates).await;
        assert_eq!(
            estimates[0].expected_secs,
            Some(42),
            "refresh execution supplies estimates"
        );
        let sha = empty_main(&s, "after refresh").await;
        runner.once(&fake).await.unwrap();
        assert!(result_of(&s, &sha).await.reused_from.is_some());
        assert_eq!(fake.ran.lock().unwrap().len(), 2);

        std::fs::remove_dir_all(&entry).unwrap();
        let sha = empty_main(&s, "missing save").await;
        runner.once(&fake).await.unwrap();
        let missing = result_of(&s, &sha).await;
        assert!(missing.reused_from.is_some());
        assert_eq!(fake.ran.lock().unwrap().len(), 2);
        assert!(missing.cache_refresh.is_some());
        let log = s.app.ci.log_tail(&missing.id, 1 << 20).await.unwrap().0;
        assert!(log.contains("has no complete save"), "{log}");

        runner.once(&fake).await.unwrap();
        assert_eq!(fake.ran.lock().unwrap().len(), 3);
        std::fs::remove_dir_all(&entry).unwrap();
        let sha = empty_main(&s, "branch reuse needs no save").await;
        let (repo, commit) = (s.repo.id.clone(), sha.clone());
        s.app
            .store
            .write(move |w| {
                let old = crate::ci::queue::get_run(w, &repo, &commit)?.unwrap();
                let mut run = old.clone();
                run.cache_scope = "branch:feature".into();
                crate::ci::queue::put_run(w, &run)
            })
            .await
            .unwrap();
        runner.once(&fake).await.unwrap();
        assert!(result_of(&s, &sha).await.reused_from.is_some());
        assert_eq!(fake.ran.lock().unwrap().len(), 3);
    }

    /// The run of a protected branch saves the cache; a pull request's run
    /// restores that entry and saves nothing.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn only_a_protected_branch_saves_the_cache() {
        let Service {
            dir,
            app,
            url,
            owner,
            runner_token,
            http,
            repo,
            bare,
        } = service(
            "[[jobs]]\nname = \"build\"\nimage = \"alpine\"\ncommands = [\"write target/out built\"]\ncache = { paths = [\"target\"] }\n",
        )
        .await;
        let runner = Runner::new(
            Api::new(&url, &runner_token),
            dir.path().join("runner"),
            None,
            ExecutorConfig::default(),
        );
        let fake = Fake::new();
        let log_of = |sha: String| {
            let (app, repo) = (app.clone(), repo.clone());
            async move {
                let checks = app.ci.head_checks(&repo, &sha).await.unwrap().checks;
                let id = checks[0].attempt_id.clone().unwrap();
                app.ci.log_tail(&id, 1 << 20).await.unwrap().0
            }
        };
        let main = app.git.branch_tips(&bare).await.unwrap()["main"].clone();
        assert!(runner.once(&fake).await.unwrap());
        let log = log_of(main).await;
        assert!(log.contains("[branvil] no cache entry yet\n"), "{log}");
        assert!(log.contains("[branvil] cache saved in "), "{log}");

        let work = dir.path().join("work");
        git(&work, &["checkout", "-qb", "feature"]);
        std::fs::write(work.join("feature.txt"), "feature").unwrap();
        git(&work, &["add", "."]);
        git(&work, &["commit", "-q", "-m", "feature"]);
        git(&work, &["push", "-q", bare.to_str().unwrap(), "feature"]);
        crate::refs::record_refs(&app, &repo).await.unwrap();
        let resp = http
            .post(format!("{url}/api/v1/repos/acme%2Fdemo/pulls"))
            .bearer_auth(&owner)
            .json(&serde_json::json!({"title": "feature", "head": "feature", "base": "main"}))
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 201);
        let feature = app.git.branch_tips(&bare).await.unwrap()["feature"].clone();
        assert!(runner.once(&fake).await.unwrap());
        let log = log_of(feature).await;
        assert!(log.contains("[branvil] cache restored in "), "{log}");
        assert!(
            log.contains("[branvil] cache not saved (only a protected branch's run saves it)\n")
                && !log.contains("cache saved"),
            "{log}"
        );
        let entries = std::fs::read_dir(dir.path().join("runner/cache"))
            .unwrap()
            .count();
        assert_eq!(entries, 1, "the pull request added no entry");

        // A fast-forward merge queues the cache job as protected, but the
        // same commit's PR execution and the existing recent save avoid a VM.
        git(&work, &["checkout", "-q", "main"]);
        git(&work, &["merge", "--ff-only", "feature"]);
        git(&work, &["push", "-q", bare.to_str().unwrap(), "main"]);
        crate::refs::record_refs(&app, &repo).await.unwrap();
        assert!(runner.once(&fake).await.unwrap());
        let feature = app.git.branch_tips(&bare).await.unwrap()["main"].clone();
        let log = log_of(feature).await;
        assert!(
            log.contains("reused result of") && log.contains("reuse cache"),
            "{log}"
        );
        assert!(!log.contains("source fetched"), "{log}");
        assert_eq!(fake.ran.lock().unwrap().len(), 2);

        // main's next commit restores the entry and saves it again.
        git(&work, &["checkout", "-q", "main"]);
        std::fs::write(work.join("next.txt"), "next").unwrap();
        git(&work, &["add", "."]);
        git(&work, &["commit", "-q", "-m", "next"]);
        git(&work, &["push", "-q", bare.to_str().unwrap(), "main"]);
        crate::refs::record_refs(&app, &repo).await.unwrap();
        let next = app.git.branch_tips(&bare).await.unwrap()["main"].clone();
        assert!(runner.once(&fake).await.unwrap());
        let log = log_of(next).await;
        assert!(
            log.contains("[branvil] cache restored in ")
                && log.contains("[branvil] cache saved in "),
            "{log}"
        );
    }

    /// Up to `max_jobs` attempts run at once, each to its own end. The
    /// runner reads the limit from its configuration before every claim, so
    /// a new limit applies to the next claim and stops no running job.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn the_runner_runs_up_to_max_jobs_attempts_at_once() {
        let pipeline: String = ["a", "b", "c", "d"]
            .iter()
            .map(|n| {
                format!("[[jobs]]\nname = \"{n}\"\nimage = \"alpine\"\ncommands = [\"wait\"]\n\n")
            })
            .collect();
        let Service {
            dir,
            app,
            url,
            owner,
            runner_token,
            http,
            repo,
            bare,
        } = service(&pipeline).await;
        let config = dir.path().join("runner.toml");
        let max_jobs =
            |n: u32| std::fs::write(&config, format!("[ci.executor]\nmax_jobs = {n}\n")).unwrap();
        max_jobs(2);
        let runner = Runner::new(
            Api::new(&url, &runner_token),
            dir.path().join("runner"),
            Some((config.clone(), dir.path().to_path_buf())),
            ExecutorConfig::default(),
        );
        let fake = Fake::new();
        let serving = tokio::spawn(Arc::new(runner).serve(Arc::new(fake)));
        let sha = app.git.branch_tips(&bare).await.unwrap()["main"].clone();
        // The checks once `done` holds of them (by name: status, attempt).
        let settle = |done: fn(&[(String, String, u64)]) -> bool| {
            let (app, repo, sha) = (app.clone(), repo.clone(), sha.clone());
            async move {
                for _ in 0..100 {
                    let mut seen: Vec<(String, String, u64, String)> = app
                        .ci
                        .head_checks(&repo, &sha)
                        .await
                        .unwrap()
                        .checks
                        .into_iter()
                        .map(|c| {
                            let status = c.conclusion.unwrap_or(c.status);
                            (
                                c.name,
                                status,
                                c.attempt.unwrap_or(0),
                                c.attempt_id.unwrap_or_default(),
                            )
                        })
                        .collect();
                    seen.sort();
                    let states: Vec<_> = seen
                        .iter()
                        .map(|c| (c.0.clone(), c.1.clone(), c.2))
                        .collect();
                    if done(&states) {
                        return seen;
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
                panic!("the checks never settled");
            }
        };
        let cancel = |id: String| {
            let (http, url, owner) = (http.clone(), url.clone(), owner.clone());
            async move {
                let resp = http
                    .post(format!("{url}/api/v1/repos/acme%2Fdemo/jobs/{id}/cancel"))
                    .bearer_auth(&owner)
                    .send()
                    .await
                    .unwrap();
                assert_eq!(resp.status(), 200);
            }
        };
        fn count(s: &[(String, String, u64)], status: &str) -> usize {
            s.iter().filter(|c| c.1 == status).count()
        }

        // Two run at once; the others wait in the queue, and the end of one
        // starts the next.
        let seen = settle(|s| count(s, "in_progress") == 2 && count(s, "queued") == 2).await;
        cancel(
            seen.iter()
                .find(|c| c.1 == "in_progress")
                .unwrap()
                .3
                .clone(),
        )
        .await;
        let seen = settle(|s| {
            count(s, "cancelled") == 1 && count(s, "in_progress") == 2 && count(s, "queued") == 1
        })
        .await;
        let running: Vec<_> = seen.iter().filter(|c| c.1 == "in_progress").collect();
        // With the limit lowered to one, the end of one job leaves the other
        // running and the last queued.
        max_jobs(1);
        cancel(running[0].3.clone()).await;
        settle(|s| count(s, "cancelled") == 2 && count(s, "in_progress") == 1).await;
        tokio::time::sleep(Duration::from_secs(3)).await;
        let seen = settle(|s| count(s, "in_progress") == 1 && count(s, "queued") == 1).await;
        assert!(
            seen.iter()
                .any(|c| c.0 == running[1].0 && c.1 == "in_progress" && c.2 == 1),
            "{seen:?}"
        );
        // Raised again, the last starts next to it.
        max_jobs(2);
        let seen = settle(|s| count(s, "in_progress") == 2 && count(s, "cancelled") == 2).await;
        for c in seen.iter().filter(|c| c.1 == "in_progress") {
            assert_eq!(c.2, 1, "never interrupted: {seen:?}");
            cancel(c.3.clone()).await;
        }
        settle(|s| count(s, "cancelled") == 4).await;
        serving.abort();
    }

    /// A refused token stops the runner, but only once the jobs it runs have
    /// ended on their own (each stopping and removing its microVM), not by
    /// dropping them with their VMs.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_refused_token_lets_the_running_jobs_end_first() {
        let pipeline = "[[jobs]]\nname = \"a\"\nimage = \"alpine\"\ncommands = [\"wait\"]\n\n[[jobs]]\nname = \"b\"\nimage = \"alpine\"\ncommands = [\"wait\"]\n";
        let s = service(pipeline).await;
        let runner = Runner::new(
            Api::new(&s.url, &s.runner_token),
            s.dir.path().join("runner"),
            None,
            ExecutorConfig {
                max_jobs: 2,
                ..ExecutorConfig::default()
            },
        );
        let fake = Arc::new(Fake::new());
        let serving = tokio::spawn(Arc::new(runner).serve(fake.clone()));
        // `in_progress` means claimed, not that the executor has started.
        tokio::time::timeout(Duration::from_secs(10), async {
            while fake.waiting.load(Ordering::SeqCst) != 2 {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("both jobs reached the executor's wait");
        s.revoke_runner_token().await;
        let stopped = tokio::time::timeout(Duration::from_secs(30), serving)
            .await
            .expect("the runner stops")
            .unwrap();
        assert!(stopped.is_err());
        assert_eq!(
            fake.stopped.load(Ordering::SeqCst),
            2,
            "both jobs ended on their own"
        );
    }

    /// Source fetching is part of the attempt's lifetime too: even after a
    /// claim is refused, each held request must return and clean up its job
    /// directory, without ever starting the executor on a failed fetch.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_refused_token_lets_source_fetches_end_first() {
        let pipeline: String = ["a", "b", "c"]
            .iter()
            .map(|name| {
                format!(
                    "[[jobs]]\nname = \"{name}\"\nimage = \"alpine\"\ncommands = [\"wait\"]\n\n"
                )
            })
            .collect();
        let (requested, mut requests) = mpsc::channel(2);
        let refused = Arc::new(tokio::sync::Notify::new());
        let source_refusals = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let s = service_with_source_gate(
            &pipeline,
            Some(SourceGate {
                requested,
                refused: refused.clone(),
                source_refusals: source_refusals.clone(),
            }),
        )
        .await;
        let runner = Arc::new(Runner::new(
            Api::new(&s.url, &s.runner_token),
            s.dir.path().join("runner"),
            None,
            ExecutorConfig {
                // Leave capacity for another claim to observe the refusal
                // while all three source requests remain held.
                max_jobs: 4,
                ..ExecutorConfig::default()
            },
        ));
        let fake = Arc::new(Fake::new());
        let serving = tokio::spawn(runner.clone().serve(fake.clone()));
        let mut held = Vec::new();
        for _ in 0..3 {
            held.push(
                tokio::time::timeout(Duration::from_secs(10), requests.recv())
                    .await
                    .expect("the source request arrives")
                    .unwrap(),
            );
        }
        s.revoke_runner_token().await;
        tokio::time::timeout(Duration::from_secs(10), refused.notified())
            .await
            .expect("a claim observes the refused token");
        // One source fetch returns 401 and cleans up; the runner must still
        // let the other two finish instead of dropping their tasks.
        held.pop().unwrap().send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(10), async {
            while std::fs::read_dir(runner.work_dir.join("jobs"))
                .unwrap()
                .count()
                != held.len()
            {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("the first source fetch ended and removed its directory");
        assert!(!serving.is_finished(), "source requests are still held");
        assert!(fake.ran.lock().unwrap().is_empty());
        for resume in held {
            resume.send(()).unwrap();
        }
        let result = tokio::time::timeout(Duration::from_secs(10), serving)
            .await
            .expect("the runner stops after all source fetches end")
            .unwrap();
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("token is not valid")
        );
        assert!(fake.ran.lock().unwrap().is_empty(), "no executor started");
        assert_eq!(source_refusals.load(Ordering::SeqCst), 3);
        assert_eq!(fake.stopped.load(Ordering::SeqCst), 0);
        assert_eq!(
            std::fs::read_dir(runner.work_dir.join("jobs"))
                .unwrap()
                .count(),
            0,
            "every attempt removed its directory before the runner returned"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn the_runner_loop_reports_results_cancellation_and_an_unavailable_executor() {
        let Service {
            dir,
            app,
            url,
            owner,
            runner_token,
            http,
            repo,
            bare,
        } = service(
            "[[jobs]]\nname = \"ok\"\nimage = \"alpine\"\ncommands = [\"echo hello from the job\", \"write out/r.txt report\", \"git rev-parse HEAD\", \"git describe --tags\", \"git tag --list\", \"git ls-files\", \"git config --list --local\"]\nartifacts = [{name = \"report\", paths = [\"out/\"]}]\n\n\
             [[jobs]]\nname = \"slow\"\nimage = \"alpine\"\ncommands = [\"wait\"]\ntimeout_secs = 5000\n",
        )
        .await;

        let runner = Runner::new(
            Api::new(&url, &runner_token),
            dir.path().join("runner"),
            None,
            ExecutorConfig {
                max_cpus: 3,
                max_memory_mib: 2048,
                max_timeout_secs: 100,
                ..ExecutorConfig::default()
            },
        );
        let fake = Fake::new();
        let job = |id: &str| {
            let (http, url, owner, id) = (http.clone(), url.clone(), owner.clone(), id.to_string());
            async move {
                http.get(format!("{url}/api/v1/repos/acme%2Fdemo/jobs/{id}"))
                    .bearer_auth(&owner)
                    .send()
                    .await
                    .unwrap()
                    .json::<serde_json::Value>()
                    .await
                    .unwrap()
            }
        };
        let sha = app.git.branch_tips(&bare).await.unwrap()["main"].clone();
        let checks = || {
            let (app, repo, sha) = (app.clone(), repo.clone(), sha.clone());
            async move { app.ci.head_checks(&repo, &sha).await.unwrap().checks }
        };

        // Job "ok": output streamed, artifact collected, success.
        assert!(runner.once(&fake).await.unwrap());
        let ok = checks().await.into_iter().find(|c| c.name == "ok").unwrap();
        assert_eq!(ok.conclusion.as_deref(), Some("success"), "{ok:?}");
        // The executor runs the tag with the runner's CPUs and memory, and
        // the runner's time limit for a job that gives none.
        assert_eq!(
            fake.ran.lock().unwrap()[0],
            ("alpine".to_string(), 3, 2048, 100)
        );
        assert_eq!(ok.artifact_names, vec!["report".to_string()]);
        let id = ok.attempt_id.clone().unwrap();
        // The attempt records the image it ran on, and the log says so first.
        let digest = digest_of("alpine");
        assert_eq!(job(&id).await["image_digest"], digest.as_str());
        let (log, _) = app.ci.log_tail(&id, 1 << 20).await.unwrap();
        let (first, rest) = log.split_once("\n[branvil] queued for ").unwrap();
        assert_eq!(
            first,
            format!(
                "[branvil] attempt 1 of job ok on acme/demo {sha} (executor fake, image alpine, 3 cpu, 2048 MiB, timeout 100 s, network off)"
            )
        );
        assert!(
            rest.split_once(" s\n")
                .is_some_and(|(secs, rest)| secs.parse::<u64>().is_ok()
                    && rest.starts_with(&format!("[branvil] image alpine is {digest}\n"))),
            "{log}"
        );
        // Each step of the runner's own says how long it took.
        for step in [
            "[branvil] source fetched in ",
            "[branvil] checked out in ",
            "[branvil] artifacts handled in ",
        ] {
            assert!(log.contains(step), "{step} in {log}");
        }
        assert!(log.contains("hello from the job"), "{log}");
        // The work directory is a checkout of the commit with its history
        // and the tags it reaches, and without a remote.
        assert!(
            log.contains(&format!("\n{sha}\nv1\nv1\n.branvil/ci.toml\n")),
            "{log}"
        );
        assert!(!log.contains("later") && !log.contains("remote."), "{log}");
        assert!(
            log.contains("artifact report uploaded (1 file(s))"),
            "{log}"
        );
        assert!(
            !runner.work_dir.join("jobs").join(&id).exists(),
            "work directory removed"
        );

        // Job "slow": cancelled through the API while it runs, then again
        // while its check waits for the image cache.
        let cancel_slow = |runner: Runner, fake: Fake| {
            let (checks, http, url, owner) = (checks, http.clone(), url.clone(), owner.clone());
            async move {
                let running = tokio::spawn(async move {
                    let r = runner.once(&fake).await;
                    (runner, fake, r)
                });
                let mut slow_id = None;
                for _ in 0..50 {
                    if let Some(c) = checks()
                        .await
                        .into_iter()
                        .find(|c| c.name == "slow" && c.status == "in_progress")
                    {
                        slow_id = c.attempt_id;
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
                let slow_id = slow_id.expect("slow job running");
                let resp = http
                    .post(format!(
                        "{url}/api/v1/repos/acme%2Fdemo/jobs/{slow_id}/cancel"
                    ))
                    .bearer_auth(&owner)
                    .send()
                    .await
                    .unwrap();
                assert_eq!(resp.status(), 200);
                let (runner, fake, r) = tokio::time::timeout(Duration::from_secs(20), running)
                    .await
                    .unwrap()
                    .unwrap();
                assert!(r.unwrap());
                (runner, fake, slow_id)
            }
        };
        let (runner, fake, slow_id) = cancel_slow(runner, fake).await;
        let slow = job(&slow_id).await;
        assert_eq!(slow["conclusion"], "cancelled", "{slow}");
        assert_eq!(slow["image_digest"], digest.as_str());
        // A larger time limit is lowered to the runner's, and the log says so.
        assert_eq!(fake.ran.lock().unwrap()[1].3, 100);
        let (log, _) = app.ci.log_tail(&slow_id, 1 << 20).await.unwrap();
        assert!(
            log.contains("[branvil] timeout_secs 5000 exceeds the runner limit 100; using 100\n"),
            "{log}"
        );
        fake.hold_images.store(true, Ordering::SeqCst);
        let resp = http
            .post(format!(
                "{url}/api/v1/repos/acme%2Fdemo/jobs/{slow_id}/retry"
            ))
            .bearer_auth(&owner)
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);
        let (runner, fake, waiting_id) = cancel_slow(runner, fake).await;
        let waiting = job(&waiting_id).await;
        assert_eq!(waiting["conclusion"], "cancelled", "{waiting}");
        assert!(waiting["image_digest"].is_null(), "{waiting}");
        // Its log still names the attempt it was.
        let (log, _) = app.ci.log_tail(&waiting_id, 1 << 20).await.unwrap();
        assert!(
            log.starts_with("[branvil] attempt 2 of job slow on acme/demo ")
                && log.contains("job ended: cancelled while waiting for the image cache"),
            "{log}"
        );
        assert_eq!(
            fake.ran.lock().unwrap().len(),
            2,
            "the waiting job never ran"
        );
        fake.hold_images.store(false, Ordering::SeqCst);

        // An unusable executor: the job concludes startup_failure with why.
        fake.ready.store(false, Ordering::SeqCst);
        let resp = http
            .post(format!("{url}/api/v1/repos/acme%2Fdemo/jobs/{id}/retry"))
            .bearer_auth(&owner)
            .send()
            .await
            .unwrap();
        assert_eq!(resp.status(), 200);
        assert!(runner.once(&fake).await.unwrap());
        let ok = checks().await.into_iter().find(|c| c.name == "ok").unwrap();
        assert_eq!(ok.attempt, Some(2));
        assert_eq!(ok.conclusion.as_deref(), Some("startup_failure"));
        assert!(ok.details.as_deref().unwrap().contains("switched off"));
        let diag = app.ci.diagnostics().await;
        assert_eq!(diag["state"], "executor_unavailable", "{diag}");
        assert!(!runner.once(&fake).await.unwrap(), "nothing left");
    }
}
