//! Test harness: a real `branvil serve` on an ephemeral loopback port, real
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

pub const BIN: &str = env!("CARGO_BIN_EXE_branvil");

pub struct Server {
    child: Option<ServiceProcess>,
    /// The service's standard output, line by line.
    lines: std::sync::mpsc::Receiver<String>,
    pub url: String,
    pub data: PathBuf,
    pub http: reqwest::Client,
}

/// `branvil init` into `dir/data`; returns the owner's token (from the file
/// init writes).
pub fn init(dir: &Path, owner: &str) -> String {
    let out = Command::new(BIN)
        .args([
            "init",
            "--owner",
            owner,
            "--listen",
            "127.0.0.1:0",
            "--data-dir",
        ])
        .arg(dir.join("data"))
        .output()
        .expect("run branvil init");
    assert!(
        out.status.success(),
        "init failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    owner_token(&dir.join("data"))
}

/// The owner's token as `<data>/secrets/owner.token` holds it now.
pub fn owner_token(data: &Path) -> String {
    std::fs::read_to_string(data.join("secrets").join("owner.token"))
        .expect("owner.token")
        .trim()
        .to_string()
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
        let mut cmd = Command::new(BIN);
        cmd.args(["serve", "--listen", listen, "--data-dir"])
            .arg(&data)
            .env("BRANVIL_LOG", "branvil=debug")
            .env_remove("BRANVIL_FAILPOINT")
            .stdout(Stdio::piped())
            .stderr(log);
        for (k, v) in env {
            cmd.env(k, v);
        }
        let mut child = ServiceProcess::spawn(&mut cmd).expect("spawn branvil serve");
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
            http: reqwest::Client::new(),
        }
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

    /// A new single-use entry link of the owner (`POST /session/links`);
    /// returns its code, the URL fragment.
    pub async fn entry_code(&self, owner: &str) -> String {
        let (st, v) = self.post("/session/links", owner, json!({})).await;
        assert_eq!(st, StatusCode::CREATED, "{v}");
        let url = v["url"].as_str().unwrap();
        url.strip_prefix(&format!("{}/-/enter#", self.url))
            .unwrap_or_else(|| panic!("unexpected link {url}"))
            .to_string()
    }

    /// Exchanges an entry code for a session (`POST /session`).
    pub async fn enter(&self, code: &str) -> reqwest::Response {
        self.http
            .post(format!("{}/api/v1/session", self.url))
            .json(&json!({"code": code}))
            .send()
            .await
            .unwrap()
    }

    /// A new GUI session of the owner: the cookie pair and the CSRF value.
    pub async fn session(&self, owner: &str) -> (String, String) {
        let resp = self.enter(&self.entry_code(owner).await).await;
        assert_eq!(resp.status(), StatusCode::OK);
        let cookie = resp.headers()["set-cookie"]
            .to_str()
            .unwrap()
            .split(';')
            .next()
            .unwrap()
            .to_string();
        let body: Value = resp.json().await.unwrap();
        (cookie, body["csrf"].as_str().unwrap().to_string())
    }

    /// The clone URL (credentials come from `git_as`'s askpass, never the URL).
    pub fn remote(&self, repo: &str) -> String {
        format!("{}/git/{repo}.git", self.url)
    }

    /// Runs `branvil` with the given arguments against this server.
    pub fn cli(&self, token: &str, args: &[&str]) -> Output {
        Command::new(BIN)
            .args(args)
            .env("BRANVIL_URL", &self.url)
            .env("BRANVIL_TOKEN", token)
            .env("BRANVIL_DATA_DIR", &self.data)
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
        git_as_ok(&work, "alice", &owner, &["push", "-q", "origin", "main"]);
        let mut t = tokens.into_iter();
        World {
            owner,
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
        let out = Command::new(BIN)
            .args(["restore", "--from"])
            .arg(&backup)
            .arg("--data-dir")
            .arg(into.join("data"))
            .output()
            .unwrap();
        assert!(
            out.status.success(),
            "{}",
            String::from_utf8_lossy(&out.stderr)
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
pub fn as_schema_24(db: &Path) {
    edit_records(db, "pulls", |v| {
        if let Some(x) = v["upstream"].as_object_mut() {
            x.remove("checks_registered_by");
            x.remove("checks_settled");
        }
    });
    edit_records(db, "meta", |v| {
        if v.is_u64() {
            *v = json!(24);
        }
    });
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
