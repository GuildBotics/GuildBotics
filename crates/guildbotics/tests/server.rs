//! `guildbotics server start`: GuildBotics' port as the Python server's
//! launchers and screens expect it, and Branvil in the same process, whose
//! commands it starts again on this executable (a push runs the hooks as
//! `guildbotics branvil hook`).

#![cfg(unix)]

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output, Stdio};
use std::sync::mpsc;
use std::time::Duration;

use reqwest::StatusCode;
use serde_json::{Value, json};

const BIN: &str = env!("CARGO_BIN_EXE_guildbotics");

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

/// The environment of a server under `dir`: its own machine root and port,
/// and Branvil's data directory there (set up or not).
fn server_command(dir: &Path, port: u16) -> Command {
    let mut command = Command::new(BIN);
    command
        .args(["server", "start"])
        .env("GUILDBOTICS_HOME", dir.join("home"))
        .env("GUILDBOTICS_SERVER_PORT", port.to_string())
        .env("BRANVIL_DATA_DIR", dir.join("branvil"))
        .env("NO_COLOR", "1")
        .stdin(Stdio::null());
    command
}

fn lines(stream: impl std::io::Read + Send + 'static) -> mpsc::Receiver<String> {
    let (sender, received) = mpsc::channel();
    std::thread::spawn(move || {
        for line in BufReader::new(stream).lines().map_while(Result::ok) {
            let _ = sender.send(line);
        }
    });
    received
}

struct Server {
    child: Child,
    port: u16,
    home: PathBuf,
    stdout: mpsc::Receiver<String>,
    stderr: mpsc::Receiver<String>,
    http: reqwest::Client,
}

impl Server {
    /// Starts the server and waits for its notice that it listens.
    fn start(dir: &Path) -> Server {
        Server::start_on(dir, free_port())
    }

    fn start_on(dir: &Path, port: u16) -> Server {
        let mut child = server_command(dir, port)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let stdout = lines(child.stdout.take().unwrap());
        let stderr = lines(child.stderr.take().unwrap());
        loop {
            let line = stdout
                .recv_timeout(Duration::from_secs(30))
                .expect("the server says that it listens");
            if line == format!("GUILDBOTICS_APP_API_PORT={port}") {
                break;
            }
            assert_eq!(line, "", "nothing but the notice on stdout");
        }
        Server {
            child,
            port,
            home: dir.join("home"),
            stdout,
            stderr,
            http: reqwest::Client::builder().no_proxy().build().unwrap(),
        }
    }

    fn owner_token(&self) -> String {
        std::fs::read_to_string(
            self.home
                .join(format!("data/server/{}/owner.token", self.port)),
        )
        .unwrap()
        .trim()
        .to_string()
    }

    fn url(&self, path: &str) -> String {
        format!("http://127.0.0.1:{}{path}", self.port)
    }

    /// The URL Branvil says it listens on.
    fn branvil_url(&self) -> String {
        loop {
            let line = self
                .stderr
                .recv_timeout(Duration::from_secs(30))
                .expect("Branvil says where it listens");
            if let Some((_, rest)) = line.split_once("listening on ") {
                return rest.split_whitespace().next().unwrap().to_string();
            }
        }
    }

    /// Stops the server as a Ctrl-C does: a stop asked for is a successful
    /// one, and stdout carried nothing after the notice.
    fn stop(self) {
        let stopped = Command::new("kill")
            .args(["-INT", &self.child.id().to_string()])
            .output()
            .unwrap();
        assert!(stopped.status.success());
        let output = self.child.wait_with_output().unwrap();
        assert!(output.status.success(), "{:?}", output.status);
        assert_eq!(
            self.stdout.try_iter().collect::<Vec<_>>(),
            Vec::<String>::new()
        );
    }
}

async fn body(response: reqwest::Response) -> (StatusCode, reqwest::header::HeaderMap, Value) {
    let status = response.status();
    let headers = response.headers().clone();
    let text = response.text().await.unwrap();
    (
        status,
        headers,
        serde_json::from_str(&text).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn the_port_says_it_listens_and_lets_only_its_owner_in() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    let token = server.owner_token();
    assert!(token.starts_with("gbt_"), "{token}");

    let health = server
        .http
        .get(server.url("/api/health"))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    let (status, headers, health) = body(health).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(health["status"], "ok");
    assert!(!health["machine_name"].as_str().unwrap().is_empty());
    assert!(!health["service_instance_id"].as_str().unwrap().is_empty());
    assert_eq!(headers["x-frame-options"], "DENY");

    let refused = server
        .http
        .get(server.url("/api/health"))
        .send()
        .await
        .unwrap();
    let (status, headers, refused) = body(refused).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(refused["code"], "unauthorized");
    assert!(
        refused["message"]
            .as_str()
            .unwrap()
            .starts_with("This request has no valid session or token.")
    );
    assert_eq!(refused["context"], json!({}));
    assert!(
        headers["content-security-policy"]
            .to_str()
            .unwrap()
            .contains("frame-ancestors 'none'")
    );
    let in_japanese = server
        .http
        .get(server.url("/api/health"))
        .header("X-GuildBotics-Language", "ja")
        .send()
        .await
        .unwrap();
    let (_, _, in_japanese) = body(in_japanese).await;
    assert!(
        in_japanese["message"]
            .as_str()
            .unwrap()
            .starts_with("有効なセッションまたはトークンがありません。"),
        "{in_japanese}"
    );

    for (header, value, status, code) in [
        (
            "host",
            "evil.example".to_string(),
            StatusCode::MISDIRECTED_REQUEST,
            "misdirected",
        ),
        (
            "origin",
            "http://evil.example".to_string(),
            StatusCode::FORBIDDEN,
            "origin_mismatch",
        ),
        // The origin of another of the machine's servers.
        (
            "origin",
            format!("http://127.0.0.1:{}", server.port + 1),
            StatusCode::FORBIDDEN,
            "origin_mismatch",
        ),
    ] {
        let response = server
            .http
            .get(server.url("/api/health"))
            .bearer_auth(&token)
            .header(header, value)
            .send()
            .await
            .unwrap();
        let (got, headers, error) = body(response).await;
        assert_eq!((got, error["code"].as_str()), (status, Some(code)));
        assert_eq!(headers["x-content-type-options"], "nosniff");
    }
    let same = server
        .http
        .get(server.url("/api/health"))
        .bearer_auth(&token)
        .header("origin", server.url(""))
        .send()
        .await
        .unwrap();
    assert_eq!(same.status(), StatusCode::OK);

    // The client's proof that the port is this server's.
    let (id, secret) = token["gbt_".len()..].split_once('_').unwrap();
    let nonce = "ab".repeat(32);
    let proof = server
        .http
        .post(server.url("/api/local-api/proof"))
        .json(&json!({"token_id": id, "nonce": nonce}))
        .send()
        .await
        .unwrap();
    let (status, _, proof) = body(proof).await;
    assert_eq!(status, StatusCode::OK);
    let expected = loopback_auth::hex(&loopback_auth::hmac_sha256(
        loopback_auth::digest(secret).as_bytes(),
        format!("guildbotics-local-api-proof\n{nonce}\n{id}").as_bytes(),
    ));
    assert_eq!(proof["proof"], expected);
    for (request, status) in [
        (
            json!({"token_id": "0123456789abcdef", "nonce": nonce}),
            StatusCode::UNAUTHORIZED,
        ),
        (
            json!({"token_id": id, "nonce": "short"}),
            StatusCode::UNPROCESSABLE_ENTITY,
        ),
    ] {
        let response = server
            .http
            .post(server.url("/api/local-api/proof"))
            .json(&request)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), status);
    }
    server.stop();
}

#[tokio::test]
async fn a_browser_enters_once_with_a_link_and_writes_with_its_csrf() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    let token = server.owner_token();
    let link = server
        .http
        .post(server.url("/api/session/links"))
        .bearer_auth(&token)
        .json(&json!({"next": "/commands"}))
        .send()
        .await
        .unwrap();
    let (status, _, link) = body(link).await;
    assert_eq!(status, StatusCode::CREATED);
    let url = link["url"].as_str().unwrap();
    let (page, code) = url.split_once('#').unwrap();
    assert_eq!(page, server.url("/-/enter?next=%2Fcommands"));

    let entered = server
        .http
        .post(server.url("/api/session"))
        .json(&json!({"code": code}))
        .send()
        .await
        .unwrap();
    let (status, headers, session) = body(entered).await;
    assert_eq!(status, StatusCode::OK);
    let csrf = session["csrf"].as_str().unwrap().to_string();
    let set_cookie = headers["set-cookie"].to_str().unwrap();
    let name = format!("guildbotics_session_{}=", server.port);
    assert!(set_cookie.starts_with(&name), "{set_cookie}");
    assert!(
        set_cookie.contains("HttpOnly") && set_cookie.contains("SameSite=Strict"),
        "{set_cookie}"
    );
    let cookie = set_cookie.split(';').next().unwrap().to_string();
    let again = server
        .http
        .post(server.url("/api/session"))
        .json(&json!({"code": code}))
        .send()
        .await
        .unwrap();
    assert_eq!(again.status(), StatusCode::UNAUTHORIZED);

    let current = server
        .http
        .get(server.url("/api/session"))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    let (status, _, current) = body(current).await;
    assert_eq!(
        (status, current["csrf"].as_str()),
        (StatusCode::OK, Some(csrf.as_str()))
    );
    let without = server
        .http
        .post(server.url("/api/session/links"))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    let (status, _, without) = body(without).await;
    assert_eq!(
        (status, without["code"].as_str()),
        (StatusCode::FORBIDDEN, Some("csrf_mismatch"))
    );
    let with = server
        .http
        .post(server.url("/api/session/links"))
        .header("cookie", &cookie)
        .header("X-GuildBotics-CSRF", &csrf)
        .send()
        .await
        .unwrap();
    let (status, _, with) = body(with).await;
    assert_eq!(status, StatusCode::CREATED);
    assert!(
        with["url"]
            .as_str()
            .unwrap()
            .starts_with(&server.url("/-/enter#"))
    );
    let elsewhere = server
        .http
        .post(server.url("/api/session/links"))
        .bearer_auth(&token)
        .json(&json!({"next": "//evil.example"}))
        .send()
        .await
        .unwrap();
    let (status, _, elsewhere) = body(elsewhere).await;
    assert_eq!(
        (status, elsewhere["code"].as_str()),
        (StatusCode::UNPROCESSABLE_ENTITY, Some("validation_error"))
    );
    server.stop();
}

#[test]
fn nothing_can_write_to_stdout_after_the_notice() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    // The launcher's pipe is closed while the server still runs.
    assert_eq!(
        server.stdout.recv_timeout(Duration::from_secs(10)),
        Err(mpsc::RecvTimeoutError::Disconnected)
    );
    server.stop();
}

#[test]
fn one_server_runs_per_machine_root() {
    let dir = tempfile::tempdir().unwrap();
    let first = Server::start(dir.path());
    let other_port = free_port();
    let second = server_command(dir.path(), other_port).output().unwrap();
    // Refused before it touched any records.
    assert!(
        !dir.path()
            .join(format!("home/data/server/{other_port}"))
            .exists()
    );
    assert!(!second.status.success());
    assert!(
        String::from_utf8_lossy(&second.stderr)
            .contains("Another GuildBotics server is already running."),
        "{}",
        String::from_utf8_lossy(&second.stderr)
    );
    assert_eq!(String::from_utf8_lossy(&second.stdout), "");
    let token = first.owner_token();
    let port = first.port;
    first.stop();
    // The lock goes with its holder, and the owner token of a port stays.
    let next = Server::start_on(dir.path(), port);
    assert_eq!(next.owner_token(), token);
    next.stop();
}

#[test]
fn an_empty_branvil_data_dir_is_refused_rather_than_the_current_directory() {
    let dir = tempfile::tempdir().unwrap();
    let work = dir.path().join("work");
    std::fs::create_dir(&work).unwrap();
    let started = server_command(dir.path(), free_port())
        .current_dir(&work)
        .env("BRANVIL_DATA_DIR", "")
        .output()
        .unwrap();

    assert!(!started.status.success());
    assert!(String::from_utf8_lossy(&started.stderr).contains("BRANVIL_DATA_DIR is empty"));
    assert_eq!(std::fs::read_dir(&work).unwrap().count(), 0);
}

fn guildbotics(data: &Path, args: &[&str]) -> Output {
    Command::new(BIN)
        .args(args)
        .env("BRANVIL_DATA_DIR", data)
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

/// Git with no configuration but its own, answering the credential prompt
/// with the owner's token.
fn git(dir: &Path, home: &Path, token: &str, args: &[&str]) -> Output {
    let askpass = home.join("askpass.sh");
    std::fs::write(
        &askpass,
        "#!/bin/sh\ncase \"$1\" in\n  Username*) echo owner ;;\n  *) printf '%s\\n' \"$TEST_TOKEN\" ;;\nesac\n",
    )
    .unwrap();
    std::fs::set_permissions(
        &askpass,
        std::os::unix::fs::PermissionsExt::from_mode(0o700),
    )
    .unwrap();
    let global = home.join("gitconfig");
    std::fs::write(&global, "").unwrap();
    let mut cmd = Command::new("git");
    for (key, _) in std::env::vars() {
        if key.starts_with("GIT_") || key == "SSH_ASKPASS" {
            cmd.env_remove(key);
        }
    }
    cmd.current_dir(dir)
        .args(["-c", "credential.helper=", "-c", "core.askPass="])
        .args(args)
        .env("HOME", home)
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", &global)
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_ASKPASS", &askpass)
        .env("TEST_TOKEN", token)
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@example.invalid")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@example.invalid")
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

fn assert_ok(output: &Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
fn branvil_runs_in_the_server_and_its_hooks_run_on_this_executable() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("branvil");
    assert_ok(&guildbotics(
        &data,
        &[
            "branvil",
            "init",
            "--owner",
            "owner",
            "--listen",
            "127.0.0.1:0",
        ],
    ));
    let token = std::fs::read_to_string(data.join("secrets/owner.token")).unwrap();
    let server = Server::start(dir.path());
    let url = server.branvil_url();

    for args in [
        ["branvil", "workspace", "create", "acme"],
        ["branvil", "repo", "create", "acme/demo"],
    ] {
        let mut args = args.to_vec();
        args.extend(["--url", &url]);
        assert_ok(&guildbotics(&data, &args));
    }
    let hook = std::fs::read_to_string(data.join("hooks/pre-receive")).unwrap();
    assert!(
        hook.contains(&format!("exec '{BIN}' 'branvil' 'hook' 'pre-receive'")),
        "{hook}"
    );

    let home = dir.path().join("git-home");
    let work = dir.path().join("work");
    std::fs::create_dir_all(&home).unwrap();
    std::fs::create_dir_all(&work).unwrap();
    std::fs::write(work.join("README.md"), "demo\n").unwrap();
    for args in [
        vec!["init", "-q", "-b", "main"],
        vec!["add", "README.md"],
        vec!["commit", "-q", "-m", "first"],
    ] {
        assert_ok(&git(&work, &home, "", &args));
    }
    // The pre-receive hook refuses a push it cannot run.
    let remote = format!("{url}/git/acme/demo.git");
    assert_ok(&git(
        &work,
        &home,
        token.trim(),
        &["push", "-q", &remote, "main"],
    ));
    let listed = git(&work, &home, token.trim(), &["ls-remote", &remote, "main"]);
    assert_ok(&listed);
    let head = git(&work, &home, "", &["rev-parse", "HEAD"]);
    assert!(
        String::from_utf8_lossy(&listed.stdout)
            .starts_with(String::from_utf8_lossy(&head.stdout).trim())
    );
    server.stop();
}

/// `guildbotics branvil init` with Branvil's data under `dir` and listening on
/// `listen`.
fn set_up_branvil(dir: &Path, listen: &str) -> PathBuf {
    let data = dir.join("branvil");
    assert_ok(&guildbotics(
        &data,
        &["branvil", "init", "--owner", "owner", "--listen", listen],
    ));
    data
}

#[test]
fn branvil_failing_to_start_stops_the_server() {
    let dir = tempfile::tempdir().unwrap();
    let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    set_up_branvil(dir.path(), &taken.local_addr().unwrap().to_string());
    let mut child = server_command(dir.path(), free_port())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stderr = String::new();
    std::io::Read::read_to_string(child.stderr.as_mut().unwrap(), &mut stderr).unwrap();
    let status = child.wait().unwrap();
    assert!(!status.success());
    assert!(stderr.contains("cannot listen"), "{stderr}");
}

#[test]
fn a_refused_server_never_starts_branvil() {
    let dir = tempfile::tempdir().unwrap();
    let first = Server::start(dir.path());
    let other = tempfile::tempdir().unwrap();
    let data = set_up_branvil(other.path(), "127.0.0.1:0");
    let refused = server_command(dir.path(), free_port())
        .env("BRANVIL_DATA_DIR", &data)
        .output()
        .unwrap();
    assert!(!refused.status.success());
    // Branvil writes its hooks for the running executable as it starts.
    assert!(!data.join("hooks/pre-receive").exists());
    first.stop();
}
