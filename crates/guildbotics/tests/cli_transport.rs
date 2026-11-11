//! Exercise the executable against an authenticated peer with scripted replies.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

use serde_json::{Value, json};

const BIN: &str = env!("CARGO_BIN_EXE_guildbotics");

fn peer_workspace(root: &Path) -> PathBuf {
    let workspace = root.join("workspace");
    std::fs::create_dir_all(workspace.join(".guildbotics/config")).unwrap();
    gb_base::files::atomic_write(
        &workspace.join(".guildbotics/state/workspace.json"),
        br#"{"schema_version":4,"workspace_id":"019691c2-8dc0-7654-8000-000000000042","created_at":"2026-11-06T00:00:00Z"}"#,
    ).unwrap();
    workspace
}

/// A loopback peer with a real discovery lock and proof, but scripted replies.
struct CliPeer {
    _lock: gb_base::files::FileLock,
    listener: tokio::net::TcpListener,
}

impl CliPeer {
    const TOKEN: &str = "gbt_0123456789abcdef_fixture-token";

    async fn bind(root: &Path) -> Self {
        use std::io::Write as _;

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let data = root.join("home/data");
        let lock =
            gb_base::files::FileLock::hold(&data.join("run/app-api.lock"), Duration::from_secs(1))
                .unwrap();
        let mut record = lock.file();
        record.set_len(0).unwrap();
        writeln!(
            record,
            " {}",
            json!({"pid": std::process::id(), "port": port})
        )
        .unwrap();
        let token_dir = data.join(format!("server/{port}"));
        std::fs::create_dir_all(&token_dir).unwrap();
        std::fs::write(token_dir.join("owner.token"), Self::TOKEN).unwrap();
        Self {
            _lock: lock,
            listener,
        }
    }

    async fn next(&self) -> (tokio::net::TcpStream, String, String, Value) {
        use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};

        let (stream, _) = tokio::time::timeout(Duration::from_secs(10), self.listener.accept())
            .await
            .unwrap()
            .unwrap();
        let mut reader = BufReader::new(stream);
        let mut request = String::new();
        reader.read_line(&mut request).await.unwrap();
        let mut headers = String::new();
        loop {
            let mut line = String::new();
            assert!(reader.read_line(&mut line).await.unwrap() > 0);
            if line == "\r\n" {
                break;
            }
            headers.push_str(&line);
        }
        let len = headers
            .lines()
            .find_map(|line| line.strip_prefix("content-length: "))
            .map_or(0, |value| value.parse().unwrap());
        let mut body = vec![0; len];
        reader.read_exact(&mut body).await.unwrap();
        let body = if len == 0 {
            Value::Null
        } else {
            serde_json::from_slice(&body).unwrap()
        };
        (reader.into_inner(), request, headers, body)
    }

    async fn prove(&self) {
        let (socket, request, headers, body) = self.next().await;
        assert!(request.starts_with("POST /-/guildbotics/api/local-api/proof "));
        assert!(!headers.contains("authorization:"));
        assert_eq!(body["token_id"], "0123456789abcdef");
        let nonce = body["nonce"].as_str().unwrap();
        let message = format!("guildbotics-local-api-proof\n{nonce}\n0123456789abcdef");
        let digest = loopback_auth::digest("fixture-token");
        let proof = loopback_auth::hex(&loopback_auth::hmac_sha256(
            digest.as_bytes(),
            message.as_bytes(),
        ));
        Self::respond(socket, "200 OK", &json!({"proof": proof}).to_string()).await;
    }

    async fn respond(mut socket: tokio::net::TcpStream, status: &str, body: &str) {
        use tokio::io::AsyncWriteExt;
        socket.write_all(format!(
            "HTTP/1.1 {status}\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
            body.len()
        ).as_bytes()).await.unwrap();
    }

    async fn authorized(&self, method: &str, path: &str) -> (tokio::net::TcpStream, Value) {
        let (socket, request, headers, body) = self.next().await;
        assert!(
            request.starts_with(&format!("{method} /-/guildbotics/api/{path} ")),
            "{request}"
        );
        assert!(headers.contains(&format!("authorization: Bearer {}\r\n", Self::TOKEN)));
        (socket, body)
    }

    async fn expect_no_retry(&self) {
        assert!(
            tokio::time::timeout(Duration::from_millis(100), self.listener.accept())
                .await
                .is_err()
        );
    }
}

fn peer_cli(root: &Path, arguments: &[&str]) -> tokio::process::Command {
    let mut cli = tokio::process::Command::new(BIN);
    cli.args(arguments)
        .kill_on_drop(true)
        .current_dir(root)
        .env("HOME", root)
        .env("GUILDBOTICS_HOME", root.join("home"))
        .env("GUILDBOTICS_SERVER_PORT", "1")
        .env("LANGUAGE", "ja")
        .env_remove("GUILDBOTICS_WORKSPACE_ROOT")
        .env_remove("GUILDBOTICS_CONFIG_DIR")
        .stdin(Stdio::null());
    cli
}

#[tokio::test]
async fn service_cli_preserves_server_errors_and_never_retries_a_post() {
    for (args, path, payload) in [
        (&["server", "stop"][..], "shutdown", json!({"force": false})),
        (
            &["server", "stop", "--force"][..],
            "shutdown",
            json!({"force": true}),
        ),
        (&["service", "start"][..], "scheduler/start", Value::Null),
        (
            &["service", "stop"][..],
            "scheduler/stop",
            json!({"force": false}),
        ),
    ] {
        let root = tempfile::tempdir().unwrap();
        let peer = CliPeer::bind(root.path()).await;
        for (status, body, expected) in [
            (
                "409 Conflict",
                Some(r#"{"message":"サーバーは終了中です。"}"#),
                "サーバーは終了中です。",
            ),
            (
                "503 Service Unavailable",
                Some(r#"{"message":"サーバーは終了中です。"}"#),
                "サーバーは終了中です。",
            ),
            ("409 Conflict", Some("{}"), "HTTP 409"),
            ("409 Conflict", Some("[]"), "HTTP 409"),
            ("409 Conflict", Some(r#"{"message":null}"#), "HTTP 409"),
            ("409 Conflict", Some(r#"{"message":""}"#), "HTTP 409"),
            ("409 Conflict", Some("temporarily unavailable"), "HTTP 409"),
            ("", None, "error sending request"),
        ] {
            let mut cli = peer_cli(root.path(), args);
            let output = tokio::spawn(async move { cli.output().await.unwrap() });
            peer.prove().await;
            let (socket, request) = peer.authorized("POST", path).await;
            assert_eq!(request, payload);
            if let Some(body) = body {
                CliPeer::respond(socket, status, body).await;
            } else {
                drop(socket);
            }
            let output = tokio::time::timeout(Duration::from_secs(10), output)
                .await
                .unwrap()
                .unwrap();
            assert_eq!(output.status.code(), Some(1));
            assert!(output.stdout.is_empty());
            let stderr = String::from_utf8(output.stderr).unwrap();
            assert!(stderr.contains(expected), "{args:?} {stderr}");
            peer.expect_no_retry().await;
        }
    }
}

#[tokio::test]
async fn run_cli_forwards_the_workspace_cwd_person_arguments_and_stdin_only_once() {
    use tokio::io::AsyncWriteExt;

    let dir = tempfile::tempdir().unwrap();
    let root = gb_base::host_path::normalize(dir.path()).unwrap();
    let workspace = peer_workspace(&root);
    let peer = CliPeer::bind(&root).await;
    let caller = root.join("caller");
    let working = caller.join("work");
    std::fs::create_dir_all(&working).unwrap();
    for (arguments, person, cwd, status, reply, expected_stdout, expected_error) in [
        (
            vec!["run", "ask@alice", "--cwd", "work", "topic=review", "value"],
            json!("alice"),
            working.as_path(),
            "200 OK",
            Some(r#"{"output":"review output"}"#),
            "review output\n",
            "",
        ),
        (
            vec![
                "run",
                "ask@bob",
                "--person",
                "alice",
                "--cwd",
                "work",
                "topic=review",
                "value",
            ],
            json!("alice"),
            working.as_path(),
            "200 OK",
            Some(r#"{"output":"review output"}"#),
            "review output\n",
            "",
        ),
        (
            vec!["run", " ask ", "topic=review", "value"],
            Value::Null,
            caller.as_path(),
            "200 OK",
            Some(r#"{"output":""}"#),
            "",
            "",
        ),
        (
            vec!["run", "ask", "topic=review", "value"],
            Value::Null,
            caller.as_path(),
            "409 Conflict",
            Some(r#"{"code":"command_already_running","message":"実行中のため開始できません"}"#),
            "",
            "実行中のため開始できません",
        ),
        (
            vec!["run", "ask", "topic=review", "value"],
            Value::Null,
            caller.as_path(),
            "409 Conflict",
            Some(r#"{"code":"work_rejected","message":"実行中のため開始できません"}"#),
            "",
            "実行中のため開始できません",
        ),
        (
            vec!["run", "ask", "topic=review", "value"],
            Value::Null,
            caller.as_path(),
            "500 Internal Server Error",
            Some("Internal Server Error"),
            "",
            "HTTP 500 Internal Server Error",
        ),
        (
            vec!["run", "ask", "topic=review", "value"],
            Value::Null,
            caller.as_path(),
            "",
            None,
            "",
            "",
        ),
    ] {
        let read_only = person.is_null();
        std::fs::write(cwd.join("input.txt"), "original").unwrap();
        std::fs::write(cwd.join("remove.txt"), "before").unwrap();
        let mut child = peer_cli(&root, &arguments)
            .current_dir(&caller)
            .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let message = "日本語\nreview";
        child
            .stdin
            .take()
            .unwrap()
            .write_all(message.as_bytes())
            .await
            .unwrap();
        peer.prove().await;
        let (socket, _) = peer.authorized("GET", "health").await;
        CliPeer::respond(
            socket,
            "200 OK",
            &json!({"workspace_id":"019691c2-8dc0-7654-8000-000000000042", "service_instance_id":"fixture", "command_time":100000}).to_string(),
        )
        .await;
        let (socket, preflight) = peer.authorized("POST", "commands/run").await;
        assert_eq!(
            preflight,
            json!({
                "preflight":true,"client_copy":false,"upload":null,
                "command":"ask","args":["topic=review","value"],"person":person,
                "message":message,"cwd":cwd,"expected_workspace":"019691c2-8dc0-7654-8000-000000000042","request_id":null,
            })
        );
        let resolved_person = person.as_str().unwrap_or("default-member");
        CliPeer::respond(
            socket,
            "200 OK",
            &json!({"output":json!({"read_only":read_only,"person":resolved_person}).to_string()})
                .to_string(),
        )
        .await;
        let (socket, request) = peer.authorized("POST", "commands/run").await;
        let id = request["request_id"].as_str().unwrap();
        assert!(id.starts_with("fixture.100000."));
        assert_eq!(request["preflight"], false);
        assert_eq!(request["client_copy"], true);
        assert_eq!(request["command"], "ask");
        assert_eq!(request["args"], json!(["topic=review", "value"]));
        assert_eq!(request["person"], resolved_person);
        assert_eq!(request["message"], message);
        assert_eq!(request["cwd"], json!(cwd));
        assert_eq!(
            request["expected_workspace"],
            "019691c2-8dc0-7654-8000-000000000042"
        );
        if read_only {
            assert!(request["upload"].is_null());
        } else {
            let files = request["upload"]["files"].as_array().unwrap();
            assert_eq!(files.len(), 2);
            assert!(
                files
                    .iter()
                    .any(|f| f["path"] == "input.txt" && f["content"] == "b3JpZ2luYWw=")
            );
        }
        if reply.is_none() || status == "200 OK" {
            if reply.is_none() {
                drop(socket);
            } else {
                CliPeer::respond(socket, "200 OK", r#"{"state":"running"}"#).await;
            }
            let path = format!("commands/runs/{id}");
            let (socket, _) = peer.authorized("GET", &path).await;
            CliPeer::respond(socket, "200 OK", r#"{"state":"running"}"#).await;
            let (socket, _) = peer.authorized("GET", &path).await;
            let mut result: Value =
                serde_json::from_str(reply.unwrap_or(r#"{"output":"recovered"}"#)).unwrap();
            if !read_only {
                result["worktree_changes"] = json!(
                    [
                        json!({"path":"input.txt","content":"ZWRpdGVk"}).to_string(),
                        json!({"path":"remove.txt","deleted":true}).to_string(),
                        json!({"path":"new.txt","content":"Y3JlYXRlZA=="}).to_string(),
                    ]
                    .join("\n")
                );
            }
            CliPeer::respond(
                socket,
                "200 OK",
                &json!({"state":"finished", "status":200,"result":result}).to_string(),
            )
            .await;
        } else if let Some(reply) = reply {
            CliPeer::respond(socket, status, reply).await;
        }
        let output = tokio::time::timeout(Duration::from_secs(10), child.wait_with_output())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            output.status.success(),
            expected_error.is_empty(),
            "{output:?}"
        );
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            if reply.is_none() {
                "recovered\n"
            } else {
                expected_stdout
            }
        );
        let stderr = String::from_utf8(output.stderr).unwrap();
        if expected_error.is_empty() {
            assert!(stderr.contains(id), "{stderr}");
        } else {
            assert!(stderr.contains(expected_error), "{stderr}");
            assert!(!stderr.contains("command_not_found"), "{stderr}");
        }
        if !read_only && expected_error.is_empty() {
            assert_eq!(
                std::fs::read_to_string(cwd.join("input.txt")).unwrap(),
                "edited"
            );
            assert!(!cwd.join("remove.txt").exists());
            assert_eq!(
                std::fs::read_to_string(cwd.join("new.txt")).unwrap(),
                "created"
            );
            std::fs::remove_file(cwd.join("new.txt")).unwrap();
        } else {
            assert_eq!(
                std::fs::read_to_string(cwd.join("input.txt")).unwrap(),
                "original"
            );
            assert!(cwd.join("remove.txt").exists());
        }
        peer.expect_no_retry().await;
    }
}

#[tokio::test]
async fn member_cli_renders_backend_plan_results_without_executing() {
    let dir = tempfile::tempdir().unwrap();
    let root = gb_base::host_path::normalize(dir.path()).unwrap();
    let workspace = peer_workspace(&root);
    let peer = CliPeer::bind(&root).await;
    for (args, code, stdout, stderr) in [
        (
            &["member", "pr", "read", "--help"][..],
            0,
            "backend help\n",
            "",
        ),
        (
            &["member", "not-a-command"][..],
            2,
            "",
            "backend usage error\n",
        ),
    ] {
        let mut cli = peer_cli(&root, args);
        cli.env("GUILDBOTICS_WORKSPACE_ROOT", &workspace);
        let child = tokio::spawn(async move { cli.output().await.unwrap() });
        peer.prove().await;
        let (socket, request) = peer.authorized("POST", "member/plan").await;
        assert_eq!(request["arguments"], json!(&args[1..]));
        assert_eq!(request["stdin"], "");
        let reply = member_plan(json!({
            "result": {"exit_code": code, "stdout": stdout, "stderr": stderr},
        }));
        CliPeer::respond(socket, "200 OK", &reply).await;
        let output = tokio::time::timeout(Duration::from_secs(10), child)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(output.status.code(), Some(code));
        assert_eq!(output.stdout, stdout.as_bytes());
        assert_eq!(output.stderr, stderr.as_bytes());
        peer.expect_no_retry().await;
    }
}

#[tokio::test]
async fn member_attachment_resolves_a_relative_parent_on_the_caller() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let workspace = peer_workspace(&root);
    let sub = root.join("sub");
    std::fs::create_dir(&sub).unwrap();
    let peer = CliPeer::bind(&root).await;
    let args = [
        "member",
        "ticket",
        "attachment",
        "download",
        "--dest",
        "../images",
    ];
    let mut cli = peer_cli(&root, &args);
    cli.current_dir(sub)
        .env("GUILDBOTICS_WORKSPACE_ROOT", workspace);
    let child = tokio::spawn(async move { cli.output().await.unwrap() });
    peer.prove().await;
    let (socket, request) = peer.authorized("POST", "member/plan").await;
    assert_eq!(request["arguments"], json!(&args[1..]));
    let plan = member_plan(json!({"download":true,"arguments":&args[1..],"output_format":"json"}));
    CliPeer::respond(socket, "200 OK", &plan).await;
    let (socket, _) = peer.authorized("POST", "member/run").await;
    assert!(!root.join("images").exists());
    let reply = json!({"exit_code":0,"stdout":"{}","stderr":"","size":3,
        "download":{"destination":"../images","attachment":["fixture.png","id"]}});
    CliPeer::respond(socket, "200 OK", &format!("{reply}\npng")).await;
    let output = tokio::time::timeout(Duration::from_secs(10), child)
        .await
        .unwrap()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let expected = root.join("images/fixture.png");
    let receipt: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(receipt["files"], json!([expected]));
    assert_eq!(std::fs::read(expected).unwrap(), b"png");
}

fn member_plan(plan: Value) -> String {
    format!(
        "{}\n",
        json!({"exit_code": 0, "stdout": "", "stderr": "", "plan":plan})
    )
}

#[tokio::test]
async fn member_cli_reads_files_and_stdin_locally_redacts_memory_and_never_retries_run() {
    use tokio::io::AsyncWriteExt;

    let dir = tempfile::tempdir().unwrap();
    let root = gb_base::host_path::normalize(dir.path()).unwrap();
    let workspace = peer_workspace(&root);
    std::fs::write(
        workspace.join(".guildbotics/config/secrets.yml"),
        "keys:\n- DATABASE_URL\n",
    )
    .unwrap();
    let peer = CliPeer::bind(&root).await;
    let body = "調査結果\nUTF-8 の本文です。\n";
    std::fs::write(root.join("body.txt"), body).unwrap();
    std::fs::write(root.join("invalid.txt"), [0xff]).unwrap();
    std::fs::create_dir(root.join("body-directory")).unwrap();
    for (file, memory, disconnect) in [
        (Some("body.txt"), false, false),
        (None, false, false),
        (None, true, false),
        (None, false, true),
        (Some("missing.txt"), false, false),
        (Some("invalid.txt"), false, false),
        (Some("body-directory"), false, false),
    ] {
        let mut arguments = if memory {
            vec![
                "member",
                "memory",
                "record",
                "--person",
                "yuki",
                "--title",
                "local-dummy-value",
            ]
        } else {
            vec![
                "member",
                "ticket",
                "comment",
                "--person",
                "yuki",
                "--url",
                "local://tickets/demo/T-1",
            ]
        };
        let original = arguments.len() - 1;
        match file {
            Some(path) => arguments.extend(["--content-file", path]),
            None => arguments.push("--content-stdin"),
        }
        let mut child = peer_cli(&root, &arguments)
            .env("LANGUAGE", "en")
            .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace)
            .env("DATABASE_URL", "local-dummy-value")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let input = if memory {
            "body local-dummy-value"
        } else {
            body
        };
        child
            .stdin
            .take()
            .unwrap()
            .write_all(input.as_bytes())
            .await
            .unwrap();
        peer.prove().await;
        let (socket, request) = peer.authorized("POST", "member/plan").await;
        assert_eq!(request["stdin"], "");
        assert!(!request.to_string().contains("local-dummy-value"));
        let mut forwarded = request["arguments"].as_array().unwrap()[..original].to_vec();
        forwarded.push("--content-stdin".into());
        let file_source = file.map(|_| json!({"index": original + 1, "offset": 0}));
        let reply = member_plan(json!({
            "arguments": forwarded,
            "stdin": file.is_none(),
            "file_source": file_source,
            "content_file": file,
            "memory": memory,
        }));
        CliPeer::respond(socket, "200 OK", &reply).await;
        let valid = matches!(file, None | Some("body.txt"));
        if valid {
            let (socket, request) = peer.authorized("POST", "member/run").await;
            assert_eq!(request["arguments"], json!(forwarded));
            assert_eq!(request["stdin"], if memory { "body ***" } else { body });
            assert!(!request.to_string().contains("local-dummy-value"));
            if disconnect {
                drop(socket);
            } else {
                let reply =
                    json!({"exit_code": 0, "stdout": "server output\n", "stderr": "server note\n"});
                CliPeer::respond(socket, "200 OK", &format!("{reply}\n")).await;
            }
        }
        let output = tokio::time::timeout(Duration::from_secs(10), child.wait_with_output())
            .await
            .unwrap()
            .unwrap();
        if valid && !disconnect {
            assert_eq!(output.status.code(), Some(0));
            assert_eq!(output.stdout, b"server output\n");
            assert_eq!(output.stderr, b"server note\n");
        } else {
            assert_eq!(output.status.code(), Some(1));
            assert!(output.stdout.is_empty());
            let error = String::from_utf8(output.stderr).unwrap();
            if let Some(path) = file {
                assert!(error.contains(path), "{error}");
                let expected = if path == "invalid.txt" {
                    "UTF-8"
                } else {
                    "read"
                };
                assert!(error.contains(expected), "{error}");
            } else {
                assert!(error.contains("error sending request"), "{error}");
            }
        }
        peer.expect_no_retry().await;
    }
}

#[test]
fn startup_rejects_the_old_commands_and_service_configuration_flags() {
    for args in [
        &["start"][..],
        &["stop"][..],
        &["kill"][..],
        &["server", "start", "--only=events"][..],
        &["server", "start", "--max-consecutive-errors=3"][..],
    ] {
        let output = Command::new(BIN).args(args).output().unwrap();
        assert_eq!(output.status.code(), Some(2), "{args:?} {output:?}");
    }
}

#[tokio::test]
async fn hub_probe_names_the_source_tree_the_executable_was_built_from() {
    let dir = tempfile::tempdir().unwrap();
    let output = peer_cli(dir.path(), &["server", "hub-probe", "--source"])
        .output()
        .await
        .unwrap();
    assert!(output.status.success());
    let checkout = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    assert_eq!(
        Path::new(String::from_utf8(output.stdout).unwrap().trim_end())
            .canonicalize()
            .unwrap(),
        checkout.canonicalize().unwrap()
    );
}

#[tokio::test]
async fn hub_probe_requires_the_lock_and_authenticates_readiness() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    for ready in [false, true] {
        let mut args = vec!["server", "hub-probe"];
        if ready {
            args.push("--ready");
        }
        let output = peer_cli(root, &args).output().await.unwrap();
        assert_eq!(output.status.code(), Some(i32::from(ready)));
        assert!(output.stdout.is_empty());
    }

    let peer = CliPeer::bind(root).await;
    let pid = format!("{}\n", std::process::id());
    let holder = peer_cli(root, &["server", "hub-probe"])
        .output()
        .await
        .unwrap();
    assert!(holder.status.success());
    assert_eq!(holder.stdout, pid.as_bytes());
    peer.expect_no_retry().await;

    let mut probe = peer_cli(root, &["server", "hub-probe", "--ready"]);
    let task = tokio::spawn(async move { probe.output().await.unwrap() });
    let (socket, path, headers, _) = peer.next().await;
    assert!(path.starts_with("POST /-/guildbotics/api/local-api/proof "));
    assert!(!headers.contains("authorization:"));
    CliPeer::respond(socket, "200 OK", r#"{"proof":"incorrect"}"#).await;
    let refused = task.await.unwrap();
    assert_eq!(refused.status.code(), Some(1));
    assert!(refused.stdout.is_empty());

    let mut probe = peer_cli(root, &["server", "hub-probe", "--ready"]);
    let task = tokio::spawn(async move { probe.output().await.unwrap() });
    peer.prove().await;
    let ready = task.await.unwrap();
    assert!(ready.status.success());
    assert_eq!(ready.stdout, pid.as_bytes());
}

#[tokio::test]
async fn server_stop_waits_for_the_lock_and_authenticates_before_requesting_force() {
    use tokio::io::{AsyncBufReadExt, AsyncReadExt, BufReader};

    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    for args in [
        &["server", "stop", "--force"][..],
        &["service", "start"],
        &["service", "stop"],
    ] {
        let peer = CliPeer::bind(root).await;
        let mut cli = peer_cli(root, args);
        cli.env("LANGUAGE", "en");
        let stop = tokio::spawn(async move { cli.output().await.unwrap() });
        let (socket, path, headers, _) = peer.next().await;
        assert!(path.starts_with("POST /-/guildbotics/api/local-api/proof "));
        assert!(!headers.contains("authorization:"));
        CliPeer::respond(socket, "200 OK", r#"{"proof":"incorrect"}"#).await;
        let refused = tokio::time::timeout(Duration::from_secs(10), stop)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(refused.status.code(), Some(1));
        assert!(
            String::from_utf8_lossy(&refused.stderr).contains(&gb_server::i18n::t(
                "en",
                "runtime.service_lock.api_unavailable",
                &[]
            ))
        );
        peer.expect_no_retry().await;
    }

    for force in [false, true] {
        let peer = CliPeer::bind(root).await;
        let mut args = vec!["server", "stop"];
        if force {
            args.push("--force");
        }
        let mut cli = peer_cli(root, &args);
        cli.env("LANGUAGE", "en");
        let mut stop = cli
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut reader = BufReader::new(stop.stdout.take().unwrap());
        let mut stdout = String::new();
        peer.prove().await;
        let (socket, body) = peer.authorized("POST", "shutdown").await;
        assert_eq!(body, json!({"force":force}));
        CliPeer::respond(socket, "200 OK", "{}").await;
        if force {
            assert!(
                tokio::time::timeout(Duration::from_millis(250), stop.wait())
                    .await
                    .is_err()
            );
        } else {
            let waiting = gb_server::i18n::t("en", "runtime.service_lock.waiting_for_work", &[]);
            tokio::time::timeout(Duration::from_secs(10), async {
                while !stdout.contains(&waiting) {
                    assert!(reader.read_line(&mut stdout).await.unwrap() > 0, "{stdout}");
                }
            })
            .await
            .expect("server stop must report waiting while the peer holds the lock");
            assert!(stop.try_wait().unwrap().is_none());
        }
        drop(peer);
        let result = tokio::time::timeout(Duration::from_secs(5), async {
            reader.read_to_string(&mut stdout).await.unwrap();
            stop.wait_with_output().await.unwrap()
        })
        .await
        .unwrap();
        assert!(result.status.success(), "{result:?}");
        for key in if force {
            &["runtime.service_lock.stopped_after_cancel"][..]
        } else {
            &[
                "runtime.service_lock.waiting_for_work",
                "runtime.service_lock.stopped",
            ][..]
        } {
            assert!(
                stdout.contains(&gb_server::i18n::t("en", key, &[])),
                "{stdout}"
            );
        }
    }
}

#[tokio::test]
async fn run_requires_a_server_before_waiting_for_stdin() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let workspace = peer_workspace(&root);
    let mut cli = peer_cli(&root, &["run", "ask"]);
    let mut child = cli
        .current_dir(workspace)
        .env("LANGUAGE", "en")
        .stdin(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let input = child.stdin.take().unwrap();
    let output = tokio::time::timeout(Duration::from_secs(5), child.wait_with_output())
        .await
        .expect("workspace failure must not wait for EOF")
        .unwrap();
    drop(input);
    assert_eq!(output.status.code(), Some(1));
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("requires a running GuildBotics server"),
        "{output:?}"
    );
    assert!(!root.join("home/data/workspaces.json").exists());
}

#[tokio::test]
async fn run_cli_never_falls_back_when_the_peer_cannot_confirm_the_workspace() {
    let dir = tempfile::tempdir().unwrap();
    let root = gb_base::host_path::normalize(dir.path()).unwrap();
    let workspace = peer_workspace(&root);
    gb_base::files::atomic_write(
        &workspace.join(".guildbotics/config/team/project.yml"),
        b"name: CLI test\nlanguage: en\n",
    )
    .unwrap();
    let peer = CliPeer::bind(&root).await;
    let port = peer.listener.local_addr().unwrap().port();
    for state in [
        "other-workspace",
        "health-error",
        "malformed",
        "disconnect",
        "proof-failed",
        "missing-token",
    ] {
        if state == "missing-token" {
            std::fs::remove_file(root.join(format!("home/data/server/{port}/owner.token")))
                .unwrap();
        }
        let mut cli = peer_cli(&root, &["run", "does-not-exist@no-such-member"]);
        cli.env("GUILDBOTICS_WORKSPACE_ROOT", &workspace)
            .env("LANGUAGE", "en");
        let running = tokio::spawn(async move { cli.output().await.unwrap() });
        if state == "proof-failed" {
            let (socket, request, headers, _) = peer.next().await;
            assert!(request.starts_with("POST /-/guildbotics/api/local-api/proof "));
            assert!(!headers.contains("authorization:"));
            CliPeer::respond(socket, "200 OK", r#"{"proof":"incorrect"}"#).await;
        } else if state != "missing-token" {
            peer.prove().await;
            let (socket, _) = peer.authorized("GET", "health").await;
            match state {
                "other-workspace" => {
                    CliPeer::respond(socket, "200 OK", r#"{"workspace":"other"}"#).await
                }
                "health-error" => CliPeer::respond(socket, "401 Unauthorized", "{}").await,
                "malformed" => CliPeer::respond(socket, "200 OK", "[]").await,
                "disconnect" => drop(socket),
                _ => unreachable!(),
            }
        }
        let output = tokio::time::timeout(Duration::from_secs(10), running)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(
            stderr.contains("requires a running")
                || stderr.contains("different workspace selected"),
            "{state}: {stderr}"
        );
        if state == "other-workspace" || state == "malformed" {
            assert!(stderr.contains(&gb_server::i18n::t("en", "cli.run.workspace_mismatch", &[])));
        }
        peer.expect_no_retry().await;
    }
}

#[tokio::test]
async fn service_commands_request_the_same_endpoints_and_payloads_as_the_screen() {
    let root = tempfile::tempdir().unwrap();
    let peer = CliPeer::bind(root.path()).await;
    for (args, path, payload, key) in [
        (
            &["service", "start"][..],
            "scheduler/start",
            Value::Null,
            "runtime.service_lock.services_started",
        ),
        (
            &["service", "stop"][..],
            "scheduler/stop",
            json!({"force":false}),
            "runtime.service_lock.services_stopped",
        ),
        (
            &["service", "stop", "--force"][..],
            "scheduler/stop",
            json!({"force":true}),
            "runtime.service_lock.services_stopped",
        ),
    ] {
        let mut cli = peer_cli(root.path(), args);
        cli.env("LANGUAGE", "en");
        let output = tokio::spawn(async move { cli.output().await.unwrap() });
        peer.prove().await;
        let (socket, body) = peer.authorized("POST", path).await;
        assert_eq!(body, payload);
        CliPeer::respond(socket, "200 OK", "{}").await;
        let output = tokio::time::timeout(Duration::from_secs(10), output)
            .await
            .unwrap()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        assert_eq!(
            String::from_utf8(output.stdout).unwrap(),
            format!("{}\n", gb_server::i18n::t("en", key, &[]))
        );
        assert!(output.stderr.is_empty());
        peer.expect_no_retry().await;
    }
}

#[cfg(unix)]
#[tokio::test]
async fn workspace_and_member_refuse_a_directory_the_user_cannot_list() {
    use std::os::unix::fs::PermissionsExt;

    let dir = tempfile::tempdir().unwrap();
    let root = gb_base::host_path::normalize(dir.path()).unwrap();
    let protected = root.join("Documents/protected");
    std::fs::create_dir_all(&protected).unwrap();
    std::fs::set_permissions(&protected, std::fs::Permissions::from_mode(0o300)).unwrap();
    if std::fs::read_dir(&protected).is_ok() {
        // An elevated test process can list this directory regardless of its mode.
        return;
    }
    let path = protected.to_str().unwrap();
    let peer = CliPeer::bind(&root).await;
    let use_workspace = peer_cli(&root, &["workspace", "use", path])
        .env("LANGUAGE", "en")
        .output()
        .await
        .unwrap();
    let mut cli = peer_cli(&root, &["member", "--workspace", path, "help"]);
    cli.env("LANGUAGE", "en");
    let child = tokio::spawn(async move { cli.output().await.unwrap() });
    peer.prove().await;
    let member = tokio::time::timeout(Duration::from_secs(10), child)
        .await
        .unwrap()
        .unwrap();
    std::fs::set_permissions(&protected, std::fs::Permissions::from_mode(0o700)).unwrap();
    for output in [use_workspace, member] {
        assert_eq!(output.status.code(), Some(1));
        let stderr = String::from_utf8(output.stderr).unwrap();
        let expected = if cfg!(target_os = "macos") {
            "Privacy & Security > Files & Folders"
        } else {
            "cannot list"
        };
        assert!(
            stderr.contains(path) && stderr.contains(expected),
            "{stderr}"
        );
        assert!(!stderr.contains("Traceback"));
    }
    peer.expect_no_retry().await;
    assert!(!root.join("home/data/workspaces.json").exists());
}

#[test]
fn run_without_a_server_never_loads_provider_configuration_or_starts_work() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let workspace = peer_workspace(root);
    gb_base::files::atomic_write(
        &workspace.join(".guildbotics/config/team/project.yml"),
        b"invalid: [",
    )
    .unwrap();
    for language in ["en", "ja"] {
        let output = Command::new(BIN)
            .args(["run", "solo@alice"])
            .env("GUILDBOTICS_HOME", root.join("home"))
            .env("GUILDBOTICS_WORKSPACE_ROOT", &workspace)
            .env("LANGUAGE", language)
            .stdin(Stdio::null())
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(
            String::from_utf8_lossy(&output.stderr).contains(&gb_server::i18n::t(
                language,
                "cli.run.backend_required",
                &[]
            ))
        );
        assert!(!workspace.join(".guildbotics/state/task-runs").exists());
        assert!(!root.join("home/data/msb").exists());
    }
}

#[tokio::test]
async fn resume_only_reads_the_receipt_without_workspace_or_stdin() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let peer = CliPeer::bind(root).await;
    let mut child = peer_cli(root, &["run", "--resume", "fixture.100000.id"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let input = child.stdin.take().unwrap();
    peer.prove().await;
    let (socket, _) = peer
        .authorized("GET", "commands/runs/fixture.100000.id")
        .await;
    CliPeer::respond(
        socket,
        "200 OK",
        r#"{"state":"finished","status":200,"result":{"output":"recovered"}}"#,
    )
    .await;
    let output = tokio::time::timeout(Duration::from_secs(5), child.wait_with_output())
        .await
        .unwrap()
        .unwrap();
    drop(input);
    assert!(output.status.success(), "{output:?}");
    assert_eq!(output.stdout, b"recovered\n");
    peer.expect_no_retry().await;
}

#[tokio::test]
async fn invalid_resume_id_has_a_localized_input_error() {
    for language in ["en", "ja"] {
        let dir = tempfile::tempdir().unwrap();
        let peer = CliPeer::bind(dir.path()).await;
        let mut cli = peer_cli(dir.path(), &["run", "--resume", "bad/id"]);
        cli.env("LANGUAGE", language);
        let running = tokio::spawn(async move { cli.output().await.unwrap() });
        peer.prove().await;
        let output = running.await.unwrap();
        assert_eq!(output.status.code(), Some(1));
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(stderr.contains(&gb_server::i18n::t(language, "cli.run.invalid_id", &[])));
        assert!(!stderr.contains(&gb_server::i18n::t(language, "cli.run.invalid_output", &[])));
        peer.expect_no_retry().await;
    }
}
