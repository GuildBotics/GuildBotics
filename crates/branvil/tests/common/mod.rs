//! Test harness: an embedded Branvil server on an ephemeral loopback port, real
//! redb data directories, and real Git clients over Smart HTTP.

#![allow(dead_code)]

pub mod github;
mod process;

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

pub use process::ServiceProcess;

use reqwest::{Method, StatusCode};
use serde_json::{Value, json};

/// Run this test executable as the server, its hooks, and its runners.
#[ctor::ctor]
fn process_entry() {
    if let Some(code) = branvil::askpass_main() {
        std::process::exit(if code == std::process::ExitCode::SUCCESS {
            0
        } else {
            1
        });
    }
    if std::env::var_os("BRANVIL_TEST_PROCESS").is_some() {
        let args: Vec<String> = std::env::args().skip(1).collect();
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .unwrap();
        let code = runtime.block_on(process(args));
        std::process::exit(code);
    }
}

/// What the server runs as itself: `serve --listen <addr> --data-dir <dir>`
/// (this harness's own), `server git-hook <name>`, and `runner ...`.
async fn process(args: Vec<String>) -> i32 {
    let _ = tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_env("BRANVIL_LOG")
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("branvil=info")),
        )
        .with_writer(std::io::stderr)
        .try_init();
    let words: Vec<&str> = args.iter().map(String::as_str).collect();
    let result = match words.as_slice() {
        ["serve", "--listen", listen, "--data-dir", data] => {
            let vault: std::sync::Arc<dyn branvil::vault::Vault> =
                if std::env::var_os("BRANVIL_TEST_NO_VAULT").is_some() {
                    std::sync::Arc::new(NoVault)
                } else {
                    match std::env::var_os("BRANVIL_TEST_VAULT") {
                        Some(dir) => {
                            std::sync::Arc::new(branvil::test_support::FileVault(dir.into()))
                        }
                        None => std::sync::Arc::new(branvil::vault::Memory::default()),
                    }
                };
            let (interrupt, terminate) = stop_signals();
            branvil::test_support::serve(
                Path::new(data),
                listen,
                vault,
                interrupt,
                terminate,
                true,
                None,
            )
            .await
        }
        ["server", "git-hook", name] => Ok(branvil::git_hook(name).await),
        // The code host's part of `guildbotics server hub-data`.
        ["admin", what, "--data-dir", data] => {
            let operation = match *what {
                "check" => branvil::Maintenance::Check,
                "reindex" => branvil::Maintenance::Reindex,
                "upgrade" => branvil::Maintenance::Upgrade,
                other => panic!("unsupported maintenance: {other}"),
            };
            branvil::maintain(operation, Path::new(data)).map(|()| 0)
        }
        ["backup", "--to", to] => {
            let env = |name| std::env::var(name).unwrap_or_default();
            branvil::request_backup(&env("BRANVIL_URL"), &env("BRANVIL_TOKEN"), Path::new(to))
                .await
                .map(|()| 0)
        }
        ["restore", "--from", from, "--data-dir", data] => {
            branvil::restore(Path::new(from), Path::new(data)).await
        }
        ["runner", ..] => {
            use clap::Parser;
            #[derive(Parser)]
            struct Runner {
                #[command(flatten)]
                args: branvil::RunnerArgs,
            }
            let parsed = Runner::try_parse_from(&args).unwrap_or_else(|e| e.exit());
            let data = std::env::var_os("BRANVIL_DATA_DIR").unwrap_or_default();
            let args = if let Some(vm_data) = std::env::var_os("BRANVIL_TEST_VM_DATA") {
                parsed.args.vm_home(vm_data.into())
            } else {
                parsed.args
            };
            branvil::run_runner(args, String::new(), data.into(), async {
                anyhow::bail!("the test runner has no owner token")
            })
            .await
        }
        other => panic!("unsupported test process: {other:?}"),
    };
    match result {
        Ok(code) => i32::from(code),
        Err(e) => {
            eprintln!("branvil: {e:#}");
            1
        }
    }
}

/// SIGINT and SIGTERM, taken over before the service starts any work.
fn stop_signals() -> (
    impl std::future::Future<Output = ()> + Send + 'static,
    impl std::future::Future<Output = ()> + Send + 'static,
) {
    use tokio::signal::unix::{SignalKind, signal};
    let mut interrupt = signal(SignalKind::interrupt()).unwrap();
    let mut terminate = signal(SignalKind::terminate()).unwrap();
    (
        async move {
            interrupt.recv().await;
        },
        async move {
            terminate.recv().await;
        },
    )
}

/// A host that cannot provide a vault must refuse secret operations.
struct NoVault;
impl branvil::vault::Vault for NoVault {
    fn get(&self, _: &str) -> anyhow::Result<Option<String>> {
        anyhow::bail!("test host has no vault")
    }
    fn set(&self, _: &str, _: &str) -> anyhow::Result<()> {
        anyhow::bail!("test host has no vault: secrets cannot be kept")
    }
    fn delete(&self, _: &str) -> anyhow::Result<()> {
        anyhow::bail!("test host has no vault")
    }
}

pub fn branvil_command() -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command.env("BRANVIL_TEST_PROCESS", "1");
    command
}

pub fn async_branvil_command() -> tokio::process::Command {
    branvil_command().into()
}

pub struct Server {
    child: Option<ServiceProcess>,
    /// The service's standard output, line by line.
    lines: std::sync::mpsc::Receiver<String>,
    pub url: String,
    pub data: PathBuf,
    /// The directory of the test-provided sealed vault,
    /// outside its data directory.
    pub vault: PathBuf,
    pub http: reqwest::Client,
}

/// Initialize `dir/data` with `owner` as its owner and return the owner's
/// credential, the host's owner token.
pub fn init(dir: &Path, owner: &str) -> String {
    branvil::test_support::initialize(&dir.join("data"), owner).unwrap();
    branvil::test_support::OWNER_TOKEN.into()
}

impl Server {
    pub fn start(dir: &Path) -> Server {
        Self::start_with(dir, &[])
    }

    /// Starts the service on a free port with extra environment variables.
    pub fn start_with(dir: &Path, env: &[(&str, &str)]) -> Server {
        Self::start_on(dir, "127.0.0.1:0", env)
    }

    /// Starts the service on `listen` (for a restart on the same base URL).
    pub fn start_on(dir: &Path, listen: &str, env: &[(&str, &str)]) -> Server {
        let data = dir.join("data");
        let log = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join("serve.log"))
            .unwrap();
        let vault = dir.join("vault");
        let mut cmd = branvil_command();
        cmd.args(["serve", "--listen", listen, "--data-dir"])
            .arg(&data)
            .env("BRANVIL_TEST_VAULT", &vault)
            .env("BRANVIL_LOG", "branvil=debug")
            .env_remove("BRANVIL_FAILPOINT")
            .stdout(Stdio::piped())
            .stderr(log);
        for (k, v) in env {
            cmd.env(k, v);
        }
        let mut child = ServiceProcess::spawn(&mut cmd).expect("spawn the test server");
        let stdout = child.take_stdout();
        let (sender, lines) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if sender.send(line).is_err() {
                    break;
                }
            }
        });
        let line = lines.recv().unwrap_or_default();
        let url = line
            .strip_prefix("branvil listening on ")
            .and_then(|l| l.split_whitespace().next())
            .unwrap_or_else(|| {
                panic!(
                    "unexpected first line {line:?}; see {}",
                    dir.join("serve.log").display()
                )
            })
            .to_string();
        Server {
            child: Some(child),
            lines,
            url,
            data,
            vault,
            http: reqwest::Client::new(),
        }
    }

    /// The service's vault, and the name `name` gives in it.
    fn in_vault(
        &self,
        name: impl FnOnce(&branvil::vault::Secrets) -> String,
    ) -> (branvil::test_support::FileVault, String) {
        use branvil::vault::{Memory, Secrets};
        let names = Secrets::new(std::sync::Arc::new(Memory::default()), &self.data);
        (
            branvil::test_support::FileVault(self.vault.clone()),
            name(&names),
        )
    }

    /// What the service keeps in its vault under the name `name` gives.
    pub fn kept(&self, name: impl FnOnce(&branvil::vault::Secrets) -> String) -> Option<String> {
        use branvil::vault::Vault;
        let (vault, name) = self.in_vault(name);
        vault.get(&name).unwrap()
    }

    /// Removes that from the vault, as a restore elsewhere loses it.
    pub fn forget(&self, name: impl FnOnce(&branvil::vault::Secrets) -> String) {
        use branvil::vault::Vault;
        let (vault, name) = self.in_vault(name);
        vault.delete(&name).unwrap();
    }

    /// The vault's file of the name `name` gives.
    pub fn vault_file(&self, name: impl FnOnce(&branvil::vault::Secrets) -> String) -> PathBuf {
        self.vault.join(self.in_vault(name).1)
    }

    /// Whether a file of the data directory holds `value` in plain text.
    pub fn data_shows(&self, value: &str) -> bool {
        fn shows(dir: &Path, value: &str) -> bool {
            std::fs::read_dir(dir).into_iter().flatten().any(|e| {
                let path = e.unwrap().path();
                if path.is_dir() {
                    shows(&path, value)
                } else {
                    String::from_utf8_lossy(&std::fs::read(&path).unwrap_or_default())
                        .contains(value)
                }
            })
        }
        shows(&self.data, value)
    }

    /// Stops the service as Ctrl-C does (no wait for CI jobs; the repository
    /// operations already started finish) and drains its children, before
    /// offline access or a restart. Crashes are failpoints' (`wait_exit`).
    /// Without SIGINT (Windows) the service is killed.
    pub fn stop(&mut self) {
        #[cfg(unix)]
        if self.child.is_some() {
            self.signal("INT");
            let limit = std::time::Duration::from_secs(10);
            let status = self.exit_within(limit);
            assert!(
                status.is_some_and(|s| s.success()),
                "the service did not stop cleanly within {limit:?} of SIGINT: {status:?}; see {}",
                self.data.with_file_name("serve.log").display()
            );
        }
        if let Some(mut c) = self.child.take() {
            c.stop().expect("stop service process group");
        }
    }

    /// Sends SIGTERM and waits up to `limit` for the process to exit.
    /// Returns its status and how long it took, or `None` (after killing it)
    /// when it was still running at `limit`.
    #[cfg(unix)]
    pub fn terminate(
        &mut self,
        limit: std::time::Duration,
    ) -> Option<(std::process::ExitStatus, std::time::Duration)> {
        let sent = std::time::Instant::now();
        self.signal("TERM");
        self.exit_within(limit)
            .map(|status| (status, sent.elapsed()))
    }

    /// The next line of the service's standard output, if one comes within
    /// `limit`.
    pub fn line_within(&self, limit: std::time::Duration) -> Option<String> {
        self.lines.recv_timeout(limit).ok()
    }

    /// Sends SIGINT to the service's process group, as a terminal's Ctrl-C
    /// does, and returns at once.
    #[cfg(unix)]
    pub fn interrupt_group(&self) {
        let pid = self.child.as_ref().expect("running").id();
        // SAFETY: the service leads its own group (`ServiceProcess::spawn`).
        assert_eq!(unsafe { libc::killpg(pid as libc::pid_t, libc::SIGINT) }, 0);
    }

    /// Whether the service process is still running.
    pub fn running(&mut self) -> bool {
        self.child.as_mut().is_some_and(|c| !c.exited().unwrap())
    }

    /// Sends `signal` (`TERM`, `INT`) and returns at once.
    #[cfg(unix)]
    pub fn signal(&self, signal: &str) {
        let pid = self.child.as_ref().expect("running").id();
        let kill = Command::new("kill")
            .args([format!("-{signal}"), pid.to_string()])
            .status()
            .unwrap();
        assert!(kill.success());
    }

    /// Waits up to `limit` for the process to exit; `None` (after killing
    /// it) when it was still running at `limit`.
    pub fn exit_within(&mut self, limit: std::time::Duration) -> Option<std::process::ExitStatus> {
        let mut c = self.child.take().expect("running");
        c.exit_within(limit)
            .expect("wait for service process group")
    }

    /// Waits for the process to exit by itself (after a failpoint).
    pub fn wait_exit(&mut self) -> std::process::ExitStatus {
        let mut c = self.child.take().expect("running");
        c.wait().unwrap()
    }

    pub async fn call(
        &self,
        method: Method,
        path: &str,
        token: &str,
        body: Option<Value>,
    ) -> (StatusCode, Value) {
        let mut req = self
            .http
            .request(method, format!("{}/api/v1{path}", self.url))
            .bearer_auth(token);
        if let Some(b) = body {
            req = req.json(&b);
        }
        let resp = req.send().await.expect("request");
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        (
            status,
            serde_json::from_str(&text).unwrap_or(Value::String(text)),
        )
    }

    pub async fn get(&self, path: &str, token: &str) -> (StatusCode, Value) {
        self.call(Method::GET, path, token, None).await
    }
    pub async fn post(&self, path: &str, token: &str, body: Value) -> (StatusCode, Value) {
        self.call(Method::POST, path, token, Some(body)).await
    }
    pub async fn put(&self, path: &str, token: &str, body: Value) -> (StatusCode, Value) {
        self.call(Method::PUT, path, token, Some(body)).await
    }
    pub async fn delete(&self, path: &str, token: &str) -> (StatusCode, Value) {
        self.call(Method::DELETE, path, token, None).await
    }
    pub async fn delete_with(&self, path: &str, token: &str, body: Value) -> (StatusCode, Value) {
        self.call(Method::DELETE, path, token, Some(body)).await
    }
    pub async fn patch(&self, path: &str, token: &str, body: Value) -> (StatusCode, Value) {
        self.call(Method::PATCH, path, token, Some(body)).await
    }

    /// Like `get` but asserts 200 and returns the body.
    pub async fn ok(&self, path: &str, token: &str) -> Value {
        let (s, v) = self.get(path, token).await;
        assert_eq!(s, StatusCode::OK, "GET {path}: {v}");
        v
    }

    /// A new token of the member `name` (the owner's for Git, which takes
    /// only members' own tokens), issued by the host's owner.
    pub async fn git_token(&self, name: &str) -> String {
        let (st, v) = self
            .post(
                &format!("/members/{name}/tokens"),
                branvil::test_support::OWNER_TOKEN,
                json!({"label": "git"}),
            )
            .await;
        assert_eq!(st, StatusCode::CREATED, "{v}");
        v["token"].as_str().unwrap().to_string()
    }

    /// Creates a member of `kind` (`agent` or `runner`) and returns a token
    /// of it.
    pub async fn member(&self, owner: &str, name: &str, kind: &str) -> String {
        let (st, v) = self
            .post("/members", owner, json!({"name": name, "kind": kind}))
            .await;
        assert_eq!(st, StatusCode::CREATED, "{v}");
        let (st, v) = self
            .post(
                &format!("/members/{name}/tokens"),
                owner,
                json!({"label": "test"}),
            )
            .await;
        assert_eq!(st, StatusCode::CREATED, "{v}");
        v["token"].as_str().unwrap().to_string()
    }

    /// The host's browser session of the owner: the cookie pair and the CSRF
    /// value.
    pub fn session(&self) -> (String, String) {
        (
            branvil::test_support::SESSION_COOKIE.into(),
            branvil::test_support::SESSION_CSRF.into(),
        )
    }

    /// The clone URL (credentials come from `git_as`'s askpass, never the URL).
    pub fn remote(&self, repo: &str) -> String {
        format!("{}/git/{repo}.git", self.url)
    }

    /// Runs this harness's `backup` or `admin <what>` against this server's
    /// data, as `guildbotics server hub-data` does for the code host.
    pub fn cli(&self, token: &str, args: &[&str]) -> Output {
        let mut command = branvil_command();
        command.args(args);
        if args.first() == Some(&"admin") {
            command.arg("--data-dir").arg(&self.data);
        }
        command
            .env("BRANVIL_URL", &self.url)
            .env("BRANVIL_TOKEN", token)
            .output()
            .unwrap()
    }
}

/// Runs a client-side Git command isolated from the user's configuration and
/// credential helpers.
pub fn git(dir: &Path, args: &[&str]) -> Output {
    git_cmd(dir, args, None)
}

/// Like `git`, authenticating as `member` through an askpass script that
/// reads the token from the environment (never the URL or argv).
pub fn git_as(dir: &Path, member: &str, token: &str, args: &[&str]) -> Output {
    git_cmd(dir, args, Some((member, token)))
}

fn git_cmd(dir: &Path, args: &[&str], auth: Option<(&str, &str)>) -> Output {
    let home = dir.parent().unwrap_or(dir).join("git-home");
    std::fs::create_dir_all(&home).unwrap();
    let global = home.join("gitconfig");
    if !global.exists() {
        std::fs::write(&global, "").unwrap();
    }
    let askpass = home.join("askpass.sh");
    if !askpass.exists() {
        std::fs::write(
            &askpass,
            "#!/bin/sh\ncase \"$1\" in\n  Username*) printf '%s\\n' \"$BRANVIL_TEST_USER\" ;;\n  *) printf '%s\\n' \"$BRANVIL_TEST_TOKEN\" ;;\nesac\n",
        )
        .unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&askpass, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
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
        .env("HOME", &home)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", &global)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .stdin(Stdio::null());
    match auth {
        Some((member, token)) => {
            cmd.env("GIT_ASKPASS", &askpass)
                .env("BRANVIL_TEST_USER", member)
                .env("BRANVIL_TEST_TOKEN", token);
        }
        // Without credentials, a prompt must fail instead of asking anyone.
        None => {
            cmd.env("GIT_ASKPASS", "/usr/bin/false");
        }
    }
    cmd.output().unwrap()
}

pub fn git_ok(dir: &Path, args: &[&str]) -> String {
    check(git(dir, args), args)
}

pub fn git_as_ok(dir: &Path, member: &str, token: &str, args: &[&str]) -> String {
    check(git_as(dir, member, token, args), args)
}

fn check(out: Output, args: &[&str]) -> String {
    assert!(
        out.status.success(),
        "git {args:?} failed: {}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8_lossy(&out.stdout).trim().to_string()
}

pub fn encode(path: &str) -> String {
    path.replace('/', "%2F")
}

/// A seeded service: the workspace `acme` with `acme/demo`, which has an
/// initial `main` and the labels `bug`, `feature`, `docs`, `ui`, and needs no
/// approval for a merge or a publish (the tests of approvals ask for them),
/// and members:
/// - `alice` the owner (from init)
/// - `ken` and `aiko`, agents
pub struct World {
    pub dir: tempfile::TempDir,
    pub server: Server,
    pub owner: String,
    /// A token of the owner, for Git.
    pub owner_git: String,
    pub ken: String,
    pub aiko: String,
    pub work: PathBuf,
}

pub const WS: &str = "acme";
pub const REPO: &str = "acme/demo";

impl World {
    pub async fn new() -> World {
        let dir = tempfile::tempdir().unwrap();
        let owner = init(dir.path(), "alice");
        let server = Server::start(dir.path());
        let s = &server;
        assert_eq!(
            s.post("/workspaces", &owner, json!({"name": WS})).await.0,
            StatusCode::CREATED
        );
        assert_eq!(
            s.post(
                "/repos",
                &owner,
                json!({"path": REPO, "default_branch": "main", "labels": ["bug", "feature", "docs", "ui"]})
            )
            .await
            .0,
            StatusCode::CREATED
        );
        let repo = format!("/repos/{}", encode(REPO));
        assert_eq!(
            s.patch(
                &repo,
                &owner,
                json!({"revision": 1, "required_approvals": 0})
            )
            .await
            .0,
            StatusCode::OK
        );
        let (st, v) = s
            .post("/members/alice/tokens", &owner, json!({"label": "git"}))
            .await;
        assert_eq!(st, StatusCode::CREATED, "{v}");
        let owner_git = v["token"].as_str().unwrap().to_string();
        let mut tokens = Vec::new();
        for name in ["ken", "aiko"] {
            tokens.push(s.member(&owner, name, "agent").await);
        }
        let work = dir.path().join("work");
        std::fs::create_dir_all(&work).unwrap();
        git_ok(&work, &["init", "-q", "-b", "main"]);
        std::fs::write(work.join("README.md"), "demo\n").unwrap();
        git_ok(&work, &["add", "README.md"]);
        git_ok(&work, &["commit", "-qm", "initial"]);
        git_ok(&work, &["remote", "add", "origin", &server.remote(REPO)]);
        git_as_ok(
            &work,
            "alice",
            &owner_git,
            &["push", "-q", "origin", "main"],
        );
        let mut t = tokens.into_iter();
        World {
            owner,
            owner_git,
            ken: t.next().unwrap(),
            aiko: t.next().unwrap(),
            server,
            dir,
            work,
        }
    }

    /// Commits a file change on `branch` (created from the current HEAD when
    /// missing) and pushes it as aiko. Returns the new tip.
    pub fn push_change(&self, branch: &str, file: &str, content: &str) -> String {
        let w = &self.work;
        if git(
            w,
            &[
                "rev-parse",
                "--verify",
                "--quiet",
                &format!("refs/heads/{branch}"),
            ],
        )
        .status
        .success()
        {
            git_ok(w, &["checkout", "-q", branch]);
        } else {
            git_ok(w, &["checkout", "-q", "-b", branch]);
        }
        std::fs::write(w.join(file), content).unwrap();
        git_ok(w, &["add", file]);
        git_ok(w, &["commit", "-qm", &format!("change {file}")]);
        git_as_ok(
            w,
            "aiko",
            &self.aiko,
            &[
                "push",
                "-q",
                "origin",
                &format!("+HEAD:refs/heads/{branch}"),
            ],
        );
        git_ok(w, &["rev-parse", "HEAD"])
    }

    /// Stops the service and starts it again (new port) with `env`; the
    /// working clone follows the new URL.
    pub fn restart(&mut self, env: &[(&str, &str)]) {
        self.server.stop();
        self.server = Server::start_with(self.dir.path(), env);
        git_ok(
            &self.work,
            &["remote", "set-url", "origin", &self.server.remote(REPO)],
        );
    }

    /// Backs the service up, restores the backup under `into`, and starts
    /// the restored service there (members' tokens stay valid).
    pub async fn restored(&self, into: &Path) -> Server {
        let backup = into.join("backup");
        let (st, v) = self
            .server
            .post("/admin/backup", &self.owner, json!({"to": backup}))
            .await;
        assert_eq!(st, StatusCode::OK, "{v}");
        assert_eq!(
            branvil::restore(&backup, &into.join("data")).await.unwrap(),
            0
        );
        Server::start(into)
    }

    pub async fn ticket(&self, title: &str) -> Value {
        let (st, v) = self
            .server
            .post(
                &format!("{}/tickets", self.repo_path()),
                &self.aiko,
                json!({"title": title, "body": "b"}),
            )
            .await;
        assert_eq!(st, StatusCode::CREATED, "{v}");
        v
    }

    /// aiko, the author of pull request `n`, answers what Branvil said on it
    /// (a failed gate or host check) with a reaction.
    pub async fn acknowledge(&self, n: u64) {
        let at = format!("{}/pulls/{n}", self.repo_path());
        let activity = self.server.ok(&format!("{at}/activity"), &self.aiko).await;
        for c in activity["comments"].as_array().unwrap() {
            if c["author"] == "branvil" {
                let (st, v) = self
                    .server
                    .post(
                        &format!("{at}/reactions"),
                        &self.aiko,
                        json!({"target_id": c["id"], "reaction": "ack"}),
                    )
                    .await;
                assert_eq!(st, StatusCode::OK, "{v}");
            }
        }
    }

    pub fn repo_path(&self) -> String {
        format!("/repos/{}", encode(REPO))
    }

    pub async fn open_pull(&self, head: &str, tickets: Value) -> Value {
        let (st, v) = self
            .server
            .post(
                &format!("{}/pulls", self.repo_path()),
                &self.aiko,
                json!({"head": head, "title": format!("PR {head}"), "body": "body", "tickets": tickets}),
            )
            .await;
        assert_eq!(st, StatusCode::CREATED, "{v}");
        v["pull"].clone()
    }
}

/// The JSON records of `table` in the database file `db` (the service
/// stopped).
pub fn records(db: &Path, table: &str) -> Vec<Value> {
    let def: redb::TableDefinition<&[u8], &[u8]> = redb::TableDefinition::new(table);
    let database = redb::Database::open(db).unwrap();
    let txn = redb::ReadableDatabase::begin_read(&database).unwrap();
    let t = txn.open_table(def).unwrap();
    redb::ReadableTable::iter(&t)
        .unwrap()
        .map(|item| serde_json::from_slice(item.unwrap().1.value()).unwrap())
        .collect()
}

/// Rewrites every JSON record of `table` in the database file `db` (the
/// service stopped) with `f`; with `table` `meta`, the schema version.
pub fn edit_records(db: &Path, table: &str, mut f: impl FnMut(&mut Value)) {
    let def: redb::TableDefinition<&[u8], &[u8]> = redb::TableDefinition::new(table);
    let database = redb::Database::open(db).unwrap();
    let txn = database.begin_write().unwrap();
    {
        let mut t = txn.open_table(def).unwrap();
        let all: Vec<(Vec<u8>, Vec<u8>)> = redb::ReadableTable::iter(&t)
            .unwrap()
            .map(|item| {
                let (k, v) = item.unwrap();
                (k.value().to_vec(), v.value().to_vec())
            })
            .collect();
        for (k, v) in all {
            let mut value: Value = serde_json::from_slice(&v).unwrap();
            f(&mut value);
            t.insert(k.as_slice(), serde_json::to_vec(&value).unwrap().as_slice())
                .unwrap();
        }
    }
    txn.commit().unwrap();
}

/// Recreates the immediately preceding schema in a stopped database.
pub fn as_schema_37(db: &Path) {
    edit_records(db, "meta", |v| {
        if v.is_u64() {
            *v = json!(37);
        }
    });
    edit_records(db, "ci_attempts", |v| {
        let attempt = v.as_object_mut().unwrap();
        attempt.remove("allocation");
        attempt.remove("peak_memory_mib");
    });
    let memory: redb::TableDefinition<&[u8], &[u8]> = redb::TableDefinition::new("ix_ci_memory");
    let database = redb::Database::open(db).unwrap();
    let txn = database.begin_write().unwrap();
    assert!(txn.delete_table(memory).unwrap());
    txn.commit().unwrap();
}

/// Every table of a database, by name, as raw keys and values.
pub type Tables = std::collections::BTreeMap<String, Vec<(Vec<u8>, Vec<u8>)>>;

/// Every table of the database file `db` (the service stopped).
pub fn tables(db: &Path) -> Tables {
    let database = redb::Database::open(db).unwrap();
    let txn = redb::ReadableDatabase::begin_read(&database).unwrap();
    let mut out = std::collections::BTreeMap::new();
    for handle in txn.list_tables().unwrap() {
        let name = redb::TableHandle::name(&handle).to_string();
        let def: redb::TableDefinition<&[u8], &[u8]> = redb::TableDefinition::new(&name);
        let t = txn.open_table(def).unwrap();
        let rows = redb::ReadableTable::iter(&t)
            .unwrap()
            .map(|item| {
                let (k, v) = item.unwrap();
                (k.value().to_vec(), v.value().to_vec())
            })
            .collect();
        out.insert(name, rows);
    }
    out
}

/// Holds the service's Git in the ref transactions of one ref (its
/// `reference-transaction` hook in the `prepared` state, the ref's lock
/// taken) until `release`, at most 30 s.
#[cfg(unix)]
pub struct RefHold {
    reached: PathBuf,
    released: PathBuf,
}

#[cfg(unix)]
impl RefHold {
    /// Installs the hook for `refname` in the service's hooks of `w`.
    pub fn new(w: &World, refname: &str) -> RefHold {
        use std::os::unix::fs::PermissionsExt;
        let reached = w.dir.path().join("hold-reached");
        let released = w.dir.path().join("hold-released");
        let hook = w.server.data.join("hooks/reference-transaction");
        // `reached` holds the service's pid: the hook's parent is Git, whose
        // parent is the service.
        std::fs::write(
            &hook,
            format!(
                "#!/bin/sh\n[ \"$1\" = prepared ] || {{ cat >/dev/null; exit 0; }}\n\
                 grep -q ' {refname}$' || exit 0\n\
                 ps -o ppid= -p $PPID >'{0}.tmp'\nmv '{0}.tmp' '{0}'\n\
                 i=0\nwhile [ ! -e '{1}' ] && [ $i -lt 3000 ]; do sleep 0.01; i=$((i+1)); done\n",
                reached.display(),
                released.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&hook, std::fs::Permissions::from_mode(0o755)).unwrap();
        RefHold { reached, released }
    }

    /// Whether Git is held (or was).
    pub fn reached(&self) -> bool {
        self.reached.exists()
    }

    /// Waits until Git is held, at most 15 s.
    pub async fn wait(&self) {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(15);
        while !self.reached() {
            assert!(
                std::time::Instant::now() < deadline,
                "Git never reached the hold"
            );
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    }

    /// The pid of the service whose Git is held.
    pub fn service_pid(&self) -> u32 {
        std::fs::read_to_string(&self.reached)
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }

    pub fn release(&self) {
        std::fs::write(&self.released, "").unwrap();
    }

    /// The file whose creation releases Git (`release`).
    pub fn released_path(&self) -> PathBuf {
        self.released.clone()
    }
}

/// Sends `request` and drops it once `hold` holds the service's Git: the
/// client gives up in the middle of the operation.
#[cfg(unix)]
pub async fn abandon_at(hold: &RefHold, request: impl std::future::Future) {
    tokio::select! {
        _ = request => panic!("the request ended before Git reached the hold"),
        _ = hold.wait() => {}
    }
}
