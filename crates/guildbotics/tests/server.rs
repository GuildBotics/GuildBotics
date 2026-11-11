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

#[path = "support/real_device.rs"]
mod real_device;

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
        .env_remove("GUILDBOTICS_WORKSPACE_ROOT")
        .env_remove("GUILDBOTICS_CONFIG_DIR")
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
        Server::spawn(server_command(dir, port), dir, port)
    }

    fn spawn(mut command: Command, dir: &Path, port: u16) -> Server {
        let mut child = command
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let stdout = lines(child.stdout.take().unwrap());
        let stderr = lines(child.stderr.take().unwrap());
        loop {
            let line = stdout
                .recv_timeout(Duration::from_secs(30))
                .unwrap_or_else(|error| {
                    let _ = child.kill();
                    let _ = child.wait();
                    let diagnostics = stderr
                        .recv_timeout(Duration::from_millis(250))
                        .ok()
                        .into_iter()
                        .chain(stderr.try_iter())
                        .collect::<Vec<_>>()
                        .join("\n");
                    panic!("the server did not announce its port: {error}\n{diagnostics}");
                });
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
            if line.contains("Branvil ready on the shared listener") {
                return self.url("");
            }
        }
    }

    async fn exit_within(&mut self, timeout: Duration) -> std::process::ExitStatus {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                return status;
            }
            if std::time::Instant::now() >= deadline {
                self.child.kill().unwrap();
                self.child.wait().unwrap();
                panic!("combined server did not exit");
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
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

/// Screen responses must agree with the Python services on the same persisted data.
#[tokio::test]
async fn command_and_diagnostics_screens_match_the_python_golden() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../records/golden/screens.json")).unwrap();
    let dir = tempfile::tempdir().unwrap();
    let root = host_name(dir.path());
    let workspace = root.join("workspace");
    for (section, base) in [
        ("config", workspace.join(".guildbotics/config")),
        ("identity", root.join("identity")),
    ] {
        for (name, text) in fixture[section].as_object().unwrap() {
            let path = base.join(name);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, text.as_str().unwrap()).unwrap();
        }
    }
    let run = workspace.join(".guildbotics/local/run");
    std::fs::create_dir_all(run.join("sessions")).unwrap();
    let expected = &fixture["expected"];
    std::fs::write(
        run.join("diagnostics.jsonl"),
        expected["index"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| format!("{v}\n"))
            .collect::<String>(),
    )
    .unwrap();
    for (name, text) in expected["sessions"].as_object().unwrap() {
        std::fs::write(run.join("sessions").join(name), text.as_str().unwrap()).unwrap();
    }
    let port = free_port();
    let mut command = server_command(&root, port);
    command
        .env("HOME", &root)
        .env("GUILDBOTICS_HOME", root.join(".guildbotics"))
        .env("GUILDBOTICS_IDENTITY_ROOT", root.join("identity"))
        .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace);
    let mut server = Server::spawn(command, &root, port);
    server.home = root.join(".guildbotics");
    let normalize = |value: Value| -> Value {
        serde_json::from_str(
            &serde_json::to_string(&value)
                .unwrap()
                .replace(root.to_str().unwrap(), "<root>"),
        )
        .unwrap()
    };
    // Host topology was added after the Python fixture; this server has no Branvil.
    let mut expected_status = expected["status"].clone();
    expected_status["branvil_available"] = json!(false);
    let mut urls = vec![
        (
            "/-/guildbotics/api/config/status".to_owned(),
            expected_status,
        ),
        (
            "/-/guildbotics/api/team".to_owned(),
            expected["team"].clone(),
        ),
        (
            "/-/guildbotics/api/commands/files".to_owned(),
            expected["files"].clone(),
        ),
        (
            "/-/guildbotics/api/diagnostics/traces".to_owned(),
            expected["traces"].clone(),
        ),
    ];
    for detail in expected["details"].as_array().unwrap() {
        urls.push((
            format!(
                "/-/guildbotics/api/commands/files/{}",
                detail["id"].as_str().unwrap()
            ),
            detail.clone(),
        ));
    }
    for detail in expected["trace_details"].as_array().unwrap() {
        urls.push((
            format!(
                "/-/guildbotics/api/diagnostics/traces/{}",
                detail["trace_id"].as_str().unwrap()
            ),
            detail.clone(),
        ));
    }
    for (url, expected) in urls {
        let (status, _, actual) = body(
            server
                .http
                .get(server.url(&url))
                .bearer_auth(server.owner_token())
                .send()
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(status, StatusCode::OK, "{url}: {actual}");
        assert_eq!(normalize(actual), expected, "{url}");
        assert_eq!(
            server
                .http
                .get(server.url(&url))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::UNAUTHORIZED
        );
    }
    for filter in ["limit=0", "limit=1001", "limit=invalid"] {
        assert_eq!(
            server
                .http
                .get(server.url(&format!("/-/guildbotics/api/diagnostics/traces?{filter}")))
                .bearer_auth(server.owner_token())
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::UNPROCESSABLE_ENTITY
        );
    }
    for filter in [
        "source=scheduled",
        "person_id=other",
        "q=not-present",
        "attr_key=test&attr_value=missing",
    ] {
        let (_, _, actual) = body(
            server
                .http
                .get(server.url(&format!("/-/guildbotics/api/diagnostics/traces?{filter}")))
                .bearer_auth(server.owner_token())
                .send()
                .await
                .unwrap(),
        )
        .await;
        assert_eq!(actual, json!({"traces":[]}));
    }
    let detail = expected["details"]
        .as_array()
        .unwrap()
        .iter()
        .find(|d| d["command"] == "look")
        .unwrap();
    let request = json!({"command":"look","person":"aiko","expected_command_file_id":detail["id"],"expected_command_file_revision":detail["revision"]});
    let mut changed = request.clone();
    changed["expected_command_file_revision"] = "stale".into();
    let (status, refusal) = server.run(changed, "en").await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(refusal["code"], "command_file_changed");
    changed = request.clone();
    changed["command"] = "simple".into();
    let (status, refusal) = server.run(changed, "en").await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(refusal["code"], "command_file_shadowed");
    changed = request.clone();
    changed
        .as_object_mut()
        .unwrap()
        .remove("expected_command_file_revision");
    assert_eq!(
        server.run(changed, "en").await.0,
        StatusCode::UNPROCESSABLE_ENTITY
    );
    // The selected file is the one that runs, but this device has no snapshot
    // to run it in: the screens are told before anything starts.
    let (status, refusal) = server.run(request.clone(), "en").await;
    assert_eq!(status, StatusCode::CONFLICT, "{refusal}");
    assert_eq!(refusal["code"], "command_requirement_missing");
    let member_command = workspace.join(".guildbotics/config/team/members/aiko/commands/look.md");
    std::fs::create_dir_all(member_command.parent().unwrap()).unwrap();
    std::fs::write(member_command, "Member command\n").unwrap();
    let (status, refusal) = server.run(request, "en").await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(refusal["code"], "command_file_shadowed");
    assert_eq!(refusal["context"]["shadow_source"], "member");
    server.stop();
}

#[tokio::test]
async fn the_port_says_it_listens_and_lets_only_its_owner_in() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    let token = server.owner_token();
    assert!(token.starts_with("gbt_"), "{token}");

    let health = server
        .http
        .get(server.url("/-/guildbotics/api/health"))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    let (status, headers, health) = body(health).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(health["status"], "ok");
    assert!(!health["machine_name"].as_str().unwrap().is_empty());
    assert_eq!(
        (&health["workspace"], &health["workspace_id"]),
        (&Value::Null, &Value::Null)
    );
    assert!(!health["service_instance_id"].as_str().unwrap().is_empty());
    assert_eq!(headers["x-frame-options"], "DENY");

    let refused = server
        .http
        .get(server.url("/-/guildbotics/api/health"))
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
        .get(server.url("/-/guildbotics/api/health"))
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

    let owner_id = &token[4..20];
    for nonce in [
        json!(""),
        json!("0".repeat(63)),
        json!("g".repeat(64)),
        json!(42),
    ] {
        let refused = server
            .http
            .post(server.url("/-/guildbotics/api/local-api/proof"))
            .json(&json!({"token_id":owner_id,"nonce":nonce}))
            .send()
            .await
            .unwrap();
        assert_eq!(
            refused.status(),
            StatusCode::UNPROCESSABLE_ENTITY,
            "{nonce}"
        );
    }
    let unknown = server
        .http
        .post(server.url("/-/guildbotics/api/local-api/proof"))
        .json(&json!({"token_id":"0123456789abcdef","nonce":"0".repeat(64)}))
        .send()
        .await
        .unwrap();
    assert_eq!(unknown.status(), StatusCode::UNAUTHORIZED);

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
            .get(server.url("/-/guildbotics/api/health"))
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
        .get(server.url("/-/guildbotics/api/health"))
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
        .post(server.url("/-/guildbotics/api/local-api/proof"))
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
            .post(server.url("/-/guildbotics/api/local-api/proof"))
            .json(&request)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), status);
    }
    server.stop();
}

impl Server {
    /// The health the owner reads.
    async fn health(&self) -> Value {
        let response = self
            .http
            .get(self.url("/-/guildbotics/api/health"))
            .bearer_auth(self.owner_token())
            .send()
            .await
            .unwrap();
        let (status, _, health) = body(response).await;
        assert_eq!(status, StatusCode::OK, "{health}");
        health
    }
}

/// `path` as the Python version names a host path: without the links the
/// OS puts at the top, such as macOS's `/var`.
fn host_name(path: &Path) -> PathBuf {
    let parent = std::fs::canonicalize(path.parent().unwrap()).unwrap();
    parent.join(path.file_name().unwrap())
}

#[tokio::test]
async fn the_server_opens_the_workspace_selected_and_names_it_by_its_identity() {
    let dir = tempfile::tempdir().unwrap();
    let selected = host_name(&dir.path().join("selected"));
    std::fs::create_dir(&selected).unwrap();
    let data = dir.path().join("home/data");
    std::fs::create_dir_all(&data).unwrap();
    std::fs::write(
        data.join("active-workspace.json"),
        json!({"workspace": selected, "config_dir": selected.join(".guildbotics/config")})
            .to_string(),
    )
    .unwrap();
    let server = Server::start(dir.path());

    let health = server.health().await;
    assert_eq!(health["workspace"], json!(selected));
    let identity: Value = serde_json::from_slice(
        &std::fs::read(selected.join(".guildbotics/state/workspace.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(health["workspace_id"], identity["workspace_id"]);
    let device: Value =
        serde_json::from_slice(&std::fs::read(data.join("device.json")).unwrap()).unwrap();
    assert_eq!(health["machine_name"], device["display_name"]);
    assert_eq!(server.health().await, health);
    let registered: Vec<PathBuf> =
        serde_json::from_slice(&std::fs::read(data.join("workspaces.json")).unwrap()).unwrap();
    assert_eq!(registered, [selected]);
    server.stop();

    // A workspace named explicitly wins over the selected one.
    let named = host_name(&dir.path().join("named"));
    let port = free_port();
    let mut command = server_command(dir.path(), port);
    command.env("GUILDBOTICS_WORKSPACE_ROOT", &named);
    let server = Server::spawn(command, dir.path(), port);
    let health = server.health().await;
    assert_eq!(health["workspace"], json!(named));
    assert!(named.join(".guildbotics/state/workspace.json").is_file());
    server.stop();
}

#[cfg(unix)]
#[tokio::test]
async fn a_workspace_reached_through_a_link_is_not_opened() {
    let dir = tempfile::tempdir().unwrap();
    let real = host_name(&dir.path().join("real"));
    std::fs::create_dir(&real).unwrap();
    let link = host_name(&dir.path().join("link"));
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let port = free_port();
    let mut command = server_command(dir.path(), port);
    command.env("GUILDBOTICS_WORKSPACE_ROOT", &link);
    let server = Server::spawn(command, dir.path(), port);
    let health = server.health().await;
    assert_eq!(
        (&health["workspace"], &health["workspace_id"]),
        (&Value::Null, &Value::Null)
    );
    assert!(!real.join(".guildbotics").exists());
    server.stop();
}

#[tokio::test]
async fn a_browser_enters_once_with_a_link_and_writes_with_its_csrf() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    let token = server.owner_token();
    let link = server
        .http
        .post(server.url("/-/guildbotics/api/session/links"))
        .bearer_auth(&token)
        .json(&json!({"next": "/commands"}))
        .send()
        .await
        .unwrap();
    let (status, _, link) = body(link).await;
    assert_eq!(status, StatusCode::CREATED);
    let url = link["url"].as_str().unwrap();
    let (page, code) = url.split_once('#').unwrap();
    assert_eq!(page, server.url("/-/guildbotics/-/enter?next=%2Fcommands"));

    let entered = server
        .http
        .post(server.url("/-/guildbotics/api/session"))
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
        .post(server.url("/-/guildbotics/api/session"))
        .json(&json!({"code": code}))
        .send()
        .await
        .unwrap();
    assert_eq!(again.status(), StatusCode::UNAUTHORIZED);

    let current = server
        .http
        .get(server.url("/-/guildbotics/api/session"))
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
        .post(server.url("/-/guildbotics/api/session/links"))
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
        .post(server.url("/-/guildbotics/api/session/links"))
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
            .starts_with(&server.url("/-/guildbotics/-/enter#"))
    );
    let elsewhere = server
        .http
        .post(server.url("/-/guildbotics/api/session/links"))
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

/// `guildbotics open` of the machine root under `dir`, for the port, with
/// proxies that lead nowhere: the port is reached directly.
fn open(dir: &Path, port: u16) -> Output {
    let mut command = Command::new(BIN);
    for proxy in ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"] {
        command.env(proxy, "http://127.0.0.1:9");
    }
    command
        .args(["open", "--print"])
        .env("LANGUAGE", "en")
        .env_remove("NO_PROXY")
        .env_remove("no_proxy")
        .env("GUILDBOTICS_HOME", dir.join("home"))
        .env("GUILDBOTICS_SERVER_PORT", port.to_string())
        .stdin(Stdio::null())
        .output()
        .unwrap()
}

#[tokio::test]
async fn open_prints_an_entry_link_a_browser_enters_with_once() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    let opened = open(dir.path(), server.port);
    assert_ok(&opened);
    let url = String::from_utf8(opened.stdout).unwrap();
    let (page, code) = url.trim().split_once('#').unwrap();
    assert_eq!(page, server.url("/-/guildbotics/-/enter"));
    for expected in [StatusCode::OK, StatusCode::UNAUTHORIZED] {
        let entered = server
            .http
            .post(server.url("/-/guildbotics/api/session"))
            .json(&json!({"code": code}))
            .send()
            .await
            .unwrap();
        assert_eq!(entered.status(), expected);
    }
    // The page the link opens, refused for another host, as every request.
    let page = server.http.get(page).send().await.unwrap();
    assert!(
        [StatusCode::OK, StatusCode::SERVICE_UNAVAILABLE].contains(&page.status()),
        "{page:?}"
    );
    assert_eq!(page.headers()["x-frame-options"], "DENY");
    let misdirected = server
        .http
        .get(server.url("/"))
        .header("host", "evil.example")
        .send()
        .await
        .unwrap();
    assert_eq!(misdirected.status(), StatusCode::MISDIRECTED_REQUEST);
    server.stop();
}

#[test]
fn open_without_a_server_says_so() {
    let dir = tempfile::tempdir().unwrap();
    let opened = open(dir.path(), free_port());
    assert!(!opened.status.success());
    assert!(opened.stdout.is_empty());
    assert!(
        String::from_utf8_lossy(&opened.stderr)
            .starts_with("Error: This machine has no credential that the GuildBotics server")
    );
}

/// A listener answering every request with `answer`, and the requests it
/// read, lowercased, once no more come.
async fn listener(answer: String) -> (u16, tokio::task::JoinHandle<Vec<String>>) {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let requests = tokio::spawn(async move {
        let mut seen = Vec::new();
        while let Ok(Ok((mut stream, _))) =
            tokio::time::timeout(Duration::from_secs(5), listener.accept()).await
        {
            let mut request = vec![0; 8192];
            let read = stream.read(&mut request).await.unwrap();
            seen.push(String::from_utf8_lossy(&request[..read]).to_lowercase());
            stream.write_all(answer.as_bytes()).await.unwrap();
        }
        seen
    });
    (port, requests)
}

/// Whatever listens on the port sees the owner token only once it proves it
/// knows the token's digest, and never sends the client elsewhere.
#[tokio::test]
async fn open_sends_the_token_only_to_a_listener_that_proves_it_is_the_server() {
    let json = |status: &str, headers: &str, body: &str| {
        format!(
            "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\n{headers}connection: close\r\n\r\n{body}",
            body.len()
        )
    };
    let (elsewhere, reached) = listener(json("200 OK", "", r#"{"proof":"0000"}"#)).await;
    // The redirect first, while the listener it names still listens.
    for answer in [
        json(
            "307 Temporary Redirect",
            &format!(
                "location: http://127.0.0.1:{elsewhere}/-/guildbotics/api/local-api/proof\r\n"
            ),
            "",
        ),
        json("200 OK", "", r#"{"proof":"0000"}"#),
    ] {
        let dir = tempfile::tempdir().unwrap();
        let (port, requests) = listener(answer).await;
        let tokens = dir.path().join(format!("home/data/server/{port}"));
        std::fs::create_dir_all(&tokens).unwrap();
        std::fs::write(tokens.join("owner.token"), "gbt_0123456789abcdef_secret\n").unwrap();
        let opened = tokio::task::spawn_blocking({
            let dir = dir.path().to_path_buf();
            move || open(&dir, port)
        })
        .await
        .unwrap();
        assert!(!opened.status.success());
        let seen = requests.await.unwrap();
        assert_eq!(seen.len(), 1, "{seen:?}");
        assert!(
            seen[0].starts_with("post /-/guildbotics/api/local-api/proof "),
            "{seen:?}"
        );
        assert!(!seen[0].contains("authorization") && !seen[0].contains("secret"));
    }
    assert_eq!(reached.await.unwrap(), Vec::<String>::new());
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
fn failed_bind_releases_the_server_lock_for_the_next_start() {
    let root = tempfile::tempdir().unwrap();
    let reserved = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = reserved.local_addr().unwrap().port();
    let failed = server_command(root.path(), port).output().unwrap();
    assert!(!failed.status.success());
    assert!(failed.stdout.is_empty(), "{failed:?}");
    drop(reserved);
    let server = Server::spawn(server_command(root.path(), port), root.path(), port);
    assert!(server.owner_token().starts_with("gbt_"));
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
    let occupied = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    std::fs::write(
        data.join("branvil.toml"),
        format!(
            "listen = '{}'\nbase_url = 'http://127.0.0.1:1'\n",
            occupied.local_addr().unwrap()
        ),
    )
    .unwrap();
    let token = std::fs::read_to_string(data.join("secrets/owner.token")).unwrap();
    let server = Server::start(dir.path());
    let url = server.branvil_url();
    assert_eq!(url, server.url(""));

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

#[tokio::test]
async fn open_commands_share_the_entry_and_browser_session() {
    let dir = tempfile::tempdir().unwrap();
    let listen = format!("127.0.0.1:{}", free_port());
    let data = set_up_branvil(dir.path(), &listen);
    let server = Server::start(dir.path());
    let _ = server.branvil_url();
    for (args, next) in [
        (vec!["open", "--print"], None),
        (vec!["open", "--branvil", "--print"], Some("/")),
        (vec!["branvil", "open", "--print"], Some("/")),
    ] {
        let opened = Command::new(BIN)
            .args(args)
            .env("BRANVIL_DATA_DIR", &data)
            .env("GUILDBOTICS_HOME", &server.home)
            .env("GUILDBOTICS_SERVER_PORT", server.port.to_string())
            .env_remove("BRANVIL_URL")
            .output()
            .unwrap();
        assert_ok(&opened);
        let link = String::from_utf8(opened.stdout).unwrap();
        let link = reqwest::Url::parse(link.trim()).unwrap();
        assert_eq!(link.origin().ascii_serialization(), server.url(""));
        assert_eq!(link.path(), "/-/guildbotics/-/enter");
        assert_eq!(
            link.query_pairs()
                .find(|(key, _)| key == "next")
                .map(|(_, value)| value.into_owned())
                .as_deref(),
            next
        );
        let entered = server
            .http
            .post(server.url("/-/guildbotics/api/session"))
            .json(&json!({"code": link.fragment().unwrap()}))
            .send()
            .await
            .unwrap();
        let (status, headers, session) = body(entered).await;
        assert_eq!(status, StatusCode::OK);
        let cookie = headers["set-cookie"]
            .to_str()
            .unwrap()
            .split(';')
            .next()
            .unwrap();
        for path in ["/-/guildbotics/api/session", "/api/v1/session"] {
            let current = server
                .http
                .get(server.url(path))
                .header("cookie", cookie)
                .send()
                .await
                .unwrap();
            let (status, _, current) = body(current).await;
            assert_eq!(status, StatusCode::OK);
            assert!(current["csrf"] == session["csrf"]);
            if path == "/api/v1/session" {
                assert_eq!(current["me"]["kind"], "owner");
            }
        }
    }
    server.stop();
}

/// A Ctrl-C while the server still starts -- here held, after its notice,
/// by the lock of the workspaces' registry -- stops Branvil too, which has
/// not started yet. The second signal escalates while startup is still held.
#[test]
fn ctrl_c_while_starting_reports_drain_then_cancellation_and_stops_branvil() {
    let dir = tempfile::tempdir().unwrap();
    set_up_branvil(dir.path(), "127.0.0.1:0");
    let workspace = dir.path().join("workspace");
    std::fs::create_dir_all(&workspace).unwrap();
    let registry = dir.path().join("home/data/workspaces.lock");
    std::fs::create_dir_all(registry.parent().unwrap()).unwrap();
    let held = std::fs::File::create(&registry).unwrap();
    held.lock().unwrap();
    let port = free_port();
    let mut command = server_command(dir.path(), port);
    command
        .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace)
        .env("LANGUAGE", "en");
    let mut server = Server::spawn(command, dir.path(), port);
    for key in [
        "runtime.service_lock.stopping_after_work",
        "runtime.service_lock.cancelling",
    ] {
        let interrupted = Command::new("kill")
            .args(["-INT", &server.child.id().to_string()])
            .output()
            .unwrap();
        assert!(interrupted.status.success());
        let expected = gb_server::i18n::t("en", key, &[]);
        loop {
            let line = server.stderr.recv_timeout(Duration::from_secs(5)).unwrap();
            if line == expected {
                break;
            }
        }
    }
    drop(held);
    let deadline = std::time::Instant::now() + Duration::from_secs(30);
    let status = loop {
        if let Some(status) = server.child.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            server.child.kill().unwrap();
            panic!("Ctrl-C did not end the server");
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    assert!(status.success(), "{status:?}");
}

#[test]
fn branvil_failing_to_start_stops_the_server() {
    let dir = tempfile::tempdir().unwrap();
    let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let data = set_up_branvil(dir.path(), &taken.local_addr().unwrap().to_string());
    std::fs::write(data.join("branvil.toml"), "invalid = true").unwrap();
    let mut child = server_command(dir.path(), free_port())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stderr = String::new();
    std::io::Read::read_to_string(child.stderr.as_mut().unwrap(), &mut stderr).unwrap();
    let status = child.wait().unwrap();
    assert!(!status.success());
    assert!(stderr.contains("unknown field"), "{stderr}");
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

/// A workspace under `dir` whose team is one member, `yuki`, with commands
/// of its own: `look` (read-only) and `write` (not).
fn workspace_with_a_member(dir: &Path) -> PathBuf {
    let workspace = host_name(&dir.join("workspace"));
    let files = [
        (
            workspace.join(".guildbotics/config/team/project.yml"),
            "name: Run\nlanguage: ja-JP\n",
        ),
        (
            workspace.join(".guildbotics/config/team/members/yuki/member.yml"),
            "is_active: true\nprofile:\n  roles: {}\n",
        ),
        (
            dir.join("home/identity/.guildbotics/config/team/members/yuki/identity.yml"),
            "person_id: yuki\nname: Yuki Nakamura\nperson_type: agent\n",
        ),
        (
            workspace.join(".guildbotics/config/commands/look.md"),
            "---\nread_only: true\n---\nLook\n",
        ),
        (
            workspace.join(".guildbotics/config/commands/write.md"),
            "Write\n",
        ),
    ];
    for (path, text) in files {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, text).unwrap();
    }
    workspace
}

impl Server {
    /// Asks the server to run a command, in the language `language`.
    async fn run(&self, request: Value, language: &str) -> (StatusCode, Value) {
        let response = self
            .http
            .post(self.url("/-/guildbotics/api/commands/run"))
            .bearer_auth(self.owner_token())
            .header("x-guildbotics-language", language)
            .json(&request)
            .send()
            .await
            .unwrap();
        let (status, _, body) = body(response).await;
        (status, body)
    }
}

/// The records of the trace `trace_id`: their types, and the last one.
fn trace_records(workspace: &Path, trace_id: &str) -> (Vec<String>, Value) {
    let text = std::fs::read_to_string(
        workspace.join(format!(".guildbotics/local/run/sessions/{trace_id}.jsonl")),
    )
    .unwrap();
    let records: Vec<Value> = text
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    let types = records
        .iter()
        .map(|r| r["type"].as_str().unwrap().to_owned())
        .collect();
    (types, records.last().unwrap().clone())
}

#[tokio::test]
async fn a_command_is_refused_before_it_starts_when_it_cannot_be_run_as_asked() {
    let dir = tempfile::tempdir().unwrap();
    let unselected = Server::start(dir.path());
    let (status, refused) = unselected
        .run(
            json!({"command": "look", "expected_workspace": "other-workspace-id"}),
            "en",
        )
        .await;
    assert_eq!(
        (status, refused["code"].as_str()),
        (StatusCode::CONFLICT, Some("command_workspace_changed"))
    );
    let (status, refused) = unselected.run(json!({"command": "look"}), "en").await;
    assert_eq!(
        (status, refused["code"].as_str()),
        (StatusCode::BAD_REQUEST, Some("config_not_found"))
    );
    unselected.stop();

    let workspace = workspace_with_a_member(dir.path());
    let port = free_port();
    let mut command = server_command(dir.path(), port);
    command.env("GUILDBOTICS_WORKSPACE_ROOT", &workspace);
    let server = Server::spawn(command, dir.path(), port);
    for (request, status, code) in [
        (
            json!({"command": ""}),
            StatusCode::UNPROCESSABLE_ENTITY,
            "validation_error",
        ),
        // The selected file must exist before a guarded run can start.
        (
            json!({"command": "look", "expected_command_file_id": "x", "expected_command_file_revision": "y"}),
            StatusCode::NOT_FOUND,
            "command_file_not_found",
        ),
        (
            json!({"command": "look", "expected_workspace": "other-workspace-id"}),
            StatusCode::CONFLICT,
            "command_workspace_changed",
        ),
        (
            json!({"command": "look", "cwd": "relative"}),
            StatusCode::BAD_REQUEST,
            "command_cwd_not_absolute",
        ),
        (
            json!({"command": "look", "cwd": "/a/../b"}),
            StatusCode::BAD_REQUEST,
            "command_error",
        ),
        (
            json!({"command": "look", "person": "nobody"}),
            StatusCode::BAD_REQUEST,
            "person_not_found",
        ),
        (
            json!({"command": "missing"}),
            StatusCode::BAD_REQUEST,
            "command_error",
        ),
    ] {
        let (answered, refused) = server.run(request.clone(), "en").await;
        assert_eq!(
            (answered, refused["code"].as_str()),
            (status, Some(code)),
            "{request}"
        );
    }
    let (_, refused) = server
        .run(json!({"command": "look", "person": "nobody"}), "ja")
        .await;
    assert_eq!(
        refused["message"],
        "メンバー 'nobody' が見つかりません。指定できるのは: yuki (Yuki Nakamura)"
    );
    let (_, refused) = server.run(json!({"command": "missing"}), "en").await;
    assert_eq!(refused["message"], "Unable to locate command 'missing'.");
    let project = workspace.join(".guildbotics/config/team/project.yml");
    std::fs::remove_file(&project).unwrap();
    let (status, refused) = server.run(json!({"command": "look"}), "en").await;
    assert_eq!(
        (status, &refused["code"], &refused["context"]["path"]),
        (
            StatusCode::BAD_REQUEST,
            &json!("config_not_found"),
            &json!(project)
        )
    );
    // Refused commands open no trace: only the server's startup is recorded,
    // and the screens are told of the runs refused for their member.
    let run = workspace.join(".guildbotics/local/run");
    let index = std::fs::read_to_string(run.join("diagnostics.jsonl")).unwrap();
    let (refusals, records): (Vec<Value>, Vec<Value>) = index
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .partition(|record| record["type"] == "command.failed");
    assert_eq!(refusals.len(), 2, "{refusals:?}");
    for refusal in &refusals {
        assert!(refusal["trace_id"].is_null());
        assert_eq!(refusal["payload"]["code"], "person_not_found");
        assert_eq!(refusal["payload"]["identifier"], "nobody");
    }
    assert_eq!(records.len(), 1, "{records:?}");
    let started = &records[0];
    assert_eq!(started["type"], "system.started");
    assert_eq!(started["source"], "system");
    assert!(started["trace_id"].is_null());
    let transcripts: Vec<_> = std::fs::read_dir(run.join("sessions"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    assert_eq!(transcripts.len(), 1, "{transcripts:?}");
    assert_eq!(
        transcripts[0],
        run.join(started["payload"]["path"].as_str().unwrap())
    );
    let session = std::fs::read_to_string(&transcripts[0]).unwrap();
    let session_events = session
        .lines()
        .filter(|line| serde_json::from_str::<Value>(line).unwrap()["kind"] != "log")
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    assert_eq!(session_events, index);
    assert!(
        !index
            .lines()
            .any(|line| serde_json::from_str::<Value>(line).unwrap()["kind"] == "log")
    );
    server.stop();
}

#[tokio::test]
async fn a_command_that_cannot_start_its_environment_ends_failed_in_its_trace() {
    let dir = tempfile::tempdir().unwrap();
    let workspace = workspace_with_a_member(dir.path());
    let port = free_port();
    let mut command = server_command(dir.path(), port);
    command.env("GUILDBOTICS_WORKSPACE_ROOT", &workspace);
    // Doctor repairs missing/outdated runtimes. An unplaceable runtime is
    // still refused before an invalid declaration, then a missing snapshot.
    let msb = dir.path().join("home/data/msb");
    let declaration = workspace.join(".guildbotics/config/intelligences/agent_environment.yml");
    // Keep startup upkeep outside the real runtime boundary as well.
    std::fs::create_dir_all(declaration.parent().unwrap()).unwrap();
    std::fs::write(&declaration, "resources:\n  cpus: 0\n").unwrap();
    std::fs::create_dir_all(msb.parent().unwrap()).unwrap();
    std::fs::write(&msb, "not a directory").unwrap();
    let server = Server::spawn(command, dir.path(), port);
    let refusals = [
        gb_server::i18n::t(
            "ja",
            "intelligences.agent_environment.runtime.not_placed",
            &[("home", &msb.display().to_string()), ("error", "")],
        ),
        "The resources in".into(),
        "この端末にはエージェント実行環境がありません。".into(),
    ];
    for (step, refusal) in refusals.iter().enumerate() {
        match step {
            // What a later step refuses is wrong from the start.
            0 => {}
            1 => {
                std::fs::remove_file(&msb).unwrap();
                for file in gb_environment::placed_files(&msb) {
                    std::fs::create_dir_all(file.parent().unwrap()).unwrap();
                    std::fs::write(file, "").unwrap();
                }
                std::fs::write(msb.join("version"), "0.6.16\n").unwrap();
            }
            2 => std::fs::remove_file(&declaration).unwrap(),
            _ => {}
        }
        let (status, failed) = server
            .run(json!({"command": "look", "cwd": "~/somewhere"}), "ja")
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{failed}");
        assert_eq!(failed["code"], "command_error");
        assert!(
            failed["message"]
                .as_str()
                .unwrap()
                .starts_with(refusal.as_str()),
            "{failed}"
        );
        if step == 0 {
            assert!(
                failed["message"]
                    .as_str()
                    .unwrap()
                    .strip_prefix(refusal.as_str())
                    .is_some_and(|reason| !reason.trim().is_empty())
            );
        }
        if step == 1 {
            gb_environment::runtime::ready(&msb)
                .expect("doctor must repair the outdated bundle before validating resources");
            assert_eq!(
                std::fs::read_to_string(msb.join("version")).unwrap(),
                gb_environment::runtime::RUNTIME_VERSION
            );
        }
        let trace_id = failed["context"]["trace_id"].as_str().unwrap();
        let (types, last) = trace_records(&workspace, trace_id);
        assert_eq!(types, ["command.started", "command.failed"]);
        assert_eq!(
            (&last["source"], &last["person_id"], &last["command"]),
            (&json!("manual"), &json!("yuki"), &json!("look"))
        );
        assert_eq!(last["attributes"], json!({"run.entry": "command"}));
        assert_eq!(last["payload"]["error_type"], "CommandError");
    }

    // Image validation reaches the API as a localized, value-specific refusal
    // before any real image store or microVM is opened.
    std::fs::write(
        &declaration,
        "image:\n  reference: local/agent:1\n  digests:\n    arm64: bad-digest\n",
    )
    .unwrap();
    for (language, phrase) in [("ja", "image の digest"), ("en", "image digest")] {
        let (status, failed) = server
            .run(json!({"command": "look", "cwd": "~/somewhere"}), language)
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        let message = failed["message"].as_str().unwrap();
        assert!(
            message.contains("bad-digest")
                && message.contains(phrase)
                && message.contains("agent_environment.yml"),
            "{message}"
        );
    }
    std::fs::remove_file(&declaration).unwrap();

    // Writable commands use the same environment preflight and failure trace.
    let (status, failed) = server.run(json!({"command": "write"}), "en").await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{failed}");
    assert_eq!(
        failed["message"],
        "The agent environment is missing on this device."
    );
    let trace_id = failed["context"]["trace_id"].as_str().unwrap();
    assert_eq!(
        trace_records(&workspace, trace_id).0,
        ["command.started", "command.failed"]
    );
    server.stop();
}

/// Boots a real microVM from a ready workspace's snapshot, with the
/// device's runtime: only with the owner's approval, watched by them, one at
/// a time (`--ignored`, `GUILDBOTICS_E2E_ENVIRONMENT_FROM=<ready workspace>`).
#[tokio::test]
#[ignore = "boots a real microVM: GUILDBOTICS_E2E_ENVIRONMENT_FROM and the owner's approval"]
async fn a_read_only_command_runs_in_a_microvm_of_its_own() {
    let source = PathBuf::from(
        std::env::var("GUILDBOTICS_E2E_ENVIRONMENT_FROM")
            .expect("GUILDBOTICS_E2E_ENVIRONMENT_FROM names a ready workspace"),
    );
    let home = PathBuf::from(std::env::var("HOME").unwrap());
    // The runtime opens unix sockets under its home, and macOS caps their
    // paths at 103 bytes: keep the machine root short.
    let root = home.join(".gbb");
    std::fs::create_dir_all(&root).unwrap();
    let dir = tempfile::Builder::new()
        .prefix("")
        .tempdir_in(&root)
        .unwrap();
    let workspace = workspace_with_a_member(dir.path());
    let snapshots = ".guildbotics/local/agent_environment/snapshots";
    std::fs::create_dir_all(workspace.join(snapshots).parent().unwrap()).unwrap();
    std::os::unix::fs::symlink(source.join(snapshots), workspace.join(snapshots)).unwrap();
    std::fs::create_dir_all(dir.path().join("home/data")).unwrap();
    std::os::unix::fs::symlink(
        home.join(".guildbotics/data/msb"),
        dir.path().join("home/data/msb"),
    )
    .unwrap();
    let port = free_port();
    let mut command = server_command(dir.path(), port);
    command.env("GUILDBOTICS_WORKSPACE_ROOT", &workspace);
    let server = Server::spawn(command, dir.path(), port);

    let (status, ran) = server
        .run(
            json!({"command": "functions/get_os_ui_language", "message": "こんにちは"}),
            "en",
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{ran}");
    // The command's result as its text output spells it, told the host's
    // language.
    let language = gb_environment::language::ui_language().await.unwrap();
    let output: Vec<&str> = ran["output"].as_str().unwrap().lines().collect();
    assert_eq!(output[0], "input: こんにちは", "{ran}");
    assert_eq!(
        output[1],
        format!("language_code: {}", language.split('_').next().unwrap()),
        "{ran}"
    );
    let (types, _) = trace_records(&workspace, ran["trace_id"].as_str().unwrap());
    assert_eq!(types, ["command.started", "command.finished"]);

    // SIGTERM, as a service manager sends it, cancels the command running,
    // whose trace says so, and the server ends as it was asked to.
    let running = {
        let (http, url, token) = (
            server.http.clone(),
            server.url("/-/guildbotics/api/commands/run"),
            server.owner_token(),
        );
        tokio::spawn(async move {
            let response = http
                .post(url)
                .bearer_auth(token)
                .json(&json!({"command": "functions/get_os_ui_language"}))
                .send()
                .await
                .unwrap();
            body(response).await
        })
    };
    tokio::time::sleep(Duration::from_secs(1)).await;
    let terminated = Command::new("kill")
        .args(["-TERM", &server.child.id().to_string()])
        .output()
        .unwrap();
    assert!(terminated.status.success());
    let (status, _, cancelled) = running.await.unwrap();
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{cancelled}");
    assert_eq!(cancelled["code"], "command_cancelled");
    let (types, last) = trace_records(
        &workspace,
        cancelled["context"]["trace_id"].as_str().unwrap(),
    );
    assert_eq!(types, ["command.started", "command.failed"]);
    assert_eq!(last["payload"]["code"], "cancelled");
    let output = server.child.wait_with_output().unwrap();
    assert!(output.status.success(), "{:?}", output.status);
}

#[test]
fn sigterm_with_nothing_running_ends_the_server_as_asked() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    let terminated = Command::new("kill")
        .args(["-TERM", &server.child.id().to_string()])
        .output()
        .unwrap();
    assert!(terminated.status.success());
    let output = server.child.wait_with_output().unwrap();
    assert!(output.status.success(), "{:?}", output.status);
}

fn service_command(dir: &Path, args: &[&str]) -> Output {
    // Service commands use the port held by the server, not the one in the
    // calling shell's environment.
    Command::new(BIN)
        .arg("service")
        .args(args)
        .env("GUILDBOTICS_HOME", dir.join("home"))
        .env("GUILDBOTICS_SERVER_PORT", "1")
        .env("LANGUAGE", "en")
        .env("NO_COLOR", "1")
        .output()
        .unwrap()
}

#[test]
fn service_commands_need_a_running_server() {
    let dir = tempfile::tempdir().unwrap();
    let status = service_command(dir.path(), &["status"]);
    assert_ok(&status);
    assert_eq!(
        serde_json::from_slice::<Value>(&status.stdout).unwrap(),
        json!({"server_running":false,"running":false,"state":"stopped"})
    );
    for action in ["start", "stop"] {
        let output = service_command(dir.path(), &[action]);
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("No GuildBotics server is running.")
        );
    }
}

#[test]
fn service_commands_reach_the_port_held_by_the_server_and_show_its_result() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    assert_service_status(dir.path(), false);
    // A server with no workspace refuses to start its services.
    let started = service_command(dir.path(), &["start"]);
    // Stopping idle services succeeds for both modes.
    for args in [&["stop"][..], &["stop", "--force"][..]] {
        let stopped = service_command(dir.path(), args);
        assert_ok(&stopped);
        assert!(String::from_utf8_lossy(&stopped.stdout).contains("The services stopped."));
    }
    server.stop();
    assert!(!started.status.success());
    assert!(started.stdout.is_empty());
    assert!(
        String::from_utf8_lossy(&started.stderr)
            .contains("GuildBotics configuration is not available.")
    );
}

fn assert_service_status(root: &Path, running: bool) {
    let output = service_command(root, &["status"]);
    assert_ok(&output);
    let status: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(status["server_running"], true);
    assert_eq!(status["running"], running);
    assert!(status["state"].is_string());
}

#[test]
fn run_accepts_options_after_the_command_and_executes_with_or_without_a_server() {
    let dir = tempfile::tempdir().unwrap();
    let root = host_name(dir.path());
    let workspace = workspace_with_a_member(&root);
    let port = free_port();
    let mut command = server_command(&root, port);
    command
        .env("HOME", &root)
        .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace);
    let server = Server::spawn(command, &root, port);
    let run = || {
        Command::new(BIN)
            .env("HOME", &root)
            .env("GUILDBOTICS_HOME", root.join("home"))
            .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace)
            .env("GUILDBOTICS_SERVER_PORT", "1")
            .env("LANGUAGE", "en")
            .args(["run", "look@yuki", "--person", "unknown"])
            .arg("--cwd")
            .arg(&workspace)
            .stdin(Stdio::null())
            .output()
            .unwrap()
    };
    let via_server = run();
    server.stop();
    let local = run();
    for output in [&via_server, &local] {
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        let error = String::from_utf8_lossy(&output.stderr);
        assert!(
            error.contains("unknown") && error.contains("yuki"),
            "{error}"
        );
    }
    assert_eq!(local.stderr, via_server.stderr);
}

#[test]
fn server_stop_uses_the_running_port_and_waits_for_both_servers() {
    let dir = tempfile::tempdir().unwrap();
    set_up_branvil(dir.path(), "127.0.0.1:0");
    let stop = |force: bool| {
        let mut command = Command::new(BIN);
        command
            .args(["server", "stop"])
            .env("GUILDBOTICS_HOME", dir.path().join("home"))
            .env("GUILDBOTICS_SERVER_PORT", "1")
            .env("LANGUAGE", "en");
        if force {
            command.arg("--force");
        }
        command.output().unwrap()
    };
    for force in [false, true] {
        let server = Server::start(dir.path());
        let branvil = server.branvil_url();
        let output = stop(force);
        assert_ok(&output);
        assert!(String::from_utf8_lossy(&output.stdout).contains(if force {
            "The server stopped after cancelling in-flight work."
        } else {
            "The server stopped."
        }));
        assert!(server.child.wait_with_output().unwrap().status.success());
        let address = branvil.strip_prefix("http://").unwrap();
        assert!(std::net::TcpStream::connect(address).is_err());
    }
    let output = stop(false);
    assert_ok(&output);
    assert!(String::from_utf8_lossy(&output.stdout).contains("No GuildBotics server is running."));
}

#[tokio::test]
async fn graceful_shutdown_keeps_branvil_available_until_guildbotics_drains() {
    for via_api in [true, false] {
        let dir = tempfile::tempdir().unwrap();
        set_up_branvil(dir.path(), "127.0.0.1:0");
        let mut server = Server::start(dir.path());
        let branvil = format!("{}/api/v1/health", server.branvil_url());
        let workspace = dir.path().join("workspace");
        std::fs::create_dir(&workspace).unwrap();
        let registry = gb_base::files::FileLock::hold(
            &server.home.join("data/workspaces.lock"),
            Duration::from_secs(1),
        )
        .unwrap();
        // Branvil's router is ready before startup/autostart releases workspace
        // admission. Retry only that documented conflict until the switch is
        // admitted and blocked on the registry held by this fixture.
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                match server
                    .http
                    .post(server.url("/-/guildbotics/api/workspace"))
                    .bearer_auth(server.owner_token())
                    .json(&json!({"workspace_dir": workspace}))
                    .timeout(Duration::from_millis(300))
                    .send()
                    .await
                {
                    Err(error) => {
                        assert!(error.is_timeout(), "{error}");
                        break;
                    }
                    Ok(response) => {
                        assert_eq!(response.status(), StatusCode::CONFLICT);
                        let body: Value = response.json().await.unwrap();
                        assert_eq!(body["code"], "workspace_switch_blocked_by_active_work");
                    }
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .expect("startup never released workspace admission");
        // A conflict confirms that the detached switch still holds admission
        // while waiting to persist its selection.
        let reply = server
            .http
            .post(server.url("/-/guildbotics/api/scheduler/start"))
            .bearer_auth(server.owner_token())
            .send()
            .await
            .unwrap();
        assert_eq!(reply.status(), StatusCode::CONFLICT);

        if via_api {
            let response = server
                .http
                .post(server.url("/-/guildbotics/api/shutdown"))
                .bearer_auth(server.owner_token())
                .json(&json!({"force": false}))
                .send()
                .await
                .unwrap();
            assert_eq!(response.status(), StatusCode::ACCEPTED);
        } else {
            let result = Command::new("kill")
                .args(["-INT", &server.child.id().to_string()])
                .output()
                .unwrap();
            assert_ok(&result);
        }
        let mut availability = Ok(());
        let deadline = std::time::Instant::now() + Duration::from_millis(500);
        while std::time::Instant::now() < deadline {
            availability = server
                .http
                .get(&branvil)
                .timeout(Duration::from_secs(1))
                .send()
                .await
                .and_then(reqwest::Response::error_for_status)
                .map(|_| ());
            if availability.is_err() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        drop(registry);
        assert!(server.exit_within(Duration::from_secs(10)).await.success());
        let active: Value = serde_json::from_slice(
            &std::fs::read(dir.path().join("home/data/active-workspace.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(active["workspace"], json!(host_name(&workspace)));
        assert!(
            availability.is_ok(),
            "Branvil stopped while GuildBotics was still draining (API: {via_api}): {availability:?}"
        );
    }
}

#[tokio::test]
async fn combined_shutdown_waits_for_ci_with_the_machine_lock_and_can_be_interrupted() {
    let dir = tempfile::tempdir().unwrap();
    let data = set_up_branvil(dir.path(), "127.0.0.1:0");
    let config = data.join("branvil.toml");
    let configure = |seconds| {
        std::fs::write(
            &config,
            format!("listen = '127.0.0.1:0'\n[ci]\nstop_wait_secs = {seconds}\n"),
        )
        .unwrap();
    };
    configure(2);
    let mut server = Server::start(dir.path());
    let mut url = server.branvil_url();
    let owner = std::fs::read_to_string(data.join("secrets/owner.token")).unwrap();
    for args in [
        &["workspace", "create", "acme"][..],
        &["repo", "create", "acme/demo"],
        &["member", "create", "fixture-runner", "--kind", "runner"],
    ] {
        let mut command = vec!["branvil"];
        command.extend(args);
        command.extend(["--url", &url]);
        assert_ok(&guildbotics(&data, &command));
    }
    let token = guildbotics(
        &data,
        &[
            "branvil",
            "member",
            "token",
            "fixture-runner",
            "--url",
            &url,
        ],
    );
    assert_ok(&token);
    let runner = String::from_utf8(token.stdout).unwrap();
    let work = dir.path().join("work");
    let home = dir.path().join("git-home");
    std::fs::create_dir_all(work.join(".branvil")).unwrap();
    std::fs::create_dir(&home).unwrap();
    std::fs::write(
        work.join(".branvil/ci.toml"),
        "[[jobs]]\nname = 'fixture'\nimage = 'docker.io/library/alpine:3.22'\ncommands = ['echo fixture']\n",
    )
    .unwrap();
    let remote = format!("{url}/git/acme/demo.git");
    for args in [
        &["init", "-q", "-b", "main"][..],
        &["add", ".branvil/ci.toml"],
        &["commit", "-qm", "seed CI"],
        &["push", "-q", &remote, "main"],
    ] {
        assert_ok(&git(&work, &home, owner.trim(), args));
    }

    for interrupt in [false, true] {
        let claim = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let response = server
                    .http
                    .post(format!("{url}/api/v1/runner/claim"))
                    .bearer_auth(runner.trim())
                    .json(&json!({"executor": "test", "executor_state": "ready"}))
                    .send()
                    .await
                    .unwrap();
                match response.status() {
                    StatusCode::OK => break response.json::<Value>().await.unwrap(),
                    StatusCode::NO_CONTENT => (),
                    status => panic!("claim: {status}"),
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(claim["job"]["name"], "fixture");
        let stop = server
            .http
            .post(server.url("/-/guildbotics/api/shutdown"))
            .bearer_auth(server.owner_token())
            .json(&json!({"force": false}))
            .send()
            .await
            .unwrap();
        assert_eq!(stop.status(), StatusCode::ACCEPTED);
        let saw_wait = tokio::time::timeout(Duration::from_secs(5), async {
            loop {
                let response = server
                    .http
                    .get(format!("{url}/api/v1/admin/diagnostics"))
                    .bearer_auth(owner.trim())
                    .send()
                    .await?;
                let diagnostics: Value = response.error_for_status()?.json().await?;
                if diagnostics["runner"]["state"] == "stopping" {
                    break Ok::<_, reqwest::Error>(());
                }
                tokio::time::sleep(Duration::from_millis(20)).await;
            }
        })
        .await;
        if !matches!(saw_wait, Ok(Ok(()))) {
            let _ = server.child.kill();
            let _ = server.child.wait();
            panic!("Branvil never started waiting for the leased job: {saw_wait:?}");
        }
        assert!(
            gb_server::running(&server.home.join("data"), "en")
                .unwrap()
                .is_some()
        );
        let refused = server
            .http
            .get(server.url("/-/guildbotics/api/config/status"))
            .bearer_auth(server.owner_token())
            .send()
            .await
            .unwrap();
        assert_eq!(refused.status(), StatusCode::SERVICE_UNAVAILABLE);
        let attempt = claim["attempt_id"].as_str().unwrap();
        let heartbeat = server
            .http
            .post(format!("{url}/api/v1/runner/attempts/{attempt}/heartbeat"))
            .bearer_auth(runner.trim())
            .json(&json!({"lease": claim["lease"]}))
            .send()
            .await
            .unwrap();
        assert_eq!(heartbeat.status(), StatusCode::OK);
        let remote = format!("{url}/git/acme/demo.git");
        assert_ok(&git(
            &work,
            &home,
            owner.trim(),
            &["push", "-q", &remote, "main:refs/heads/during-drain"],
        ));
        if interrupt {
            let result = Command::new("kill")
                .args(["-TERM", &server.child.id().to_string()])
                .output()
                .unwrap();
            assert_ok(&result);
        }
        assert!(server.exit_within(Duration::from_secs(8)).await.success());
        if !interrupt {
            configure(30);
            server = Server::start(dir.path());
            url = server.branvil_url();
        }
    }
}

#[tokio::test]
async fn consent_tokens_open_the_screens_but_cannot_execute_commands() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    let verifier = loopback_auth::random_secret::<48>();
    let redirect = "http://127.0.0.1:12345/callback";
    let authorize = || {
        json!({
            "client":"GuildBotics CLI Blackbox", "redirect_uri":redirect, "state":"expected",
            "code_challenge":loopback_auth::b64(&loopback_auth::sha256(verifier.as_bytes())),
        })
    };
    let authenticated = server
        .http
        .post(server.url("/-/guildbotics/api/apps/authorizations"))
        .bearer_auth(server.owner_token())
        .json(&authorize())
        .send()
        .await
        .unwrap();
    assert_eq!(authenticated.status(), StatusCode::CREATED);
    let authorized: Value = authenticated.json().await.unwrap();
    let returned = reqwest::Url::parse(authorized["redirect"].as_str().unwrap()).unwrap();
    assert_eq!(returned.path(), "/callback");
    let pairs = returned
        .query_pairs()
        .collect::<std::collections::BTreeMap<_, _>>();
    assert_eq!(pairs["state"], "expected");
    let exchange = json!({"code":pairs["code"],"redirect_uri":redirect,"code_verifier":verifier});
    let issued = server
        .http
        .post(server.url("/-/guildbotics/api/apps/token"))
        .json(&exchange)
        .send()
        .await
        .unwrap();
    assert_eq!(issued.status(), StatusCode::CREATED);
    let issued: Value = issued.json().await.unwrap();
    let token = issued["token"].as_str().unwrap();
    let spent = server
        .http
        .post(server.url("/-/guildbotics/api/apps/token"))
        .json(&exchange)
        .send()
        .await
        .unwrap();
    assert_eq!(spent.status(), StatusCode::BAD_REQUEST);
    for (path, expected) in [
        ("/session/links", StatusCode::CREATED),
        ("/shutdown", StatusCode::UNAUTHORIZED),
        ("/commands/run", StatusCode::UNAUTHORIZED),
        ("/member/run", StatusCode::UNAUTHORIZED),
        ("/apps/authorizations", StatusCode::UNAUTHORIZED),
    ] {
        let response = server
            .http
            .post(server.url(&format!("/-/guildbotics/api{path}")))
            .bearer_auth(token)
            .json(&json!({}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), expected, "{path}");
    }
    server.stop();
}

#[test]
fn member_uses_the_active_workspace_and_reads_only_planned_caller_content() {
    let dir = tempfile::tempdir().unwrap();
    let root = host_name(dir.path());
    let workspace = workspace_with_a_member(&root);
    let other = root.join("other");
    std::fs::create_dir_all(other.join(".guildbotics/state")).unwrap();
    let fake_identity = json!({
        "schema_version": 4,
        "workspace_id": "019691c2-8dc0-7654-8000-000000000042",
        "created_at":"2026-11-06T00:00:00Z",
    });
    std::fs::write(
        other.join(".guildbotics/state/workspace.json"),
        fake_identity.to_string(),
    )
    .unwrap();
    let data = root.join("home/data");
    std::fs::create_dir_all(&data).unwrap();
    let active = data.join("active-workspace.json");
    std::fs::write(&active, json!({"workspace":workspace}).to_string()).unwrap();
    let port = free_port();
    let mut launch = server_command(&root, port);
    launch.env("HOME", &root);
    let server = Server::spawn(launch, &root, port);
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let ready = server
                .http
                .get(server.url("/-/guildbotics/api/health"))
                .bearer_auth(server.owner_token())
                .timeout(Duration::from_secs(15))
                .send()
                .await
                .unwrap();
            assert_eq!(ready.status(), StatusCode::OK);
            let ready: Value = ready.json().await.unwrap();
            assert_eq!(ready["workspace"], json!(workspace));
        });
    let cli = |args: &[&str]| {
        Command::new(BIN)
            .args(args)
            .current_dir(&root)
            .env("HOME", &root)
            .env("GUILDBOTICS_HOME", root.join("home"))
            .env("GUILDBOTICS_SERVER_PORT", "1")
            .env("LANGUAGE", "en")
            .env_remove("GUILDBOTICS_WORKSPACE_ROOT")
            .env_remove("GUILDBOTICS_CONFIG_DIR")
            .stdin(Stdio::null())
            .output()
            .unwrap()
    };
    let from_active = cli(&["member", "context", "--person", "yuki", "--format", "json"]);
    assert_ok(&from_active);
    let context: Value = serde_json::from_slice(&from_active.stdout).unwrap();
    assert_eq!(context["person_id"], "yuki");
    let named = format!("--workspace={}", other.display());
    let mismatched = cli(&["member", &named, "context", "--person", "yuki"]);
    assert!(!mismatched.status.success());
    assert!(mismatched.stdout.is_empty());
    assert!(String::from_utf8_lossy(&mismatched.stderr).contains("workspace"));
    let note = root.join("note.txt");
    std::fs::write(&note, "one line\n日本語の本文\n").unwrap();
    let content = format!("--content-file={}", note.display());
    let written = cli(&[
        "member",
        "memory",
        "record",
        "--person",
        "yuki",
        "--scope",
        "team",
        "--title",
        "File content",
        "--format",
        "json",
        &content,
    ]);
    assert_ok(&written);
    let receipt: Value = serde_json::from_slice(&written.stdout).unwrap();
    let record = cli(&[
        "member",
        "memory",
        "get",
        "--person",
        "yuki",
        "--team",
        "--id",
        receipt["doc_id"].as_str().unwrap(),
        "--format",
        "json",
    ]);
    assert_ok(&record);
    let memory: Value = serde_json::from_slice(&record.stdout).unwrap();
    assert_eq!(memory["body"], "one line\n日本語の本文\n");
    let invalid = cli(&[
        "member",
        "memory",
        "record",
        "--person",
        "yuki",
        "--scope",
        "team",
        "--title",
        "Bad",
        "--content-file",
        "does-not-exist",
        "--content-stdin",
    ]);
    assert!(!invalid.status.success());
    assert!(String::from_utf8_lossy(&invalid.stderr).contains("exactly one"));
    server.stop();
    let no_server = cli(&["member", "--help"]);
    assert!(!no_server.status.success());
    assert!(String::from_utf8_lossy(&no_server.stderr).contains("Start the GuildBotics server"));
}

#[test]
fn open_passes_one_entry_link_to_the_browser() {
    let dir = tempfile::tempdir().unwrap();
    let server = Server::start(dir.path());
    let browser = if cfg!(target_os = "macos") {
        "open"
    } else {
        "xdg-open"
    };
    let handler = dir.path().join(browser);
    std::fs::write(
        &handler,
        "#!/bin/sh\n[ \"$#\" = 1 ] && printf '%s' \"$1\" > \"$CAPTURE\"\n",
    )
    .unwrap();
    use std::os::unix::fs::PermissionsExt as _;
    std::fs::set_permissions(&handler, std::fs::Permissions::from_mode(0o700)).unwrap();
    let capture = dir.path().join("opened");
    let output = Command::new(BIN)
        .arg("open")
        .env("PATH", dir.path())
        .env("CAPTURE", &capture)
        .env("GUILDBOTICS_HOME", &server.home)
        .env("GUILDBOTICS_SERVER_PORT", server.port.to_string())
        .env("LANGUAGE", "en")
        .output()
        .unwrap();
    let expected = server.url("/-/guildbotics/-/enter#");
    server.stop();
    assert_ok(&output);
    assert!(output.stdout.is_empty());
    assert!(
        std::fs::read_to_string(capture)
            .unwrap()
            .starts_with(&expected)
    );
}

#[test]
fn current_git_uses_the_caller_commit_hook_the_pushurl_and_remote_tracking() {
    use std::os::unix::fs::PermissionsExt;

    let dir = tempfile::tempdir().unwrap();
    let root = host_name(dir.path());
    let workspace = workspace_with_a_member(&root);
    let config = workspace.join(".guildbotics/config/team");
    std::fs::write(
        config.join("project.yml"),
        "name: Git CLI\nlanguage: en\nservices:\n  code_hosting_service:\n    name: local\n    owner: acme/team\n",
    ).unwrap();
    let identity = root.join("home/identity/.guildbotics/config/team/members/yuki/identity.yml");
    std::fs::write(
        identity,
        "person_id: yuki\nname: Yuki\nperson_type: agent\naccount_info:\n  git_email: yuki@example.test\n",
    ).unwrap();
    let local = workspace.join(".guildbotics/local/services/code");
    let owner = local.join("acme%2Fteam%2Fowner/repository.git");
    let other = local.join("acme%2Fteam%2Fother/repository.git");
    let seed = root.join("seed");
    std::fs::create_dir(&seed).unwrap();
    let git_checked = |cwd: &Path, args: &[&str]| {
        let output = git(cwd, &root, "", args);
        assert_ok(&output);
        String::from_utf8(output.stdout).unwrap().trim().to_owned()
    };
    git_checked(&seed, &["init", "-q", "-b", "main"]);
    git_checked(&seed, &["commit", "-q", "--allow-empty", "-m", "base"]);
    let original = git_checked(&seed, &["rev-parse", "HEAD"]);
    for remote in [&owner, &other] {
        std::fs::create_dir_all(remote.parent().unwrap()).unwrap();
        git_checked(
            &seed,
            &["clone", "-q", "--bare", ".", remote.to_str().unwrap()],
        );
    }
    let checkout = root.join("checkout");
    git_checked(
        &seed,
        &[
            "clone",
            "-q",
            other.to_str().unwrap(),
            checkout.to_str().unwrap(),
        ],
    );
    let push_url = reqwest::Url::from_file_path(&owner).unwrap();
    git_checked(
        &checkout,
        &["remote", "set-url", "--push", "origin", push_url.as_str()],
    );
    let fetch_url = reqwest::Url::from_file_path(&other).unwrap();
    git_checked(
        &checkout,
        &["remote", "set-url", "origin", fetch_url.as_str()],
    );
    for (name, script) in [
        (
            "pre-commit",
            "#!/bin/sh\nprintf '%s' \"host${GUILDBOTICS_TEST_GUEST-}\" > hook-ran\n",
        ),
        ("pre-push", "#!/bin/sh\ntouch push-ran\nexit 1\n"),
    ] {
        let path = checkout.join(".git/hooks").join(name);
        std::fs::write(&path, script).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    std::fs::write(checkout.join("change"), "new content").unwrap();
    git_checked(&checkout, &["add", "change"]);
    let port = free_port();
    let mut command = server_command(&root, port);
    command
        .env("HOME", &root)
        .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace);
    let server = Server::spawn(command, &root, port);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let status = runtime.block_on(async {
        server
            .http
            .get(server.url("/-/guildbotics/api/health"))
            .bearer_auth(server.owner_token())
            .timeout(Duration::from_secs(15))
            .send()
            .await
            .unwrap()
            .status()
    });
    assert_eq!(
        status,
        StatusCode::OK,
        "{:?}",
        server.stderr.try_iter().collect::<Vec<_>>()
    );
    let message = root.join("commit-message");
    std::fs::write(&message, "publish current workspace").unwrap();
    let sub = checkout.join("sub");
    std::fs::create_dir(&sub).unwrap();
    std::fs::write(
        root.join("gitconfig"),
        "[author]\n\tname = Configured Human\n\temail = configured@example.test\n",
    )
    .unwrap();
    let member = |action: &str, repository: &Path| {
        let mut cli = Command::new(BIN);
        for (key, _) in std::env::vars() {
            if key.starts_with("GIT_") {
                cli.env_remove(key);
            }
        }
        cli.current_dir(&sub)
            .env("GIT_AUTHOR_NAME", "Environment Human")
            .env("GIT_COMMITTER_EMAIL", "environment@example.test")
            .env("HOME", &root)
            .env("GUILDBOTICS_HOME", root.join("home"))
            .env("LANGUAGE", "en")
            .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace)
            .env("GUILDBOTICS_SERVER_PORT", "1")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", root.join("gitconfig"))
            .args([
                "member",
                "git",
                action,
                "--workspace-mode",
                "current",
                "--person",
                "yuki",
                "--format",
                "json",
                "--repo-path",
            ])
            .arg(repository)
            .stdin(Stdio::null());
        if action == "publish" {
            cli.arg("--content-file").arg(&message);
        }
        cli.output().unwrap()
    };
    let wrong = member("publish", &seed);
    assert_eq!(wrong.status.code(), Some(1), "{wrong:?}");
    assert!(
        String::from_utf8_lossy(&wrong.stderr).contains("current workspace repository"),
        "{wrong:?}"
    );
    assert!(!checkout.join("hook-ran").exists());
    let missing = member("publish", Path::new("../missing"));
    assert_eq!(missing.stderr, wrong.stderr);
    let published = member("publish", Path::new(".."));
    assert_ok(&published);
    let published: Value = serde_json::from_slice(&published.stdout).unwrap();
    let head = git_checked(&checkout, &["rev-parse", "HEAD"]);
    assert_eq!(published["commit_sha"], head);
    assert_eq!(published["pushed"], true);
    assert_eq!(
        published["commits"][0]["message"],
        "publish current workspace"
    );
    assert_eq!(
        git_checked(&checkout, &["log", "-1", "--format=%an <%ae> / %cn <%ce>"]),
        "Yuki <yuki@example.test> / Yuki <yuki@example.test>"
    );
    assert_eq!(
        std::fs::read_to_string(checkout.join("hook-ran")).unwrap(),
        "host"
    );
    assert!(!checkout.join("push-ran").exists());
    assert_eq!(git_checked(&owner, &["rev-parse", "refs/heads/main"]), head);
    assert_eq!(
        git_checked(&other, &["rev-parse", "refs/heads/main"]),
        original
    );
    assert_eq!(
        git_checked(&checkout, &["rev-parse", "refs/remotes/origin/main"]),
        head
    );

    git_checked(
        &seed,
        &["commit", "-q", "--allow-empty", "-m", "other change"],
    );
    let ahead = git_checked(&seed, &["rev-parse", "HEAD"]);
    git_checked(
        &seed,
        &["push", "-q", "--force", owner.to_str().unwrap(), "main"],
    );
    let tracking_lock = checkout.join(".git/refs/remotes/origin/main.lock");
    std::fs::write(&tracking_lock, "fixture conflict").unwrap();
    let twice_refused = member("push", &checkout);
    assert_eq!(twice_refused.status.code(), Some(1));
    let reason = String::from_utf8_lossy(&twice_refused.stderr);
    assert!(
        reason.contains("Failed to push 'main'") && reason.contains("main.lock"),
        "{reason}"
    );
    assert_eq!(
        git_checked(&owner, &["rev-parse", "refs/heads/main"]),
        ahead
    );
    std::fs::remove_file(&tracking_lock).unwrap();
    let rejected = member("push", &checkout);
    assert_eq!(rejected.status.code(), Some(1), "{rejected:?}");
    assert!(
        String::from_utf8_lossy(&rejected.stderr).contains("Failed to push 'main'"),
        "{rejected:?}"
    );
    assert_eq!(
        git_checked(&owner, &["rev-parse", "refs/heads/main"]),
        ahead
    );
    assert_eq!(
        git_checked(&checkout, &["rev-parse", "refs/remotes/origin/main"]),
        ahead
    );
    assert_eq!(git_checked(&checkout, &["rev-parse", "HEAD"]), head);

    git_checked(&checkout, &["switch", "-c", "next"]);
    let tracking_lock = tracking_lock.with_file_name("next.lock");
    std::fs::write(&tracking_lock, "fixture conflict").unwrap();
    let tracking_refused = member("push", &checkout);
    assert_eq!(tracking_refused.status.code(), Some(1));
    let reason = String::from_utf8_lossy(&tracking_refused.stderr);
    assert!(
        reason.contains("next.lock") && !reason.contains("Failed to push"),
        "{reason}"
    );
    assert_eq!(git_checked(&owner, &["rev-parse", "refs/heads/next"]), head);
    std::fs::remove_file(tracking_lock).unwrap();
    git_checked(&checkout, &["switch", "--orphan", "empty"]);
    let empty = member("push", &checkout);
    assert_eq!(empty.status.code(), Some(1), "{empty:?}");
    assert!(
        String::from_utf8_lossy(&empty.stderr).contains("Branch 'empty' has no commit to push."),
        "{empty:?}"
    );
    server.stop();
}

impl Server {
    async fn scheduler_running(&self) -> bool {
        let response = self
            .http
            .get(self.url("/-/guildbotics/api/scheduler/status"))
            .bearer_auth(self.owner_token())
            .timeout(Duration::from_secs(15))
            .send()
            .await
            .unwrap();
        let (status, _, result) = body(response).await;
        assert_eq!(status, StatusCode::OK, "{result}");
        result["scheduler"]["running"].as_bool().unwrap()
    }
}

#[tokio::test]
async fn server_starts_without_a_workspace_with_or_without_services_requested() {
    let dir = tempfile::tempdir().unwrap();
    for requested in [false, true] {
        let port = free_port();
        let mut command = server_command(dir.path(), port);
        if requested {
            command.arg("--services");
        }
        let server = Server::spawn(command, dir.path(), port);
        assert!(server.health().await["workspace"].is_null());
        assert!(!server.scheduler_running().await);
        server.stop();
        assert!(
            gb_server::running(&dir.path().join("home/data"), "en")
                .unwrap()
                .is_none()
        );
    }
}

#[tokio::test]
async fn server_starts_services_from_settings_or_flag_and_serves_after_a_startup_failure() {
    let dir = tempfile::tempdir().unwrap();
    let root = host_name(dir.path());
    let workspace = workspace_with_a_member(&root);
    gb_base::files::atomic_write(&root.join("home/data/msb"), b"disable runtime upkeep").unwrap();
    let settings = workspace.join(".guildbotics/local/service.yml");
    for (autostart, flag, invalid) in [
        (false, false, false),
        (true, false, false),
        (false, true, false),
        (true, false, true),
    ] {
        let mut settings_text = format!(
            "start_with_server: {autostart}\nscheduled: true\nroutine: false\nevent_queue: false\n"
        );
        if invalid {
            settings_text.push_str("invalid: [\n");
        }
        gb_base::files::atomic_write(&settings, settings_text.as_bytes()).unwrap();
        let port = free_port();
        let mut command = server_command(&root, port);
        command
            .env("HOME", &root)
            .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace);
        if flag {
            command.arg("--services");
        }
        let server = Server::spawn(command, &root, port);
        assert_eq!(server.health().await["workspace"], json!(workspace));
        if invalid {
            assert!(!server.scheduler_running().await);
            let start = service_command(&root, &["start"]);
            assert_eq!(start.status.code(), Some(1), "{start:?}");
            assert!(
                String::from_utf8_lossy(&start.stderr).contains("service.yml"),
                "{start:?}"
            );
        } else {
            if !(autostart || flag) {
                assert!(!server.scheduler_running().await);
                assert_ok(&service_command(&root, &["start"]));
            }
            tokio::time::timeout(Duration::from_secs(15), async {
                while !server.scheduler_running().await {
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
            })
            .await
            .unwrap();
            assert_service_status(&root, true);
            assert_ok(&service_command(&root, &["stop"]));
            assert_service_status(&root, false);
            assert!(!server.scheduler_running().await);
        }
        assert_eq!(server.health().await["workspace"], json!(workspace));
        server.stop();
    }
}
