//! The `branvil` command line against a running service (with the owner's
//! token from `--token`, `BRANVIL_TOKEN`, or the data directory's token
//! file), the offline admin commands against a stopped one, and how `serve`
//! stops.

use crate::common;

use common::*;

fn text(out: &std::process::Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

#[tokio::test(flavor = "multi_thread")]
async fn admin_notify_needs_neither_a_running_service_nor_a_readable_database() {
    use axum::{Router, routing::post};
    use std::sync::{Arc, Mutex};
    let received = Arc::new(Mutex::new(Vec::new()));
    let copy = received.clone();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/secret-hook", listener.local_addr().unwrap());
    let app = Router::new().route(
        "/secret-hook",
        post(move |axum::Json(v): axum::Json<serde_json::Value>| {
            let copy = copy.clone();
            async move {
                copy.lock().unwrap().push(v);
                "ok"
            }
        }),
    );
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path();
    std::fs::create_dir(data.join("secrets")).unwrap();
    let notify = || {
        std::process::Command::new(BIN)
            .args(["admin", "notify", "test notice", "--data-dir"])
            .arg(data)
            .output()
            .unwrap()
    };
    assert!(
        notify().status.success(),
        "unset is a no-op, even without a database"
    );
    assert!(received.lock().unwrap().is_empty());
    std::fs::write(data.join("secrets/webhook.url"), &url).unwrap();
    std::fs::write(data.join("branvil.redb"), "not a database").unwrap();
    std::fs::write(
        data.join("branvil.toml"),
        "not valid TOML or service configuration",
    )
    .unwrap();
    let out = notify();
    assert!(out.status.success(), "{}", text(&out));
    assert_eq!(
        *received.lock().unwrap(),
        vec![serde_json::json!({"text": "test notice"})]
    );
    assert!(!text(&out).contains("secret-hook"));
    server.abort();
    let out = notify();
    assert!(!out.status.success());
    assert!(text(&out).contains("could not connect to the webhook"));
    assert!(!text(&out).contains(&url));
    assert!(!text(&out).contains("secret-hook"));
    std::fs::write(
        data.join("secrets/webhook.url"),
        "http://secret-hook:bad/path",
    )
    .unwrap();
    assert!(!text(&notify()).contains("secret-hook"));
}

#[tokio::test]
async fn bootstrap_and_admin_commands() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("data");
    let out = std::process::Command::new(BIN)
        .args([
            "init",
            "--owner",
            "alice",
            "--listen",
            "127.0.0.1:0",
            "--data-dir",
        ])
        .arg(&data)
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", text(&out));
    // init names the token file and never prints a token.
    let owner = owner_token(&data);
    assert!(owner.starts_with("bvt_"));
    assert!(text(&out).contains("secrets/owner.token"), "{}", text(&out));
    assert!(!text(&out).contains("bvt_"), "{}", text(&out));
    // init refuses to run twice.
    let again = std::process::Command::new(BIN)
        .args(["init", "--listen", "127.0.0.1:0", "--data-dir"])
        .arg(dir.path().join("data"))
        .output()
        .unwrap();
    assert!(!again.status.success());

    let mut server = Server::start(dir.path());
    for args in [
        vec!["workspace", "create", "acme"],
        vec!["repo", "create", "acme/demo", "--label", "bug"],
        vec!["member", "create", "aiko", "--kind", "agent"],
    ] {
        let out = server.cli(&owner, &args);
        assert!(out.status.success(), "{args:?}: {}", text(&out));
    }
    // Options also work after the subcommand.
    let out = std::process::Command::new(BIN)
        .args([
            "member",
            "token",
            "aiko",
            "--url",
            &server.url,
            "--token",
            &owner,
        ])
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", text(&out));
    let aiko = String::from_utf8_lossy(&out.stdout).trim().to_string();
    assert!(aiko.starts_with("bvt_"));
    let me = server.ok("/me", &aiko).await;
    assert_eq!(me["name"], "aiko");
    // An agent cannot create members; the error is reported.
    let out = server.cli(&aiko, &["member", "create", "x"]);
    assert!(!out.status.success());
    assert!(text(&out).contains("403"), "{}", text(&out));
    // The repository holds the labels its tickets may carry.
    let repo = server.ok("/repos/acme%2Fdemo", &aiko).await;
    assert_eq!(repo["labels"], serde_json::json!(["bug"]));

    // On the Hub machine the owner's token file is the default token.
    let on_hub = |args: &[&str]| {
        std::process::Command::new(BIN)
            .args(args)
            .env_remove("BRANVIL_TOKEN")
            .env("BRANVIL_URL", &server.url)
            .env("BRANVIL_DATA_DIR", &data)
            .output()
            .unwrap()
    };
    let out = on_hub(&["workspace", "create", "beta"]);
    assert!(out.status.success(), "{}", text(&out));
    let out = on_hub(&["runner", "register", "runner9"]);
    assert!(out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("bvt_"), "{}", text(&out));
    // `branvil open --print` prints a single-use entry link.
    let out = on_hub(&["open", "--print"]);
    assert!(out.status.success(), "{}", text(&out));
    let link = String::from_utf8_lossy(&out.stdout).trim().to_string();
    let code = link
        .strip_prefix(&format!("{}/-/enter#", server.url))
        .unwrap_or_else(|| panic!("{link}"));
    assert_eq!(server.enter(code).await.status(), 200);
    assert_eq!(server.enter(code).await.status(), 401);
    // The runner itself never falls back to the owner's token.
    let out = on_hub(&[
        "runner",
        "--work-dir",
        dir.path().join("w").to_str().unwrap(),
    ]);
    assert!(!out.status.success());
    assert!(
        text(&out).contains("runner token is needed"),
        "{}",
        text(&out)
    );

    // Offline commands refuse a running service, then work once it stops.
    let out = std::process::Command::new(BIN)
        .args(["admin", "check", "--data-dir"])
        .arg(&server.data)
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(text(&out).contains("already open"), "{}", text(&out));
    server.stop();
    let out = std::process::Command::new(BIN)
        .args(["admin", "reindex", "--data-dir"])
        .arg(&server.data)
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", text(&out));
    let out = std::process::Command::new(BIN)
        .args(["admin", "check", "--data-dir"])
        .arg(&server.data)
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("indexes: ok"));
}

/// These guards survive schema changes; only the previous-schema fixture
/// below needs to move when the stored format changes.
#[tokio::test]
async fn an_upgraded_database_serves_only_after_a_successful_check() {
    let mut w = World::new().await;
    w.ticket("Check upgrade").await;
    w.server.stop();
    let data = w.server.data.clone();
    let db = data.join("branvil.redb");
    as_schema_24(&db);
    let admin = |what: &str| {
        std::process::Command::new(BIN)
            .args(["admin", what, "--data-dir"])
            .arg(&data)
            .output()
            .unwrap()
    };
    let unchecked = "has not passed `branvil admin check` since";
    let out = admin("upgrade");
    assert_eq!(out.status.code(), Some(3), "{}", text(&out));
    let out = admin("upgrade");
    assert_eq!(out.status.code(), Some(3), "{}", text(&out));
    assert!(text(&out).contains(unchecked), "{}", text(&out));
    async fn refuses_to_serve(data: &std::path::Path) {
        let mut serve = tokio::process::Command::new(BIN);
        serve
            .args(["serve", "--listen", "127.0.0.1:0", "--data-dir"])
            .arg(data)
            .kill_on_drop(true);
        let out = tokio::time::timeout(std::time::Duration::from_secs(20), serve.output())
            .await
            .expect("serve did not refuse an unchecked database")
            .unwrap();
        assert!(!out.status.success(), "{}", text(&out));
        assert!(
            text(&out).contains("has not passed `branvil admin check` since"),
            "{}",
            text(&out)
        );
    }
    refuses_to_serve(&data).await;
    {
        let database = redb::Database::open(&db).unwrap();
        let txn = database.begin_write().unwrap();
        {
            let mut cards = txn
                .open_table(redb::TableDefinition::<&[u8], &[u8]>::new("ix_cards"))
                .unwrap();
            let key = redb::ReadableTable::iter(&cards)
                .unwrap()
                .next()
                .unwrap()
                .unwrap()
                .0
                .value()
                .to_vec();
            cards.remove(key.as_slice()).unwrap();
        }
        txn.commit().unwrap();
    }
    let out = admin("check");
    assert_eq!(out.status.code(), Some(1), "{}", text(&out));
    assert!(text(&out).contains("index problem"), "{}", text(&out));
    let out = admin("upgrade");
    assert_eq!(out.status.code(), Some(3), "{}", text(&out));
    assert!(text(&out).contains(unchecked), "{}", text(&out));
    refuses_to_serve(&data).await;
    let out = admin("reindex");
    assert!(out.status.success(), "{}", text(&out));
    let out = admin("upgrade");
    assert_eq!(
        out.status.code(),
        Some(3),
        "reindex must not mark the database checked: {}",
        text(&out)
    );
    refuses_to_serve(&data).await;
    let out = admin("check");
    assert!(out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("indexes: ok"), "{}", text(&out));
    let checked = tables(&db);
    let out = admin("upgrade");
    assert_eq!(out.status.code(), Some(0), "{}", text(&out));
    assert!(
        text(&out).contains("already has schema version"),
        "{}",
        text(&out)
    );
    assert_eq!(tables(&db), checked);
    w.restart(&[]);
    assert_eq!(w.server.ok("/me", &w.owner).await["name"], "alice");
}

/// How long `serve` lets requests in flight finish after a signal
/// (`SHUTDOWN_GRACE` in src/cli.rs).
#[cfg(unix)]
const GRACE: std::time::Duration = std::time::Duration::from_secs(5);
#[cfg(unix)]
const MARGIN: std::time::Duration = std::time::Duration::from_secs(3);

#[cfg(unix)]
#[tokio::test]
async fn serve_ends_open_event_streams_and_exits_on_sigterm() {
    let dir = tempfile::tempdir().unwrap();
    let owner = init(dir.path(), "alice");
    let mut server = Server::start(dir.path());
    let mut events = server
        .http
        .get(format!("{}/api/v1/events", server.url))
        .bearer_auth(&owner)
        .send()
        .await
        .unwrap();
    assert_eq!(events.status(), 200);
    let (status, took) = server
        .terminate(GRACE + MARGIN)
        .expect("serve still running after SIGTERM");
    assert!(status.success(), "{status}");
    // The stream ended by itself; the grace period was not needed.
    assert!(took < GRACE, "took {took:?}");
    let end = tokio::time::timeout(MARGIN, async {
        while let Ok(Some(_)) = events.chunk().await {}
    })
    .await;
    assert!(end.is_ok(), "the event stream did not end");
}

#[cfg(unix)]
#[tokio::test]
async fn serve_exits_after_the_grace_period_when_a_request_never_finishes() {
    use std::io::Write;
    let dir = tempfile::tempdir().unwrap();
    let owner = init(dir.path(), "alice");
    let mut server = Server::start(dir.path());
    let host = server.url.strip_prefix("http://").unwrap().to_string();
    // A request whose body never arrives in full.
    let mut stalled = std::net::TcpStream::connect(&host).unwrap();
    write!(
        stalled,
        "POST /api/v1/repos HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {owner}\r\n\
         Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{{\"pa"
    )
    .unwrap();
    stalled.flush().unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    let (status, took) = server
        .terminate(GRACE + MARGIN)
        .expect("serve still running after the grace period");
    assert!(status.success(), "{status}");
    assert!(
        took >= GRACE,
        "took {took:?}: the request was not in flight"
    );
}
