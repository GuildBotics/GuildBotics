//! branvil-bench: a reproducible load tool for `branvil serve`.
//!
//! It seeds a fixture through the HTTP API and Git Smart HTTP only (never by
//! writing redb), then measures the operations the GuildBotics port performs
//! (`docs/port-mapping.md`) at several concurrency levels, the service's
//! startup time, memory, storage queues, and file sizes, and Git transfers.
//! Results go to `<work>/results-*.json` and a Markdown summary to stdout.
//! See `docs/performance.md` for how it was run and what it measured.
//!
//! ```text
//! cargo build --release
//! cargo run --release --example bench -- seed  --work /tmp/bench
//! cargo run --release --example bench -- run   --work /tmp/bench --cache-mib 32
//! cargo run --release --example bench -- git   --work /tmp/bench
//! cargo run --release --example bench -- startup --work /tmp/bench
//! ```
//!
//! Every Git client the tool starts is isolated from the user's
//! configuration: `GIT_CONFIG_NOSYSTEM=1`, an empty `GIT_CONFIG_GLOBAL`,
//! `GIT_TERMINAL_PROMPT=0`, `-c credential.helper=`, and credentials only
//! through a `GIT_ASKPASS` script that reads them from the environment.

use std::collections::BTreeMap;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use clap::{Args, Parser, Subcommand};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::Semaphore;

#[derive(Parser)]
#[command(name = "branvil-bench", about = "Seed and measure a Branvil service")]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Create a data directory and seed the fixture through the API
    Seed(SeedArgs),
    /// Measure startup, port operations, memory, queues, and sizes on a copy of the seeded data
    Run(RunArgs),
    /// Measure clone, fetch, and push over Smart HTTP
    Git(GitArgs),
    /// Start and stop the service on a copy of the seeded data several times
    Startup(StartupArgs),
}

#[derive(Args, Clone)]
struct StartupArgs {
    #[command(flatten)]
    common: Common,
    #[arg(long, default_value_t = 5)]
    repeat: usize,
    #[arg(long, default_value_t = 32)]
    cache_mib: usize,
}

#[derive(Args, Clone)]
struct Common {
    /// The branvil binary (a release build)
    #[arg(long, default_value = "target/release/branvil")]
    bin: PathBuf,
    /// Working directory of the benchmark (data, fixture, results)
    #[arg(long)]
    work: PathBuf,
}

#[derive(Args, Clone)]
struct SeedArgs {
    #[command(flatten)]
    common: Common,
    #[arg(long, default_value_t = 10_000)]
    tickets: usize,
    #[arg(long, default_value_t = 2_000)]
    pulls: usize,
    /// Conversation comments on tickets and pull requests together
    #[arg(long, default_value_t = 50_000)]
    comments: usize,
    #[arg(long, default_value_t = 4)]
    repos: usize,
    /// Workspaces, each with its board; the repositories are spread over them in order
    #[arg(long, default_value_t = 2)]
    workspaces: usize,
    #[arg(long, default_value_t = 8)]
    agents: usize,
    /// Branches per repository without a pull request (heads for `pr_create`)
    #[arg(long, default_value_t = 400)]
    spare_branches: usize,
    /// Concurrent seeding requests
    #[arg(long, default_value_t = 8)]
    concurrency: usize,
    #[arg(long, default_value_t = 32)]
    cache_mib: usize,
}

#[derive(Args, Clone)]
struct RunArgs {
    #[command(flatten)]
    common: Common,
    #[arg(long, default_value_t = 32)]
    cache_mib: usize,
    /// Concurrency levels
    #[arg(long, value_delimiter = ',', default_value = "1,8,32")]
    concurrency: Vec<usize>,
    /// Operations per scenario (operation × concurrency)
    #[arg(long, default_value_t = 200)]
    requests: usize,
    /// Scenarios to run (default: all)
    #[arg(long, value_delimiter = ',')]
    only: Vec<String>,
    /// Name of this run in the results file
    #[arg(long)]
    label: Option<String>,
}

#[derive(Args, Clone)]
struct GitArgs {
    #[command(flatten)]
    common: Common,
    /// Commits of the medium repository
    #[arg(long, default_value_t = 3_000)]
    medium_commits: usize,
    /// Files of the medium repository
    #[arg(long, default_value_t = 5_000)]
    medium_files: usize,
    #[arg(long, default_value_t = 3)]
    repeat: usize,
}

// ---------------------------------------------------------------- utilities

/// splitmix64: deterministic, so every seed of the same size is the same.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Self(seed)
    }
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        z ^ (z >> 31)
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n.max(1) as u64) as usize
    }
    fn chance(&mut self, p: f64) -> bool {
        (self.next() >> 11) as f64 / (1u64 << 53) as f64 <= p
    }
    fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[self.below(items.len())]
    }
}

const WORDS: &[&str] = &[
    "the", "service", "returns", "ticket", "board", "lane", "review", "commit", "branch", "merge",
    "check", "runner", "token", "member", "request", "response", "error", "index", "record",
    "value", "fails", "when", "after", "before", "should", "update", "remove", "add", "test",
    "build", "cache", "config", "path", "file", "line", "read", "write", "lock", "queue", "retry",
    "timeout", "page", "cursor", "label", "comment", "thread", "head", "base", "draft", "ready",
];

fn words(rng: &mut Rng, n: usize) -> String {
    let mut out = String::new();
    for i in 0..n {
        if i > 0 {
            out.push(' ');
        }
        out.push_str(rng.pick(WORDS));
    }
    out
}

/// Between `min` and `min + spread - 1` words.
fn text(rng: &mut Rng, min: usize, spread: usize) -> String {
    let n = min + rng.below(spread);
    words(rng, n)
}

fn source_lines(rng: &mut Rng, min: usize, spread: usize) -> String {
    let n = min + rng.below(spread);
    source_file(rng, n)
}

fn enc(path: &str) -> String {
    path.replace('/', "%2F")
}

fn ms(d: Duration) -> f64 {
    d.as_secs_f64() * 1000.0
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let rank = ((p / 100.0) * sorted.len() as f64).ceil() as usize;
    sorted[rank.clamp(1, sorted.len()) - 1]
}

#[derive(Serialize, Clone, Debug, Default)]
struct Summary {
    count: usize,
    errors: usize,
    p50_ms: f64,
    p95_ms: f64,
    max_ms: f64,
    mean_ms: f64,
}

fn summarize(samples: &[f64], errors: usize) -> Summary {
    let mut s = samples.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Summary {
        count: s.len(),
        errors,
        p50_ms: percentile(&s, 50.0),
        p95_ms: percentile(&s, 95.0),
        max_ms: s.last().copied().unwrap_or(0.0),
        mean_ms: if s.is_empty() {
            0.0
        } else {
            s.iter().sum::<f64>() / s.len() as f64
        },
    }
}

fn du_bytes(path: &Path) -> u64 {
    let out = Command::new("du").arg("-sk").arg(path).output();
    out.ok()
        .and_then(|o| {
            String::from_utf8_lossy(&o.stdout)
                .split_whitespace()
                .next()
                .and_then(|n| n.parse::<u64>().ok())
        })
        .unwrap_or(0)
        * 1024
}

fn mib(bytes: u64) -> f64 {
    bytes as f64 / (1024.0 * 1024.0)
}

fn free_port() -> Result<u16> {
    let l = std::net::TcpListener::bind("127.0.0.1:0")?;
    Ok(l.local_addr()?.port())
}

fn load_average() -> String {
    Command::new("sysctl")
        .args(["-n", "vm.loadavg"])
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default()
}

// ---------------------------------------------------------------- the service process

/// `branvil serve` under `/usr/bin/time -l`, which reports the exact peak RSS
/// when the service exits.
struct Service {
    time: Child,
    pid: u32,
    url: String,
    log: PathBuf,
    startup: Duration,
    stopped: bool,
}

impl Drop for Service {
    /// A run that fails part-way does not leave the service running.
    fn drop(&mut self) {
        if !self.stopped {
            let _ = Command::new("kill")
                .args(["-TERM", &self.pid.to_string()])
                .status();
            let _ = self.time.wait();
        }
    }
}

impl Service {
    fn start(bin: &Path, data: &Path, log: &Path) -> Result<Service> {
        let log_file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log)?;
        let started = Instant::now();
        let mut time = Command::new("/usr/bin/time")
            .arg("-l")
            .arg(bin)
            .args(["serve", "--data-dir"])
            .arg(data)
            .env("BRANVIL_LOG", "branvil=info")
            .env_remove("BRANVIL_FAILPOINT")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(log_file)
            .spawn()
            .context("spawn branvil serve")?;
        let stdout = time.stdout.take().unwrap();
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        reader.read_line(&mut line)?;
        let startup = started.elapsed();
        let url = line
            .strip_prefix("branvil listening on ")
            .and_then(|l| l.split_whitespace().next())
            .with_context(|| format!("unexpected first line {line:?}; see {}", log.display()))?
            .to_string();
        // Keep draining stdout so the service never blocks on it.
        std::thread::spawn(move || {
            let mut sink = String::new();
            while reader.read_line(&mut sink).unwrap_or(0) > 0 {
                sink.clear();
            }
        });
        let pid = child_pid(time.id())?;
        Ok(Service {
            time,
            pid,
            url,
            log: log.to_path_buf(),
            startup,
            stopped: false,
        })
    }

    /// Stops the service (SIGTERM, graceful) and returns its peak RSS and CPU
    /// times as reported by `time -l`.
    fn stop(mut self) -> Result<ExitReport> {
        let _ = Command::new("kill")
            .args(["-TERM", &self.pid.to_string()])
            .status();
        let _ = self.time.wait();
        self.stopped = true;
        let text = std::fs::read_to_string(&self.log).unwrap_or_default();
        let tail: Vec<&str> = text.lines().rev().take(40).collect();
        let find = |label: &str| -> Option<u64> {
            tail.iter().find_map(|l| {
                let l = l.trim();
                l.strip_suffix(label)
                    .and_then(|n| n.trim().parse::<u64>().ok())
            })
        };
        let secs = |label: &str| -> Option<f64> {
            tail.iter().find_map(|l| {
                let mut it = l.split_whitespace();
                let mut prev: Option<&str> = None;
                for w in it.by_ref() {
                    if w == label {
                        return prev.and_then(|p| p.parse().ok());
                    }
                    prev = Some(w);
                }
                None
            })
        };
        Ok(ExitReport {
            peak_rss_bytes: find("maximum resident set size").unwrap_or(0),
            peak_footprint_bytes: find("peak memory footprint").unwrap_or(0),
            user_secs: secs("user").unwrap_or(0.0),
            sys_secs: secs("sys").unwrap_or(0.0),
            real_secs: secs("real").unwrap_or(0.0),
        })
    }
}

#[derive(Serialize, Clone, Debug, Default)]
struct ExitReport {
    peak_rss_bytes: u64,
    peak_footprint_bytes: u64,
    user_secs: f64,
    sys_secs: f64,
    real_secs: f64,
}

fn child_pid(parent: u32) -> Result<u32> {
    for _ in 0..200 {
        let out = Command::new("pgrep")
            .args(["-P", &parent.to_string()])
            .output()?;
        if let Some(pid) = String::from_utf8_lossy(&out.stdout)
            .lines()
            .next()
            .and_then(|l| l.trim().parse().ok())
        {
            return Ok(pid);
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    bail!("branvil serve did not start under time")
}

/// RSS (KiB) and CPU seconds of a process, from `ps`.
fn ps_sample(pid: u32) -> Option<(u64, f64)> {
    let out = Command::new("ps")
        .args(["-o", "rss=,cputime=", "-p", &pid.to_string()])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let mut it = text.split_whitespace();
    let rss = it.next()?.parse().ok()?;
    let cpu = parse_cputime(it.next()?)?;
    Some((rss, cpu))
}

fn parse_cputime(text: &str) -> Option<f64> {
    // [[hh:]mm:]ss.cc
    let mut secs = 0.0;
    for part in text.split(':') {
        secs = secs * 60.0 + part.parse::<f64>().ok()?;
    }
    Some(secs)
}

/// Samples the service's RSS every 100 ms.
struct Sampler {
    stop: Arc<AtomicBool>,
    samples: Arc<Mutex<Vec<(Instant, u64)>>>,
    handle: Option<std::thread::JoinHandle<()>>,
}

impl Sampler {
    fn start(pid: u32) -> Sampler {
        let stop = Arc::new(AtomicBool::new(false));
        let samples = Arc::new(Mutex::new(Vec::new()));
        let (s2, v2) = (stop.clone(), samples.clone());
        let handle = std::thread::spawn(move || {
            while !s2.load(Ordering::Relaxed) {
                if let Some((rss, _)) = ps_sample(pid) {
                    v2.lock().unwrap().push((Instant::now(), rss));
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        });
        Sampler {
            stop,
            samples,
            handle: Some(handle),
        }
    }

    /// The largest sample since `since`, or the last one before it when
    /// the window was shorter than the sampling interval.
    fn max_since(&self, since: Instant) -> u64 {
        let samples = self.samples.lock().unwrap();
        samples
            .iter()
            .filter(|(t, _)| *t >= since)
            .map(|(_, r)| *r)
            .max()
            .or_else(|| samples.last().map(|(_, r)| *r))
            .unwrap_or(0)
    }

    fn finish(mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
    }
}

// ---------------------------------------------------------------- HTTP client

#[derive(Clone)]
struct Api {
    http: reqwest::Client,
    base: String,
}

struct Reply {
    status: u16,
    body: Value,
    elapsed: Duration,
}

impl Api {
    fn new(base: &str) -> Api {
        Api {
            http: reqwest::Client::builder()
                .pool_max_idle_per_host(128)
                .timeout(Duration::from_secs(300))
                .build()
                .unwrap(),
            base: base.to_string(),
        }
    }

    async fn call(
        &self,
        method: reqwest::Method,
        path: &str,
        token: &str,
        body: Option<&Value>,
        headers: &[(&str, &str)],
    ) -> Result<Reply> {
        let mut req = self
            .http
            .request(method, format!("{}/api/v1{path}", self.base))
            .bearer_auth(token);
        for (k, v) in headers {
            req = req.header(*k, *v);
        }
        if let Some(b) = body {
            req = req.json(b);
        }
        let started = Instant::now();
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let bytes = resp.bytes().await?;
        let elapsed = started.elapsed();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes)
                .unwrap_or_else(|_| Value::String(String::from_utf8_lossy(&bytes).into()))
        };
        Ok(Reply {
            status,
            body,
            elapsed,
        })
    }

    async fn get(&self, path: &str, token: &str) -> Result<Reply> {
        self.call(reqwest::Method::GET, path, token, None, &[])
            .await
    }

    async fn send(
        &self,
        method: reqwest::Method,
        path: &str,
        token: &str,
        body: Value,
    ) -> Result<Reply> {
        self.call(method, path, token, Some(&body), &[]).await
    }

    /// A request that must succeed (seeding).
    async fn must(
        &self,
        method: reqwest::Method,
        path: &str,
        token: &str,
        body: Option<Value>,
    ) -> Result<Value> {
        let r = self
            .call(method.clone(), path, token, body.as_ref(), &[])
            .await?;
        if !(200..300).contains(&r.status) {
            bail!("{method} {path}: {} {}", r.status, r.body);
        }
        Ok(r.body)
    }
}

// ---------------------------------------------------------------- Git clients

/// Runs `git` isolated from the user's configuration and credential stores.
fn git(home: &Path, dir: &Path, args: &[&str], auth: Option<(&str, &str)>) -> Result<String> {
    let out = git_cmd(home, dir, args, auth).output()?;
    if !out.status.success() {
        bail!(
            "git {args:?} failed: {}{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn git_cmd(home: &Path, dir: &Path, args: &[&str], auth: Option<(&str, &str)>) -> Command {
    std::fs::create_dir_all(home).unwrap();
    let global = home.join("gitconfig");
    if !global.exists() {
        std::fs::write(&global, "").unwrap();
    }
    let askpass = home.join("askpass.sh");
    if !askpass.exists() {
        std::fs::write(
            &askpass,
            "#!/bin/sh\ncase \"$1\" in\n  Username*) printf '%s\\n' \"$BENCH_GIT_USER\" ;;\n  *) printf '%s\\n' \"$BENCH_GIT_TOKEN\" ;;\nesac\n",
        )
        .unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&askpass, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let mut cmd = Command::new("git");
    for (k, _) in std::env::vars() {
        if k.starts_with("GIT_") || k == "SSH_ASKPASS" {
            cmd.env_remove(k);
        }
    }
    cmd.current_dir(dir)
        .args(["-c", "credential.helper=", "-c", "core.askPass="])
        .args(args)
        .env("HOME", home)
        .env("XDG_CONFIG_HOME", home)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", &global)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_AUTHOR_NAME", "Bench")
        .env("GIT_AUTHOR_EMAIL", "bench@example.invalid")
        .env("GIT_COMMITTER_NAME", "Bench")
        .env("GIT_COMMITTER_EMAIL", "bench@example.invalid")
        .stdin(Stdio::null());
    match auth {
        Some((user, token)) => {
            cmd.env("GIT_ASKPASS", &askpass)
                .env("BENCH_GIT_USER", user)
                .env("BENCH_GIT_TOKEN", token);
        }
        None => {
            cmd.env("GIT_ASKPASS", "/usr/bin/false");
        }
    }
    cmd
}

/// Feeds a `git fast-import` stream into a new repository at `dir`.
fn fast_import(home: &Path, dir: &Path, stream: &[u8]) -> Result<()> {
    std::fs::create_dir_all(dir)?;
    git(home, dir, &["init", "-q", "-b", "main"], None)?;
    let mut child = git_cmd(home, dir, &["fast-import", "--quiet"], None)
        .stdin(Stdio::piped())
        .spawn()?;
    child.stdin.take().unwrap().write_all(stream)?;
    let status = child.wait()?;
    if !status.success() {
        bail!("fast-import failed");
    }
    Ok(())
}

struct Stream(Vec<u8>);

impl Stream {
    fn data(&mut self, bytes: &[u8]) {
        self.0
            .extend_from_slice(format!("data {}\n", bytes.len()).as_bytes());
        self.0.extend_from_slice(bytes);
        self.0.push(b'\n');
    }
    fn commit(&mut self, branch: &str, mark: usize, time: u64, msg: &str, from: Option<usize>) {
        self.0.extend_from_slice(
            format!(
                "commit refs/heads/{branch}\nmark :{mark}\ncommitter Bench <bench@example.invalid> {time} +0000\n"
            )
            .as_bytes(),
        );
        self.data(msg.as_bytes());
        if let Some(f) = from {
            self.0.extend_from_slice(format!("from :{f}\n").as_bytes());
        }
    }
    fn file(&mut self, path: &str, content: &[u8]) {
        self.0
            .extend_from_slice(format!("M 100644 inline {path}\n").as_bytes());
        self.data(content);
    }
}

fn source_file(rng: &mut Rng, lines: usize) -> String {
    let mut s = String::new();
    for _ in 0..lines {
        let n = 3 + rng.below(9);
        s.push_str(&words(rng, n));
        s.push('\n');
    }
    s
}

const PIPELINE: &str = "version = 1\n\n[[jobs]]\nname = \"test\"\nimage = \"docker.io/library/alpine:3.22\"\ncommands = [\"./test.sh\"]\n";

/// A fixture repository: `main` with a small tree and a pipeline, `pr/<n>`
/// branches each one commit ahead, and `spare/<n>` branches the same way.
fn fixture_repo_stream(rng: &mut Rng, pulls: usize, spares: usize) -> Vec<u8> {
    let mut s = Stream(Vec::new());
    let t0 = 1_760_000_000u64;
    s.commit("main", 1, t0, "Initial import\n", None);
    s.file(".branvil/ci.toml", PIPELINE.as_bytes());
    s.file("test.sh", b"#!/bin/sh\nexit 0\n");
    for f in 0..200 {
        let content = source_lines(rng, 30, 40);
        s.file(&format!("src/mod_{f}.txt"), content.as_bytes());
    }
    let mut mark = 2;
    for (prefix, n) in [("pr", pulls), ("spare", spares)] {
        for i in 1..=n {
            s.commit(
                &format!("{prefix}/{i}"),
                mark,
                t0 + mark as u64,
                &format!("Change {prefix} {i}\n"),
                Some(1),
            );
            s.file(
                &format!("changes/{prefix}-{i}.txt"),
                source_file(rng, 10).as_bytes(),
            );
            let f = rng.below(200);
            s.file(
                &format!("src/mod_{f}.txt"),
                source_lines(rng, 30, 40).as_bytes(),
            );
            mark += 1;
        }
    }
    s.0
}

// ---------------------------------------------------------------- fixture

#[derive(Serialize, Deserialize, Clone, Debug)]
struct Tok {
    name: String,
    token: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct TicketFx {
    repo: usize,
    key: String,
    lane: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct PullFx {
    repo: usize,
    number: u64,
    head: String,
    head_sha: String,
    open: bool,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct Fixture {
    port: u16,
    owner: Tok,
    /// ken and mia, agents like the numbered ones.
    named: Vec<Tok>,
    agents: Vec<Tok>,
    runner: Tok,
    repos: Vec<String>,
    workspaces: Vec<String>,
    labels: Vec<String>,
    tickets: Vec<TicketFx>,
    pulls: Vec<PullFx>,
    /// (repo index, branch)
    spares: Vec<(usize, String)>,
    counts: BTreeMap<String, u64>,
    seed: Vec<Phase>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct Phase {
    name: String,
    requests: u64,
    secs: f64,
    per_sec: f64,
}

fn seeded_dir(work: &Path) -> PathBuf {
    work.join("seeded")
}

fn write_config(data: &Path, port: u16, cache_mib: usize) -> Result<()> {
    std::fs::write(
        data.join("branvil.toml"),
        format!(
            "listen = \"127.0.0.1:{port}\"\nbase_url = \"http://127.0.0.1:{port}\"\n\n[storage]\ncache_size_mib = {cache_mib}\n\n[ci]\n# No runner contacts the service after seeding; keep queued attempts queued.\nrunner_absent_secs = 864000\n"
        ),
    )?;
    Ok(())
}

/// Runs `n` tasks with at most `concurrency` in flight; returns the elapsed time.
async fn fan_out<F, Fut>(n: usize, concurrency: usize, f: F) -> Result<Duration>
where
    F: Fn(usize) -> Fut,
    Fut: std::future::Future<Output = Result<()>> + Send + 'static,
{
    let started = Instant::now();
    let sem = Arc::new(Semaphore::new(concurrency));
    let mut tasks = Vec::with_capacity(n);
    for i in 0..n {
        let permit = sem.clone().acquire_owned().await.unwrap();
        let fut = f(i);
        tasks.push(tokio::spawn(async move {
            let out = fut.await;
            drop(permit);
            out
        }));
    }
    for t in tasks {
        t.await??;
    }
    Ok(started.elapsed())
}

fn phase(seed: &mut Vec<Phase>, name: &str, requests: u64, took: Duration) {
    let secs = took.as_secs_f64();
    let p = Phase {
        name: name.to_string(),
        requests,
        secs,
        per_sec: requests as f64 / secs.max(1e-9),
    };
    eprintln!(
        "seed {:<28} {:>7} requests in {:>8.1} s = {:>7.1}/s",
        p.name, p.requests, p.secs, p.per_sec
    );
    seed.push(p);
}

const LABELS: &[&str] = &["bug", "feature", "docs", "ui", "perf", "security"];

async fn seed(a: SeedArgs) -> Result<()> {
    let work = &a.common.work;
    let data = seeded_dir(work);
    if data.exists() {
        bail!("{} exists; use a new --work directory", data.display());
    }
    std::fs::create_dir_all(work)?;
    let bin = std::fs::canonicalize(&a.common.bin)?;
    let port = free_port()?;
    let out = Command::new(&bin)
        .args(["init", "--owner", "alice", "--listen"])
        .arg(format!("127.0.0.1:{port}"))
        .arg("--data-dir")
        .arg(&data)
        .output()?;
    if !out.status.success() {
        bail!("init: {}", String::from_utf8_lossy(&out.stderr));
    }
    let owner_token = std::fs::read_to_string(data.join("secrets").join("owner.token"))
        .context("owner token")?
        .trim()
        .to_string();
    write_config(&data, port, a.cache_mib)?;
    let svc = Service::start(&bin, &data, &work.join("seed-serve.log"))?;
    let api = Api::new(&svc.url);
    let owner = Tok {
        name: "alice".into(),
        token: owner_token,
    };
    let mut rng = Rng::new(0xb7a4_0001);
    let mut seed_phases = Vec::new();
    let mut counts: BTreeMap<String, u64> = BTreeMap::new();
    use reqwest::Method as M;

    // Members, tokens.
    let started = Instant::now();
    let member = |name: &str, kind: &str| (name.to_string(), kind.to_string());
    let mut people = vec![member("ken", "agent"), member("mia", "agent")];
    for i in 0..a.agents {
        people.push(member(&format!("agent-{i}"), "agent"));
    }
    people.push(member("ci-runner", "runner"));
    let mut toks = BTreeMap::new();
    for (name, kind) in &people {
        api.must(
            M::POST,
            "/members",
            &owner.token,
            Some(json!({"name": name, "kind": kind})),
        )
        .await?;
        let t = api
            .must(
                M::POST,
                &format!("/members/{name}/tokens"),
                &owner.token,
                Some(json!({"label": "bench"})),
            )
            .await?;
        toks.insert(
            name.clone(),
            Tok {
                name: name.clone(),
                token: t["token"].as_str().unwrap().to_string(),
            },
        );
    }
    let named = vec![toks["ken"].clone(), toks["mia"].clone()];
    let agents: Vec<Tok> = (0..a.agents)
        .map(|i| toks[&format!("agent-{i}")].clone())
        .collect();
    let runner = toks["ci-runner"].clone();
    let writers: Vec<Tok> = named.iter().chain(agents.iter()).cloned().collect();

    // Workspaces, and the repositories with their labels spread over them.
    let workspaces: Vec<String> = (0..a.workspaces).map(|i| format!("acme-{i}")).collect();
    let repos: Vec<String> = (0..a.repos)
        .map(|i| format!("{}/app-{i}", workspaces[i * a.workspaces / a.repos]))
        .collect();
    for w in &workspaces {
        api.must(
            M::POST,
            "/workspaces",
            &owner.token,
            Some(json!({"name": w})),
        )
        .await?;
    }
    for r in &repos {
        api.must(
            M::POST,
            "/repos",
            &owner.token,
            Some(json!({"path": r, "labels": LABELS})),
        )
        .await?;
    }
    let n_setup = (people.len() * 2 + workspaces.len() + repos.len()) as u64;
    phase(
        &mut seed_phases,
        "members, workspaces, repos",
        n_setup,
        started.elapsed(),
    );

    // Git: main, one branch per pull request, spare branches.
    let home = work.join("git-home");
    let pulls_per_repo = a.pulls.div_ceil(a.repos);
    let mut head_shas: Vec<BTreeMap<String, String>> = Vec::new();
    let started = Instant::now();
    let mut refs_pushed = 0u64;
    for (i, r) in repos.iter().enumerate() {
        let local = work.join("fixture-src").join(format!("app-{i}"));
        let stream = fixture_repo_stream(&mut rng, pulls_per_repo, a.spare_branches);
        fast_import(&home, &local, &stream)?;
        let url = format!("{}/git/{r}.git", svc.url);
        git(
            &home,
            &local,
            &["push", "-q", &url, "main"],
            Some(("alice", &owner.token)),
        )?;
        git(
            &home,
            &local,
            &["push", "-q", &url, "refs/heads/*:refs/heads/*"],
            Some(("alice", &owner.token)),
        )?;
        let listing = git(
            &home,
            &local,
            &[
                "for-each-ref",
                "--format=%(refname:short) %(objectname)",
                "refs/heads",
            ],
            None,
        )?;
        let map: BTreeMap<String, String> = listing
            .lines()
            .filter_map(|l| l.split_once(' '))
            .map(|(a, b)| (a.to_string(), b.to_string()))
            .collect();
        refs_pushed += map.len() as u64;
        head_shas.push(map);
        // Required check `test`: the readiness path has CI to report.
        let repo = api
            .must(M::GET, &format!("/repos/{}", enc(r)), &owner.token, None)
            .await?;
        api.must(
            M::PATCH,
            &format!("/repos/{}", enc(r)),
            &owner.token,
            Some(json!({"revision": repo["revision"], "required_checks": ["test"]})),
        )
        .await?;
    }
    phase(
        &mut seed_phases,
        "git push (refs)",
        refs_pushed,
        started.elapsed(),
    );
    counts.insert("git_refs".into(), refs_pushed);

    // Tickets: created on their board lane, assigned, labelled, closed when done.
    let mut plan = Vec::new();
    for n in 0..a.tickets {
        let repo = n % repos.len();
        let roll = rng.below(100);
        let lane = match roll {
            0..=24 => "new",
            25..=30 => "ready",
            31..=34 => "in_progress",
            _ => "done",
        };
        let assignee = if lane == "new" && !rng.chance(0.3) {
            None
        } else {
            Some(rng.pick(&agents).name.clone())
        };
        let labels: Vec<&str> = (0..rng.below(4)).map(|_| *rng.pick(LABELS)).collect();
        let mut labels: Vec<String> = labels.iter().map(|s| s.to_string()).collect();
        labels.sort();
        labels.dedup();
        let author = rng.pick(&writers).clone();
        let title = text(&mut rng, 4, 6);
        let body = text(&mut rng, 40, 120);
        plan.push((repo, lane, assignee, labels, author, title, body));
    }
    let plan = Arc::new(plan);
    let created: Arc<Mutex<Vec<Option<TicketFx>>>> = Arc::new(Mutex::new(vec![None; a.tickets]));
    let requests = Arc::new(AtomicUsize::new(0));
    let repos_a = Arc::new(repos.clone());
    let took = fan_out(a.tickets, a.concurrency, |n| {
        let (api, plan, created, requests) =
            (api.clone(), plan.clone(), created.clone(), requests.clone());
        let repos = repos_a.clone();
        async move {
            let (repo, lane, assignee, labels, author, title, body) = &plan[n];
            let rpath = &repos[*repo];
            let t = api
                .must(
                    M::POST,
                    &format!("/repos/{}/tickets", enc(rpath)),
                    &author.token,
                    Some(json!({"title": title, "body": body, "labels": labels, "lane": lane})),
                )
                .await?;
            let key = t["key"].as_str().unwrap().to_string();
            let mut count = 1;
            if let Some(name) = assignee {
                api.must(
                    M::PUT,
                    &format!("/repos/{}/tickets/{key}/assignee", enc(rpath)),
                    &author.token,
                    Some(json!({"name": name})),
                )
                .await?;
                count += 1;
            }
            if *lane == "done" {
                api.must(
                    M::PATCH,
                    &format!("/repos/{}/tickets/{key}", enc(rpath)),
                    &author.token,
                    Some(json!({"state": "closed"})),
                )
                .await?;
                count += 1;
            }
            requests.fetch_add(count, Ordering::Relaxed);
            created.lock().unwrap()[n] = Some(TicketFx {
                repo: *repo,
                key,
                lane: lane.to_string(),
            });
            Ok(())
        }
    })
    .await?;
    let tickets: Vec<TicketFx> = created.lock().unwrap().iter().flatten().cloned().collect();
    phase(
        &mut seed_phases,
        "tickets (create, assign, close)",
        requests.load(Ordering::Relaxed) as u64,
        took,
    );
    counts.insert("tickets".into(), tickets.len() as u64);

    // Pull requests: one per pr/<n> branch, linked to a ticket of the repository.
    let mut pull_plan = Vec::new();
    for n in 0..a.pulls {
        let repo = n % a.repos;
        let branch = format!("pr/{}", n / a.repos + 1);
        let candidates: Vec<&TicketFx> = tickets.iter().filter(|t| t.repo == repo).collect();
        let ticket = (*rng.pick(&candidates)).clone();
        let author = rng.pick(&agents).clone();
        let others: Vec<&Tok> = writers.iter().filter(|w| w.name != author.name).collect();
        let reviewer = if rng.chance(0.5) {
            Some(rng.pick(&others).name.clone())
        } else {
            None
        };
        let title = text(&mut rng, 4, 6);
        let body = text(&mut rng, 30, 80);
        pull_plan.push((repo, branch, ticket, author, reviewer, title, body));
    }
    let pull_plan = Arc::new(pull_plan);
    let made: Arc<Mutex<Vec<Option<PullFx>>>> = Arc::new(Mutex::new(vec![None; a.pulls]));
    let requests = Arc::new(AtomicUsize::new(0));
    let heads_a = Arc::new(head_shas.clone());
    let base_url = svc.url.clone();
    let took = fan_out(a.pulls, a.concurrency, |n| {
        let (api, plan, made, requests) = (
            api.clone(),
            pull_plan.clone(),
            made.clone(),
            requests.clone(),
        );
        let (repos, heads, base_url) = (repos_a.clone(), heads_a.clone(), base_url.clone());
        async move {
            let (repo, branch, ticket, author, reviewer, title, body) = &plan[n];
            let url = format!("{base_url}/{}/-/tickets/{}", repos[ticket.repo], ticket.key);
            let p = api
                .must(
                    M::POST,
                    &format!("/repos/{}/pulls", enc(&repos[*repo])),
                    &author.token,
                    Some(json!({"head": branch, "title": title, "body": body,
                                "tickets": [{"url": url, "closes": true}]})),
                )
                .await?;
            let number = p["pull"]["number"].as_u64().unwrap();
            let mut count = 1;
            if let Some(name) = reviewer {
                api.must(
                    M::PUT,
                    &format!(
                        "/repos/{}/pulls/{number}/reviewers/{name}",
                        enc(&repos[*repo])
                    ),
                    &author.token,
                    None,
                )
                .await?;
                count += 1;
            }
            requests.fetch_add(count, Ordering::Relaxed);
            made.lock().unwrap()[n] = Some(PullFx {
                repo: *repo,
                number,
                head: branch.clone(),
                head_sha: heads[*repo][branch].clone(),
                open: true,
            });
            Ok(())
        }
    })
    .await?;
    let mut pulls: Vec<PullFx> = made.lock().unwrap().iter().flatten().cloned().collect();
    phase(
        &mut seed_phases,
        "pull requests (create, reviewer)",
        requests.load(Ordering::Relaxed) as u64,
        took,
    );
    counts.insert("pulls".into(), pulls.len() as u64);

    // CI: the bench acts as a runner without an executor, concluding every
    // queued attempt (80% success) so checks have results. Not a CI measurement.
    let started = Instant::now();
    let mut ci_requests = 0u64;
    let mut attempts = 0u64;
    let log = "x".repeat(4096);
    let deadline = Instant::now() + Duration::from_secs(900);
    loop {
        let c = api
            .send(
                M::POST,
                "/runner/claim",
                &runner.token,
                json!({"executor": "microsandbox", "executor_state": "ready"}),
            )
            .await?;
        ci_requests += 1;
        if c.status == 204 {
            // Runs that did not fit the queue are admitted as room frees up.
            let d = api
                .must(M::GET, "/admin/diagnostics", &owner.token, None)
                .await?;
            let q = &d["runner"]["queue"];
            if q["queued"] == 0 && q["waiting_runs"] == 0 || Instant::now() > deadline {
                break;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
            continue;
        }
        if c.status != 200 {
            bail!("claim: {} {}", c.status, c.body);
        }
        let id = c.body["attempt_id"].as_str().unwrap().to_string();
        let lease = c.body["lease"].as_str().unwrap().to_string();
        let r = api
            .http
            .post(format!("{}/api/v1/runner/attempts/{id}/log", api.base))
            .bearer_auth(&runner.token)
            .header("X-Branvil-Lease", &lease)
            .body(log.clone())
            .send()
            .await?;
        if !r.status().is_success() {
            bail!("log: {}", r.status());
        }
        let conclusion = if rng.chance(0.8) {
            "success"
        } else {
            "failure"
        };
        api.must(
            M::POST,
            &format!("/runner/attempts/{id}/finish"),
            &runner.token,
            Some(json!({"lease": lease, "conclusion": conclusion})),
        )
        .await?;
        ci_requests += 2;
        attempts += 1;
    }
    phase(
        &mut seed_phases,
        "ci (claim, log, finish)",
        ci_requests,
        started.elapsed(),
    );
    counts.insert("ci_attempts".into(), attempts);

    // Reviews, inline threads with replies, PR and ticket comments.
    #[derive(Clone)]
    enum Job {
        Review(usize, Tok, String, String),
        Thread(usize, Tok, Tok, String),
        PullComment(usize, Tok, String),
        TicketComment(usize, Tok, String, bool),
    }
    let mut jobs = Vec::new();
    for (i, p) in pulls.iter().enumerate() {
        let verdict = *rng.pick(&["approve", "request_changes", "comment"]);
        jobs.push(Job::Review(
            i,
            rng.pick(&writers).clone(),
            verdict.to_string(),
            text(&mut rng, 10, 30),
        ));
        if i % 2 == 0 {
            jobs.push(Job::Thread(
                i,
                rng.pick(&writers).clone(),
                rng.pick(&agents).clone(),
                text(&mut rng, 8, 20),
            ));
        }
        let _ = p;
    }
    let pull_comments = a.comments * 3 / 10;
    for _ in 0..pull_comments {
        jobs.push(Job::PullComment(
            rng.below(pulls.len()),
            rng.pick(&writers).clone(),
            text(&mut rng, 8, 60),
        ));
    }
    // Ticket comments, skewed: a third of them on a tenth of the tickets.
    for c in 0..(a.comments - pull_comments) {
        let t = if c % 3 == 0 {
            rng.below(tickets.len().div_ceil(10))
        } else {
            rng.below(tickets.len())
        };
        jobs.push(Job::TicketComment(
            t,
            rng.pick(&writers).clone(),
            text(&mut rng, 8, 60),
            rng.chance(0.1),
        ));
    }
    // Interleave the kinds the way activity arrives.
    for i in (1..jobs.len()).rev() {
        let j = rng.below(i + 1);
        jobs.swap(i, j);
    }
    let jobs = Arc::new(jobs);
    let pulls_a = Arc::new(pulls.clone());
    let tickets_a = Arc::new(tickets.clone());
    let kinds: Arc<Mutex<BTreeMap<&'static str, u64>>> = Arc::new(Mutex::new(BTreeMap::new()));
    let took = fan_out(jobs.len(), a.concurrency, |n| {
        let (api, jobs, pulls, tickets, repos, kinds) = (
            api.clone(),
            jobs.clone(),
            pulls_a.clone(),
            tickets_a.clone(),
            repos_a.clone(),
            kinds.clone(),
        );
        async move {
            let (kind, n_req) = match &jobs[n] {
                Job::Review(i, who, verdict, body) => {
                    let p = &pulls[*i];
                    api.must(
                        M::POST,
                        &format!("/repos/{}/pulls/{}/reviews", enc(&repos[p.repo]), p.number),
                        &who.token,
                        Some(json!({"verdict": verdict, "body": body, "commit": p.head_sha})),
                    )
                    .await?;
                    ("reviews", 1)
                }
                Job::Thread(i, who, replier, body) => {
                    let p = &pulls[*i];
                    let base = format!("/repos/{}/pulls/{}", enc(&repos[p.repo]), p.number);
                    let path = format!("changes/{}.txt", p.head.replace('/', "-"));
                    let root = api
                        .must(
                            M::POST,
                            &format!("{base}/inline-comments"),
                            &who.token,
                            Some(json!({"body": body, "commit": p.head_sha, "path": path,
                                        "line": 1, "side": "RIGHT"})),
                        )
                        .await?;
                    let id = root["id"].as_str().unwrap();
                    api.must(
                        M::POST,
                        &format!("{base}/threads/{id}/replies"),
                        &replier.token,
                        Some(json!({"body": "Fixed in the next push."})),
                    )
                    .await?;
                    ("threads", 2)
                }
                Job::PullComment(i, who, body) => {
                    let p = &pulls[*i];
                    api.must(
                        M::POST,
                        &format!("/repos/{}/pulls/{}/comments", enc(&repos[p.repo]), p.number),
                        &who.token,
                        Some(json!({"body": body})),
                    )
                    .await?;
                    ("pull_comments", 1)
                }
                Job::TicketComment(i, who, body, status) => {
                    let t = &tickets[*i];
                    let meta = if *status {
                        json!({"kind": "workflow_status", "routing": "suppress", "reason": "in_progress"})
                    } else {
                        Value::Null
                    };
                    api.must(
                        M::POST,
                        &format!(
                            "/repos/{}/tickets/{}/comments",
                            enc(&repos[t.repo]),
                            t.key
                        ),
                        &who.token,
                        Some(json!({"body": body, "meta": meta})),
                    )
                    .await?;
                    ("ticket_comments", 1)
                }
            };
            *kinds.lock().unwrap().entry(kind).or_default() += n_req;
            Ok(())
        }
    })
    .await?;
    let total: u64 = kinds.lock().unwrap().values().sum();
    phase(&mut seed_phases, "reviews, threads, comments", total, took);
    for (k, v) in kinds.lock().unwrap().iter() {
        counts.insert(k.to_string(), *v);
    }

    // Close 70% of the pull requests (not merged: merges are fast-forward only).
    let to_close: Vec<usize> = (0..pulls.len()).filter(|_| rng.chance(0.7)).collect();
    let to_close_a = Arc::new(to_close.clone());
    let took = fan_out(to_close.len(), a.concurrency, |n| {
        let (api, pulls, repos, to_close, token) = (
            api.clone(),
            pulls_a.clone(),
            repos_a.clone(),
            to_close_a.clone(),
            named[0].token.clone(),
        );
        async move {
            let p = &pulls[to_close[n]];
            api.must(
                M::POST,
                &format!("/repos/{}/pulls/{}/close", enc(&repos[p.repo]), p.number),
                &token,
                None,
            )
            .await?;
            Ok(())
        }
    })
    .await?;
    for i in &to_close {
        pulls[*i].open = false;
    }
    phase(
        &mut seed_phases,
        "pull requests (close)",
        to_close.len() as u64,
        took,
    );
    counts.insert("pulls_closed".into(), to_close.len() as u64);

    let spares: Vec<(usize, String)> = (0..a.repos)
        .flat_map(|r| (1..=a.spare_branches).map(move |i| (r, format!("spare/{i}"))))
        .collect();
    let exit = svc.stop()?;
    counts.insert("seed_peak_rss_bytes".into(), exit.peak_rss_bytes);
    let fx = Fixture {
        port,
        owner,
        named,
        agents,
        runner,
        repos,
        workspaces,
        labels: LABELS.iter().map(|s| s.to_string()).collect(),
        tickets,
        pulls,
        spares,
        counts,
        seed: seed_phases,
    };
    std::fs::write(work.join("fixture.json"), serde_json::to_vec_pretty(&fx)?)?;
    eprintln!(
        "seeded: redb {:.1} MiB, repos {:.1} MiB, peak RSS {:.1} MiB",
        mib(std::fs::metadata(data.join("branvil.redb"))?.len()),
        mib(du_bytes(&data.join("repos"))),
        mib(exit.peak_rss_bytes)
    );
    Ok(())
}

// ---------------------------------------------------------------- measurement

#[derive(Clone)]
struct Ctx {
    api: Api,
    fx: Arc<Fixture>,
    /// Spare branches not used by `pr_create` yet.
    spares: Arc<Mutex<Vec<(usize, String)>>>,
    open_pulls: Arc<Vec<PullFx>>,
    /// Tickets that are not done (moved between new / ready / in progress).
    active_tickets: Arc<Vec<TicketFx>>,
    base_url: String,
}

/// One timed request of an operation.
type Timings = Vec<(&'static str, f64)>;

const OPS: &[&str] = &[
    "cards",
    "patrol",
    "readiness",
    "ticket_read",
    "comment_create",
    "comment_create_idem",
    "label_update",
    "pr_create",
    "pr_create_idem",
    "review_create",
    "ticket_move",
];

impl Ctx {
    fn agent(&self, worker: usize) -> &Tok {
        &self.fx.agents[worker % self.fx.agents.len()]
    }

    fn ok(r: &Reply, what: &str) -> Result<()> {
        if !(200..300).contains(&r.status) {
            bail!("{what}: {} {}", r.status, r.body);
        }
        Ok(())
    }

    /// Follows `next_cursor` to the end, as the port does for candidates.
    async fn all_pages(&self, path: &str, token: &str, name: &'static str) -> Result<Timings> {
        let mut out = Vec::new();
        let mut cursor: Option<String> = None;
        let mut total = 0.0;
        let mut items = 0;
        loop {
            let sep = if path.contains('?') { '&' } else { '?' };
            let url = match &cursor {
                Some(c) => format!("{path}{sep}limit=100&cursor={c}"),
                None => format!("{path}{sep}limit=100"),
            };
            let r = self.api.get(&url, token).await?;
            Self::ok(&r, name)?;
            total += ms(r.elapsed);
            out.push((page_name(name), ms(r.elapsed)));
            items += r.body["items"].as_array().map(|a| a.len()).unwrap_or(0);
            match r.body["next_cursor"].as_str() {
                Some(c) => cursor = Some(c.to_string()),
                None => break,
            }
        }
        let _ = items;
        out.push((name, total));
        Ok(out)
    }

    async fn op(&self, name: &str, worker: usize, rng: &mut Rng) -> Result<Timings> {
        use reqwest::Method as M;
        let me = self.agent(worker).clone();
        let fx = &self.fx;
        match name {
            "cards" => {
                let ws = rng.pick(&fx.workspaces);
                self.all_pages(
                    &format!("/workspaces/{ws}/board/cards?lane=ready,in_progress&assignee=me"),
                    &me.token,
                    "cards",
                )
                .await
            }
            "patrol" => {
                self.all_pages("/pulls?involves=me&state=open", &me.token, "patrol")
                    .await
            }
            "readiness" => {
                let p = rng.pick(&self.open_pulls);
                let base = format!("/repos/{}/pulls/{}", enc(&fx.repos[p.repo]), p.number);
                let mut out = Vec::new();
                let mut total = 0.0;
                for (sub, path) in [
                    ("readiness.pull", base.clone()),
                    ("readiness.activity", format!("{base}/activity")),
                    ("readiness.checks", format!("{base}/checks")),
                    ("readiness.pull", base.clone()),
                ] {
                    let r = self.api.get(&path, &me.token).await?;
                    Self::ok(&r, sub)?;
                    total += ms(r.elapsed);
                    out.push((sub, ms(r.elapsed)));
                }
                out.push(("readiness", total));
                Ok(out)
            }
            "ticket_read" => {
                let t = rng.pick(&fx.tickets);
                let base = format!("/repos/{}/tickets/{}", enc(&fx.repos[t.repo]), t.key);
                let a = self.api.get(&base, &me.token).await?;
                Self::ok(&a, "ticket")?;
                let b = self
                    .api
                    .get(&format!("{base}/comments?limit=30"), &me.token)
                    .await?;
                Self::ok(&b, "comments")?;
                Ok(vec![
                    ("ticket_read.ticket", ms(a.elapsed)),
                    ("ticket_read.comments", ms(b.elapsed)),
                    ("ticket_read", ms(a.elapsed) + ms(b.elapsed)),
                ])
            }
            "comment_create" | "comment_create_idem" => {
                let t = rng.pick(&fx.tickets);
                let path = format!(
                    "/repos/{}/tickets/{}/comments",
                    enc(&fx.repos[t.repo]),
                    t.key
                );
                let body = json!({"body": words(rng, 20)});
                let key = format!("bench-{}-{}", worker, rng.next());
                let headers: &[(&str, &str)] = if name == "comment_create_idem" {
                    &[("Idempotency-Key", key.as_str())]
                } else {
                    &[]
                };
                let r = self
                    .api
                    .call(M::POST, &path, &me.token, Some(&body), headers)
                    .await?;
                Self::ok(&r, name)?;
                Ok(vec![(
                    if name == "comment_create" {
                        "comment_create"
                    } else {
                        "comment_create_idem"
                    },
                    ms(r.elapsed),
                )])
            }
            "label_update" => {
                let t = rng.pick(&fx.tickets);
                let add = rng.pick(&fx.labels).clone();
                let remove = rng.pick(&fx.labels).clone();
                let r = self
                    .api
                    .send(
                        M::PATCH,
                        &format!(
                            "/repos/{}/tickets/{}",
                            enc(&fx.repos[t.repo]),
                            t.key
                        ),
                        &me.token,
                        json!({"add_labels": [add], "remove_labels": if add == remove { vec![] } else { vec![remove] }}),
                    )
                    .await?;
                Self::ok(&r, name)?;
                Ok(vec![("label_update", ms(r.elapsed))])
            }
            "pr_create" | "pr_create_idem" => {
                let (repo, branch) = self
                    .spares
                    .lock()
                    .unwrap()
                    .pop()
                    .context("no spare branch left for pr_create")?;
                let candidates: Vec<&TicketFx> =
                    fx.tickets.iter().filter(|t| t.repo == repo).collect();
                let t = rng.pick(&candidates);
                let url = format!("{}/{}/-/tickets/{}", self.base_url, fx.repos[t.repo], t.key);
                let key = format!("bench-pr-{repo}-{branch}");
                let headers: &[(&str, &str)] = if name == "pr_create_idem" {
                    &[("Idempotency-Key", key.as_str())]
                } else {
                    &[]
                };
                let body = json!({"head": branch, "title": words(rng, 6), "body": words(rng, 40),
                                  "tickets": [{"url": url, "closes": true}]});
                let r = self
                    .api
                    .call(
                        M::POST,
                        &format!("/repos/{}/pulls", enc(&fx.repos[repo])),
                        &me.token,
                        Some(&body),
                        headers,
                    )
                    .await?;
                Self::ok(&r, name)?;
                Ok(vec![(
                    if name == "pr_create" {
                        "pr_create"
                    } else {
                        "pr_create_idem"
                    },
                    ms(r.elapsed),
                )])
            }
            "review_create" => {
                let p = rng.pick(&self.open_pulls);
                let r = self
                    .api
                    .send(
                        M::POST,
                        &format!(
                            "/repos/{}/pulls/{}/reviews",
                            enc(&fx.repos[p.repo]),
                            p.number
                        ),
                        &me.token,
                        json!({"verdict": "comment", "body": words(rng, 15), "commit": p.head_sha}),
                    )
                    .await?;
                Self::ok(&r, name)?;
                Ok(vec![("review_create", ms(r.elapsed))])
            }
            "ticket_move" => {
                let t = rng.pick(&self.active_tickets);
                let rpath = &fx.repos[t.repo];
                let lane = *rng.pick(&["new", "ready", "in_progress"]);
                let r = self
                    .api
                    .send(
                        M::PUT,
                        &format!("/repos/{}/tickets/{}/board", enc(rpath), t.key),
                        &me.token,
                        json!({"lane": lane}),
                    )
                    .await?;
                Self::ok(&r, name)?;
                Ok(vec![("ticket_move", ms(r.elapsed))])
            }
            "ticket_move_done" => {
                // A move into the lane that holds most tickets.
                let t = rng.pick(&self.active_tickets);
                let rpath = &fx.repos[t.repo];
                let r = self
                    .api
                    .send(
                        M::PUT,
                        &format!("/repos/{}/tickets/{}/board", enc(rpath), t.key),
                        &me.token,
                        json!({"lane": "done"}),
                    )
                    .await?;
                Self::ok(&r, name)?;
                let back = self
                    .api
                    .send(
                        M::PUT,
                        &format!("/repos/{}/tickets/{}/board", enc(rpath), t.key),
                        &me.token,
                        json!({"lane": t.lane}),
                    )
                    .await?;
                Self::ok(&back, name)?;
                Ok(vec![
                    ("ticket_move_done", ms(r.elapsed)),
                    ("ticket_move_back", ms(back.elapsed)),
                ])
            }
            "mixed" => {
                let roll = rng.below(100);
                let op = match roll {
                    0..=9 => "cards",
                    10..=19 => "patrol",
                    20..=39 => "readiness",
                    40..=69 => "ticket_read",
                    70..=81 => "comment_create",
                    82..=87 => "label_update",
                    88..=93 => "ticket_move",
                    94..=97 => "review_create",
                    _ => "pr_create",
                };
                let mut out = Box::pin(self.op(op, worker, rng)).await?;
                let total = out
                    .iter()
                    .find(|(n, _)| *n == op)
                    .map(|(_, v)| *v)
                    .unwrap_or(0.0);
                out.push(("mixed", total));
                Ok(out)
            }
            other => bail!("unknown operation {other}"),
        }
    }
}

fn page_name(name: &'static str) -> &'static str {
    match name {
        "cards" => "cards.page",
        "patrol" => "patrol.page",
        _ => "page",
    }
}

/// Server-side storage queue statistics between two diagnostics snapshots.
#[derive(Serialize, Clone, Debug, Default)]
struct QueueDelta {
    writes: u64,
    write_wait_p50_us: u64,
    write_wait_p95_us: u64,
    write_wait_max_us: u64,
    write_hold_p50_us: u64,
    write_hold_p95_us: u64,
    commit_p50_us: u64,
    commit_p95_us: u64,
    queue_on_arrival_p50: u64,
    queue_on_arrival_p95: u64,
    queue_on_arrival_max: u64,
    reads: u64,
    read_wait_p95_us: u64,
    read_hold_p50_us: u64,
    read_hold_p95_us: u64,
}

fn buckets(v: &Value) -> BTreeMap<u64, u64> {
    v["buckets"]
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(|p| Some((p[0].as_u64()?, p[1].as_u64()?)))
                .collect()
        })
        .unwrap_or_default()
}

/// (count, p50, p95, largest bucket bound) of the histogram difference.
fn hist_delta(before: &Value, after: &Value) -> (u64, u64, u64, u64) {
    let b = buckets(before);
    let mut d: Vec<(u64, u64)> = buckets(after)
        .into_iter()
        .map(|(k, n)| (k, n - b.get(&k).copied().unwrap_or(0)))
        .filter(|(_, n)| *n > 0)
        .collect();
    d.sort();
    let count: u64 = d.iter().map(|(_, n)| n).sum();
    let at = |p: f64| -> u64 {
        let target = ((p / 100.0) * count as f64).ceil().max(1.0) as u64;
        let mut seen = 0;
        for (k, n) in &d {
            seen += n;
            if seen >= target {
                return *k;
            }
        }
        0
    };
    (
        count,
        at(50.0),
        at(95.0),
        d.last().map(|(k, _)| *k).unwrap_or(0),
    )
}

fn queue_delta(before: &Value, after: &Value) -> QueueDelta {
    let q = |v: &Value, a: &str, b: &str| v["storage"]["queues"][a][b].clone();
    let (writes, wp50, wp95, wmax) = hist_delta(
        &q(before, "writes", "wait_us"),
        &q(after, "writes", "wait_us"),
    );
    let (_, hp50, hp95, _) = hist_delta(
        &q(before, "writes", "hold_us"),
        &q(after, "writes", "hold_us"),
    );
    let (_, cp50, cp95, _) = hist_delta(
        &q(before, "writes", "commit_us"),
        &q(after, "writes", "commit_us"),
    );
    let (_, qp50, qp95, qmax) = hist_delta(
        &q(before, "writes", "queue_on_arrival"),
        &q(after, "writes", "queue_on_arrival"),
    );
    let (reads, _, rp95, _) = hist_delta(
        &q(before, "reads", "wait_us"),
        &q(after, "reads", "wait_us"),
    );
    let (_, rh50, rh95, _) = hist_delta(
        &q(before, "reads", "hold_us"),
        &q(after, "reads", "hold_us"),
    );
    QueueDelta {
        writes,
        write_wait_p50_us: wp50,
        write_wait_p95_us: wp95,
        write_wait_max_us: wmax,
        write_hold_p50_us: hp50,
        write_hold_p95_us: hp95,
        commit_p50_us: cp50,
        commit_p95_us: cp95,
        queue_on_arrival_p50: qp50,
        queue_on_arrival_p95: qp95,
        queue_on_arrival_max: qmax,
        reads,
        read_wait_p95_us: rp95,
        read_hold_p50_us: rh50,
        read_hold_p95_us: rh95,
    }
}

#[derive(Serialize, Clone, Debug)]
struct Scenario {
    op: String,
    concurrency: usize,
    ops: usize,
    secs: f64,
    ops_per_sec: f64,
    /// Per timed name: the operation and its requests.
    latency: BTreeMap<String, Summary>,
    queues: QueueDelta,
    peak_rss_kib: u64,
    cpu_secs: f64,
    first_error: Option<String>,
}

async fn scenario(
    ctx: &Ctx,
    svc_pid: u32,
    sampler: &Sampler,
    op: &str,
    concurrency: usize,
    total: usize,
) -> Result<Scenario> {
    let before = ctx
        .api
        .get("/admin/diagnostics", &ctx.fx.owner.token)
        .await?
        .body;
    let cpu0 = ps_sample(svc_pid).map(|s| s.1).unwrap_or(0.0);
    let started = Instant::now();
    let next = Arc::new(AtomicUsize::new(0));
    let results: Arc<Mutex<BTreeMap<String, Vec<f64>>>> = Arc::new(Mutex::new(BTreeMap::new()));
    let errors = Arc::new(AtomicUsize::new(0));
    let first_error: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let mut tasks = Vec::new();
    for w in 0..concurrency {
        let (ctx, next, results, errors, first_error, op) = (
            ctx.clone(),
            next.clone(),
            results.clone(),
            errors.clone(),
            first_error.clone(),
            op.to_string(),
        );
        tasks.push(tokio::spawn(async move {
            let mut rng = Rng::new(0x5eed_0000 + w as u64 * 7919 + concurrency as u64);
            while next.fetch_add(1, Ordering::Relaxed) < total {
                match ctx.op(&op, w, &mut rng).await {
                    Ok(t) => {
                        let mut r = results.lock().unwrap();
                        for (name, v) in t {
                            r.entry(name.to_string()).or_default().push(v);
                        }
                    }
                    Err(e) => {
                        errors.fetch_add(1, Ordering::Relaxed);
                        first_error.lock().unwrap().get_or_insert(format!("{e:#}"));
                    }
                }
            }
        }));
    }
    for t in tasks {
        t.await?;
    }
    let secs = started.elapsed().as_secs_f64();
    let cpu1 = ps_sample(svc_pid).map(|s| s.1).unwrap_or(0.0);
    let after = ctx
        .api
        .get("/admin/diagnostics", &ctx.fx.owner.token)
        .await?
        .body;
    let errors = errors.load(Ordering::Relaxed);
    let latency: BTreeMap<String, Summary> = results
        .lock()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), summarize(v, if k == op { errors } else { 0 })))
        .collect();
    let ops = latency.get(op).map(|s| s.count).unwrap_or(0);
    let s = Scenario {
        op: op.to_string(),
        concurrency,
        ops,
        secs,
        ops_per_sec: ops as f64 / secs.max(1e-9),
        latency,
        queues: queue_delta(&before, &after),
        peak_rss_kib: sampler.max_since(started),
        cpu_secs: cpu1 - cpu0,
        first_error: first_error.lock().unwrap().clone(),
    };
    let main = s.latency.get(op).cloned().unwrap_or_default();
    eprintln!(
        "{:<20} c={:<3} n={:<4} {:>7.1} op/s  p50 {:>8.1} ms  p95 {:>8.1} ms  max {:>8.1} ms  err {}  wwait p95 {} us  q p95 {}  rss {:.0} MiB",
        op,
        concurrency,
        ops,
        s.ops_per_sec,
        main.p50_ms,
        main.p95_ms,
        main.max_ms,
        errors,
        s.queues.write_wait_p95_us,
        s.queues.queue_on_arrival_p95,
        s.peak_rss_kib as f64 / 1024.0
    );
    if let Some(e) = &s.first_error {
        eprintln!(
            "    first error: {}",
            e.chars().take(300).collect::<String>()
        );
    }
    Ok(s)
}

#[derive(Serialize)]
struct RunReport {
    label: String,
    load_average_start: String,
    load_average_end: String,
    cache_mib: usize,
    counts: BTreeMap<String, u64>,
    startup_ms: f64,
    first_health_ms: f64,
    first_cards_ms: f64,
    idle_rss_kib: u64,
    idle_rss_after_load_kib: u64,
    exit: ExitReport,
    redb_bytes_before: u64,
    redb_bytes_after: u64,
    repos_bytes: u64,
    ci_bytes: u64,
    data_bytes_after: u64,
    scenarios: Vec<Scenario>,
}

async fn run(a: RunArgs) -> Result<()> {
    let work = &a.common.work;
    let fx: Fixture = serde_json::from_slice(&std::fs::read(work.join("fixture.json"))?)?;
    let bin = std::fs::canonicalize(&a.common.bin)?;
    let label = a
        .label
        .clone()
        .unwrap_or_else(|| format!("cache{}", a.cache_mib));
    // Every run starts from the same seeded state.
    let data = copy_seeded(work, &format!("run-{label}"))?;
    write_config(&data, fx.port, a.cache_mib)?;
    let redb_before = std::fs::metadata(data.join("branvil.redb"))?.len();
    let load_start = load_average();
    let log = work.join(format!("run-{label}.log"));
    let _ = std::fs::remove_file(&log);
    let svc = Service::start(&bin, &data, &log)?;
    let api = Api::new(&svc.url);
    let t = Instant::now();
    let h = api.get("/health", "").await?;
    let first_health = ms(t.elapsed());
    Ctx::ok(&h, "health")?;
    let first_cards = api
        .get(
            &format!(
                "/workspaces/{}/board/cards?lane=ready,in_progress&assignee=me&limit=100",
                fx.workspaces[0]
            ),
            &fx.agents[0].token,
        )
        .await?;
    Ctx::ok(&first_cards, "first cards")?;
    let sampler = Sampler::start(svc.pid);
    tokio::time::sleep(Duration::from_secs(3)).await;
    let idle_rss = ps_sample(svc.pid).map(|s| s.0).unwrap_or(0);
    eprintln!(
        "[{label}] startup {:.0} ms, first health {:.1} ms, first cards {:.1} ms, idle RSS {:.1} MiB, load {}",
        ms(svc.startup),
        first_health,
        ms(first_cards.elapsed),
        idle_rss as f64 / 1024.0,
        load_start
    );
    let mut spares = fx.spares.clone();
    let mut rng = Rng::new(42);
    for i in (1..spares.len()).rev() {
        let j = rng.below(i + 1);
        spares.swap(i, j);
    }
    let ctx = Ctx {
        api: api.clone(),
        open_pulls: Arc::new(fx.pulls.iter().filter(|p| p.open).cloned().collect()),
        active_tickets: Arc::new(
            fx.tickets
                .iter()
                .filter(|t| t.lane != "done")
                .cloned()
                .collect(),
        ),
        spares: Arc::new(Mutex::new(spares)),
        base_url: svc.url.clone(),
        fx: Arc::new(fx.clone()),
    };
    let mut ops: Vec<&str> = OPS.to_vec();
    ops.push("ticket_move_done");
    ops.push("mixed");
    if !a.only.is_empty() {
        ops.retain(|o| a.only.iter().any(|x| x == o));
    }
    let mut scenarios = Vec::new();
    for op in ops {
        for &c in &a.concurrency {
            if op == "mixed" && c == 1 {
                continue;
            }
            scenarios.push(scenario(&ctx, svc.pid, &sampler, op, c, a.requests).await?);
        }
    }
    tokio::time::sleep(Duration::from_secs(3)).await;
    let idle_after = ps_sample(svc.pid).map(|s| s.0).unwrap_or(0);
    sampler.finish();
    let startup = svc.startup;
    let exit = svc.stop()?;
    let report = RunReport {
        label: label.clone(),
        load_average_start: load_start,
        load_average_end: load_average(),
        cache_mib: a.cache_mib,
        counts: fx.counts.clone(),
        startup_ms: ms(startup),
        first_health_ms: first_health,
        first_cards_ms: ms(first_cards.elapsed),
        idle_rss_kib: idle_rss,
        idle_rss_after_load_kib: idle_after,
        exit,
        redb_bytes_before: redb_before,
        redb_bytes_after: std::fs::metadata(data.join("branvil.redb"))?.len(),
        repos_bytes: du_bytes(&data.join("repos")),
        ci_bytes: du_bytes(&data.join("ci")),
        data_bytes_after: du_bytes(&data),
        scenarios,
    };
    std::fs::write(
        work.join(format!("results-{label}.json")),
        serde_json::to_vec_pretty(&report)?,
    )?;
    print_report(&report);
    Ok(())
}

fn print_report(r: &RunReport) {
    println!("\n## {} (cache {} MiB)\n", r.label, r.cache_mib);
    println!(
        "startup {:.0} ms; first /health {:.1} ms; first cards page {:.1} ms; idle RSS {:.1} MiB (after load {:.1} MiB); peak RSS {:.1} MiB (time -l); CPU user {:.1} s sys {:.1} s",
        r.startup_ms,
        r.first_health_ms,
        r.first_cards_ms,
        r.idle_rss_kib as f64 / 1024.0,
        r.idle_rss_after_load_kib as f64 / 1024.0,
        mib(r.exit.peak_rss_bytes),
        r.exit.user_secs,
        r.exit.sys_secs
    );
    println!(
        "redb {:.1} → {:.1} MiB; repos {:.1} MiB; ci {:.1} MiB; data dir {:.1} MiB; load {} → {}\n",
        mib(r.redb_bytes_before),
        mib(r.redb_bytes_after),
        mib(r.repos_bytes),
        mib(r.ci_bytes),
        mib(r.data_bytes_after),
        r.load_average_start,
        r.load_average_end
    );
    println!(
        "| Operation | Conc. | Ops | Ops/s | p50 ms | p95 ms | max ms | Errors | Write wait p50/p95 ms | Queue on arrival p50/p95/max | Commit p50 ms | Peak RSS MiB | CPU s |"
    );
    println!(
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: |"
    );
    for s in &r.scenarios {
        let m = s.latency.get(&s.op).cloned().unwrap_or_default();
        let q = &s.queues;
        println!(
            "| {} | {} | {} | {:.1} | {:.1} | {:.1} | {:.1} | {} | {} | {} | {} | {:.0} | {:.1} |",
            s.op,
            s.concurrency,
            s.ops,
            s.ops_per_sec,
            m.p50_ms,
            m.p95_ms,
            m.max_ms,
            m.errors,
            if q.writes > 0 {
                format!(
                    "{:.2} / {:.2}",
                    q.write_wait_p50_us as f64 / 1000.0,
                    q.write_wait_p95_us as f64 / 1000.0
                )
            } else {
                "—".into()
            },
            if q.writes > 0 {
                format!(
                    "{} / {} / {}",
                    q.queue_on_arrival_p50, q.queue_on_arrival_p95, q.queue_on_arrival_max
                )
            } else {
                "—".into()
            },
            if q.writes > 0 {
                format!("{:.2}", q.commit_p50_us as f64 / 1000.0)
            } else {
                "—".into()
            },
            s.peak_rss_kib as f64 / 1024.0,
            s.cpu_secs
        );
    }
    println!("\nRequests within operations (p50 / p95 ms):\n");
    for s in &r.scenarios {
        let parts: Vec<String> = s
            .latency
            .iter()
            .filter(|(k, _)| **k != s.op && k.contains('.'))
            .map(|(k, v)| format!("{k} {:.1} / {:.1} (n={})", v.p50_ms, v.p95_ms, v.count))
            .collect();
        if !parts.is_empty() {
            println!("- {} c={}: {}", s.op, s.concurrency, parts.join("; "));
        }
    }
}

// ---------------------------------------------------------------- Git transfers

#[derive(Serialize)]
struct GitReport {
    load_average: String,
    repos: Vec<GitRepoReport>,
}

#[derive(Serialize)]
struct GitRepoReport {
    name: String,
    commits: usize,
    files: usize,
    refs: usize,
    pack_bytes: u64,
    /// operation → milliseconds of each repetition
    timings: BTreeMap<String, Vec<f64>>,
    peak_service_rss_kib: u64,
}

fn medium_stream(rng: &mut Rng, commits: usize, files: usize) -> Vec<u8> {
    let mut s = Stream(Vec::new());
    let t0 = 1_760_000_000u64;
    let mut contents: Vec<String> = (0..files).map(|_| source_lines(rng, 20, 160)).collect();
    s.commit("main", 1, t0, "Initial import\n", None);
    for (f, c) in contents.iter().enumerate() {
        s.file(&format!("src/d{}/f{f}.txt", f % 50), c.as_bytes());
    }
    for c in 2..=commits {
        s.commit(
            "main",
            c,
            t0 + c as u64 * 60,
            &format!("Change {c}\n"),
            Some(c - 1),
        );
        for _ in 0..(1 + rng.below(6)) {
            let f = rng.below(files);
            // Edit a few lines in place, as code changes do.
            let mut lines: Vec<String> = contents[f].lines().map(str::to_string).collect();
            for _ in 0..(1 + rng.below(4)) {
                let at = rng.below(lines.len());
                let n = 3 + rng.below(9);
                lines[at] = words(rng, n);
            }
            if rng.chance(0.3) {
                let n = 3 + rng.below(9);
                lines.push(words(rng, n));
            }
            contents[f] = lines.join("\n") + "\n";
            s.file(&format!("src/d{}/f{f}.txt", f % 50), contents[f].as_bytes());
        }
    }
    s.0
}

async fn git_bench(a: GitArgs) -> Result<()> {
    let work = &a.common.work;
    let fx: Fixture = serde_json::from_slice(&std::fs::read(work.join("fixture.json"))?)?;
    let bin = std::fs::canonicalize(&a.common.bin)?;
    let data = copy_seeded(work, "run-git")?;
    write_config(&data, fx.port, 32)?;
    let log = work.join("run-git.log");
    let _ = std::fs::remove_file(&log);
    let svc = Service::start(&bin, &data, &log)?;
    let api = Api::new(&svc.url);
    let sampler = Sampler::start(svc.pid);
    let home = work.join("git-home");
    let owner = fx.owner.clone();
    let agent = fx.agents[0].clone();
    let mut rng = Rng::new(0x617);
    let src = work.join("git-src");
    let _ = std::fs::remove_dir_all(&src);
    let mut reports = Vec::new();
    // small: 20 files, 10 commits; medium: generated; fixture: app-0 with its branches.
    let small = medium_stream(&mut rng, 10, 20);
    eprintln!(
        "generating the medium repository ({} commits, {} files)",
        a.medium_commits, a.medium_files
    );
    let medium = medium_stream(&mut rng, a.medium_commits, a.medium_files);
    for (name, stream, commits, files) in [
        ("small", Some(small), 10, 20),
        ("medium", Some(medium), a.medium_commits, a.medium_files),
        ("fixture app-0", None, 0, 0),
    ] {
        let mut timings: BTreeMap<String, Vec<f64>> = BTreeMap::new();
        let started_all = Instant::now();
        let repo_path = match &stream {
            Some(_) => format!("{}/git-{name}", fx.workspaces[0]),
            None => fx.repos[0].clone(),
        };
        let url = format!("{}/git/{repo_path}.git", svc.url);
        if let Some(stream) = &stream {
            let local = src.join(name);
            fast_import(&home, &local, stream)?;
            git(&home, &local, &["repack", "-adq"], None)?;
            api.must(
                reqwest::Method::POST,
                "/repos",
                &owner.token,
                Some(json!({"path": repo_path})),
            )
            .await?;
            let t = Instant::now();
            git(
                &home,
                &local,
                &["push", "-q", &url, "main"],
                Some(("alice", &owner.token)),
            )?;
            timings
                .entry("initial push".into())
                .or_default()
                .push(ms(t.elapsed()));
        }
        let mut refs = 0;
        for i in 0..a.repeat {
            let dir = src.join(format!("{}-clone-{i}", name.replace(' ', "-")));
            let _ = std::fs::remove_dir_all(&dir);
            let t = Instant::now();
            git(
                &home,
                &src,
                &["clone", "-q", &url, dir.to_str().unwrap()],
                Some((&agent.name, &agent.token)),
            )?;
            timings
                .entry("clone".into())
                .or_default()
                .push(ms(t.elapsed()));
            refs = git(&home, &dir, &["for-each-ref", "refs/remotes"], None)?
                .lines()
                .count();
            let t = Instant::now();
            git(
                &home,
                &dir,
                &["fetch", "-q", "origin"],
                Some((&agent.name, &agent.token)),
            )?;
            timings
                .entry("fetch (no change)".into())
                .or_default()
                .push(ms(t.elapsed()));
            // One commit on a new branch: push, then fetch it from another clone.
            let branch = format!("bench/{i}");
            git(&home, &dir, &["checkout", "-q", "-b", &branch], None)?;
            std::fs::write(dir.join(format!("bench-{i}.txt")), words(&mut rng, 200))?;
            git(&home, &dir, &["add", "-A"], None)?;
            git(&home, &dir, &["commit", "-qm", "bench change"], None)?;
            let t = Instant::now();
            git(
                &home,
                &dir,
                &["push", "-q", "origin", &branch],
                Some((&agent.name, &agent.token)),
            )?;
            timings
                .entry("push (1 commit, new branch)".into())
                .or_default()
                .push(ms(t.elapsed()));
            std::fs::write(dir.join(format!("bench-{i}.txt")), words(&mut rng, 200))?;
            git(&home, &dir, &["commit", "-qam", "bench change 2"], None)?;
            let t = Instant::now();
            git(
                &home,
                &dir,
                &["push", "-q", "origin", &branch],
                Some((&agent.name, &agent.token)),
            )?;
            timings
                .entry("push (1 commit, existing branch)".into())
                .or_default()
                .push(ms(t.elapsed()));
            if i > 0 {
                let other = src.join(format!("{}-clone-0", name.replace(' ', "-")));
                let t = Instant::now();
                git(
                    &home,
                    &other,
                    &["fetch", "-q", "origin"],
                    Some((&agent.name, &agent.token)),
                )?;
                timings
                    .entry("fetch (1 new branch)".into())
                    .or_default()
                    .push(ms(t.elapsed()));
            }
        }
        let pack = du_bytes(&data.join("repos").join(format!("{repo_path}.git")));
        eprintln!("git {name}: {timings:?}");
        reports.push(GitRepoReport {
            name: name.to_string(),
            commits,
            files,
            refs,
            pack_bytes: pack,
            timings,
            peak_service_rss_kib: sampler.max_since(started_all),
        });
    }
    sampler.finish();
    let exit = svc.stop()?;
    let report = GitReport {
        load_average: load_average(),
        repos: reports,
    };
    std::fs::write(
        work.join("results-git.json"),
        serde_json::to_vec_pretty(&report)?,
    )?;
    println!(
        "\n## Git over Smart HTTP (peak service RSS {:.1} MiB)\n",
        mib(exit.peak_rss_bytes)
    );
    println!(
        "| Repository | Commits | Files | Refs | Size on Branvil MiB | Operation | ms (each run) |"
    );
    println!("| --- | ---: | ---: | ---: | ---: | --- | --- |");
    for r in &report.repos {
        for (op, v) in &r.timings {
            let list: Vec<String> = v.iter().map(|x| format!("{x:.0}")).collect();
            println!(
                "| {} | {} | {} | {} | {:.1} | {} | {} |",
                r.name,
                r.commits,
                r.files,
                r.refs,
                mib(r.pack_bytes),
                op,
                list.join(", ")
            );
        }
    }
    Ok(())
}

/// Copies the seeded data directory (an APFS clone) to `<work>/<name>`.
fn copy_seeded(work: &Path, name: &str) -> Result<PathBuf> {
    let data = work.join(name);
    if data.exists() {
        std::fs::remove_dir_all(&data)?;
    }
    let status = Command::new("cp")
        .arg("-cR")
        .arg(seeded_dir(work))
        .arg(&data)
        .status()?;
    if !status.success() {
        bail!("copying the seeded data failed");
    }
    Ok(data)
}

async fn startup(a: StartupArgs) -> Result<()> {
    let work = &a.common.work;
    let fx: Fixture = serde_json::from_slice(&std::fs::read(work.join("fixture.json"))?)?;
    let bin = std::fs::canonicalize(&a.common.bin)?;
    let data = copy_seeded(work, "run-startup")?;
    write_config(&data, fx.port, a.cache_mib)?;
    let log = work.join("run-startup.log");
    let _ = std::fs::remove_file(&log);
    let mut rows = Vec::new();
    for i in 0..a.repeat {
        let svc = Service::start(&bin, &data, &log)?;
        let api = Api::new(&svc.url);
        let t = Instant::now();
        Ctx::ok(&api.get("/health", "").await?, "health")?;
        let health = ms(t.elapsed());
        let cards = api
            .get(
                &format!(
                    "/workspaces/{}/board/cards?lane=ready,in_progress&assignee=me&limit=100",
                    fx.workspaces[0]
                ),
                &fx.agents[0].token,
            )
            .await?;
        Ctx::ok(&cards, "cards")?;
        tokio::time::sleep(Duration::from_secs(2)).await;
        let rss = ps_sample(svc.pid).map(|s| s.0).unwrap_or(0);
        let startup = ms(svc.startup);
        let exit = svc.stop()?;
        eprintln!(
            "start {i}: listening after {startup:.0} ms, first health {health:.1} ms, first cards {:.1} ms, idle RSS {:.1} MiB, peak {:.1} MiB",
            ms(cards.elapsed),
            rss as f64 / 1024.0,
            mib(exit.peak_rss_bytes)
        );
        rows.push(json!({"startup_ms": startup, "first_health_ms": health,
                         "first_cards_ms": ms(cards.elapsed), "idle_rss_kib": rss,
                         "peak_rss_bytes": exit.peak_rss_bytes}));
    }
    std::fs::write(
        work.join(format!("results-startup-cache{}.json", a.cache_mib)),
        serde_json::to_vec_pretty(&json!({"load_average": load_average(), "runs": rows}))?,
    )?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    match Cli::parse().command {
        Cmd::Seed(a) => seed(a).await,
        Cmd::Run(a) => run(a).await,
        Cmd::Git(a) => git_bench(a).await,
        Cmd::Startup(a) => startup(a).await,
    }
}
