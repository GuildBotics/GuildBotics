//! Backup of a running service and restore into another directory: the
//! restored service has the same Git refs, tickets, pull requests, reviews,
//! threads, and references between them; members' upstream tokens are not
//! carried.

use crate::common;

use std::path::Path;

use sha2::Digest;

use common::*;
use reqwest::StatusCode;
use serde_json::{Value, json};

fn text(out: &std::process::Output) -> String {
    format!(
        "{}{}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    )
}

/// Every file under `dir` contains no `needle`.
pub(crate) fn absent_everywhere(dir: &Path, needle: &str) {
    for entry in std::fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            absent_everywhere(&path, needle);
        } else {
            let bytes = std::fs::read(&path).unwrap();
            assert!(
                !bytes.windows(needle.len()).any(|w| w == needle.as_bytes()),
                "{} contains the credential",
                path.display()
            );
        }
    }
}

/// Copies the backup at `from` (as `manifest` lists it) to `to`.
fn copy_backup(from: &Path, to: &Path, manifest: &Value) {
    for f in manifest["files"].as_array().unwrap() {
        let rel = f["path"].as_str().unwrap();
        std::fs::create_dir_all(to.join(rel).parent().unwrap()).unwrap();
        std::fs::copy(from.join(rel), to.join(rel)).unwrap();
    }
    std::fs::copy(from.join("manifest.json"), to.join("manifest.json")).unwrap();
}

fn ls_remote(server: &Server, dir: &Path, token: &str) -> String {
    git_as_ok(dir, "alice", token, &["ls-remote", &server.remote(REPO)])
}

#[tokio::test(flavor = "multi_thread")]
async fn a_backup_restores_into_another_directory_with_everything_linked() {
    let mut w = World::new().await;
    let gh = common::github::FakeGitHub::start(&w.dir.path().join("github")).await;
    gh.repository("octo/demo", &w.work);
    let secret = "gh-token-7d1f";
    gh.account(secret, "alice-gh");
    let key = common::github::AppKey::generate();
    gh.app("4242", "777", "octo-app", &key);
    gh.configure(&w.server.data);
    w.restart(&[]);
    let s = &w.server;

    // Data of every kind: a ticket on the board, a pull request that closes
    // it, a review, a conversation comment, an inline thread with a reply,
    // and an upstream read with the owner's account and its token.
    let t = w.ticket("Back me up").await;
    let one = format!("/repos/{}/tickets/1", encode(REPO));
    let (st, _) = s
        .put(
            &format!("{one}/board"),
            &w.aiko,
            json!({"lane": "in_progress"}),
        )
        .await;
    assert_eq!(st, 200);
    let head = w.push_change("ticket/1", "f.txt", "one\n");
    let p = w
        .open_pull("ticket/1", json!([{"url": t["url"], "closes": true}]))
        .await;
    let pull = format!("{}/pulls/{}", w.repo_path(), p["number"]);
    let (st, review) = s
        .post(
            &format!("{pull}/reviews"),
            &w.ken,
            json!({"verdict": "approve", "body": "fine", "commit": head}),
        )
        .await;
    assert_eq!(st, 201, "{review}");
    let (st, _) = s
        .post(
            &format!("{pull}/comments"),
            &w.aiko,
            json!({"body": "ready"}),
        )
        .await;
    assert_eq!(st, 201);
    let (st, thread) = s
        .post(
            &format!("{pull}/inline-comments"),
            &w.ken,
            json!({"body": "here", "commit": head, "path": "f.txt", "line": 1, "side": "RIGHT"}),
        )
        .await;
    assert_eq!(st, 201, "{thread}");
    let (st, _) = s
        .post(
            &format!("{pull}/threads/{}/replies", thread["id"].as_str().unwrap()),
            &w.aiko,
            json!({"body": "done"}),
        )
        .await;
    assert_eq!(st, 201);
    let (st, v) = s
        .put(
            "/members/alice/accounts/github",
            &w.owner,
            json!({"token": secret}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    let (st, v) = s
        .put(
            "/members/aiko/accounts/github",
            &w.owner,
            json!({"app": {"app_id": "4242", "installation_id": "777", "private_key": key.pem}}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    let (st, v) = s
        .put(
            &format!("{}/upstream", w.repo_path()),
            &w.owner,
            json!({"provider": "github", "repository": "octo/demo"}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    let webhook = "hook-5e7a";
    let (st, v) = s
        .put(
            "/admin/webhook",
            &w.owner,
            json!({"url": format!("https://hooks.example.test/services/{webhook}")}),
        )
        .await;
    assert_eq!(st, 200, "{v}");

    // Only the owner backs up; the target must be new and absolute.
    let out_dir = tempfile::tempdir().unwrap();
    let backup = out_dir.path().join("backup");
    let (st, _) = s
        .post("/admin/backup", &w.aiko, json!({"to": backup}))
        .await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let (st, _) = s
        .post("/admin/backup", &w.owner, json!({"to": "relative/dir"}))
        .await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    let (st, _) = s
        .post(
            "/admin/backup",
            &w.owner,
            json!({"to": s.data.join("inside")}),
        )
        .await;
    assert_eq!(st, StatusCode::BAD_REQUEST);

    let (cookie, _) = s.session(&w.owner).await;
    let out = s.cli(&w.owner, &["backup", "--to", backup.to_str().unwrap()]);
    assert!(out.status.success(), "{}", text(&out));
    assert!(
        text(&out).contains("the github credential of alice"),
        "{}",
        text(&out)
    );
    let manifest: Value =
        serde_json::from_str(&std::fs::read_to_string(backup.join("manifest.json")).unwrap())
            .unwrap();
    assert_eq!(manifest["format"], 1);
    assert_eq!(manifest["repositories"][0]["path"], REPO);
    assert_eq!(
        manifest["account_tokens_excluded"],
        json!([{"member": "aiko", "provider": "github"}, {"member": "alice", "provider": "github"}])
    );
    assert!(manifest["tables"]["tickets"].as_u64().unwrap() >= 1);
    assert!(!backup.join("secrets").exists());
    absent_everywhere(&backup, secret);
    absent_everywhere(&backup, key.pem.lines().nth(5).unwrap());
    for token in gh.installation_tokens() {
        absent_everywhere(&backup, &token);
    }
    absent_everywhere(&backup, &w.owner);
    absent_everywhere(&backup, webhook);
    // A second backup into the same directory is refused.
    let again = s.cli(&w.owner, &["backup", "--to", backup.to_str().unwrap()]);
    assert!(!again.status.success());

    // The original keeps working after the backup.
    let (st, _) = s
        .post(
            &format!("{one}/comments"),
            &w.aiko,
            json!({"body": "after"}),
        )
        .await;
    assert_eq!(st, 201);

    // A damaged copy is refused before anything is written.
    let damaged = out_dir.path().join("damaged");
    copy_backup(&backup, &damaged, &manifest);
    let bundle = damaged.join(manifest["repositories"][0]["bundle"].as_str().unwrap());
    let mut bytes = std::fs::read(&bundle).unwrap();
    let last = bytes.len() - 1;
    bytes[last] ^= 0xff;
    std::fs::write(&bundle, bytes).unwrap();
    let target = tempfile::tempdir().unwrap();
    let out = std::process::Command::new(BIN)
        .args(["restore", "--from"])
        .arg(&damaged)
        .arg("--data-dir")
        .arg(target.path().join("data"))
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(text(&out).contains("checksum"), "{}", text(&out));
    assert!(!target.path().join("data").exists());

    // A backup older than the previous schema version is refused.
    let mut other = manifest.clone();
    other["schema_version"] = json!(manifest["schema_version"].as_u64().unwrap() - 2);
    std::fs::write(
        damaged.join("manifest.json"),
        serde_json::to_string(&other).unwrap(),
    )
    .unwrap();
    std::fs::copy(
        backup.join(manifest["repositories"][0]["bundle"].as_str().unwrap()),
        &bundle,
    )
    .unwrap();
    let out = std::process::Command::new(BIN)
        .args(["restore", "--from"])
        .arg(&damaged)
        .arg("--data-dir")
        .arg(target.path().join("data"))
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(text(&out).contains("schema version"), "{}", text(&out));
    assert!(!target.path().join("data").exists());

    // A backup of this schema version restores as it is.
    let current = out_dir.path().join("current");
    let out = std::process::Command::new(BIN)
        .args(["restore", "--from"])
        .arg(&backup)
        .arg("--data-dir")
        .arg(&current)
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", text(&out));
    assert!(!text(&out).contains("upgraded"), "{}", text(&out));
    let serves = format!(
        "restored into {0}; `branvil serve --data-dir {0}` starts it",
        current.display()
    );
    assert!(text(&out).contains(&serves), "{}", text(&out));

    // A backup of the previous schema version: what version 24 wrote.
    let previous = out_dir.path().join("previous");
    copy_backup(&backup, &previous, &manifest);
    let db = previous.join("branvil.redb");
    as_schema_24(&db);
    let bytes = std::fs::read(&db).unwrap();
    let mut old = manifest.clone();
    old["schema_version"] = json!(24);
    for f in old["files"].as_array_mut().unwrap() {
        if f["path"] == "branvil.redb" {
            f["bytes"] = json!(bytes.len());
            f["sha256"] = json!(
                sha2::Sha256::digest(&bytes)
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect::<String>()
            );
        }
    }
    std::fs::write(
        previous.join("manifest.json"),
        serde_json::to_string(&old).unwrap(),
    )
    .unwrap();

    // Restore it into another directory (upgraded on the way), then check it
    // offline; everything below compares the restored data with the original.
    let out = std::process::Command::new(BIN)
        .args(["restore", "--from"])
        .arg(&previous)
        .arg("--data-dir")
        .arg(target.path().join("data"))
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", text(&out));
    assert!(
        text(&out).contains("warning: the github account of alice"),
        "{}",
        text(&out)
    );
    assert!(
        text(&out).contains("from schema version 24 to 25"),
        "{}",
        text(&out)
    );
    assert!(
        text(&out).contains("pull request(s) upgraded to have their host checks read again"),
        "{}",
        text(&out)
    );
    // Upgraded on the way: it has to pass its check before it serves.
    let checks_first = format!(
        "restored into {0}; `branvil admin check --data-dir {0}` has to pass before `branvil serve --data-dir {0}` starts it",
        target.path().join("data").display()
    );
    assert!(text(&out).contains(&checks_first), "{}", text(&out));
    // The restore upgraded it: it needs its check before it serves.
    let admin = |what: &str| {
        std::process::Command::new(BIN)
            .args(["admin", what, "--data-dir"])
            .arg(target.path().join("data"))
            .output()
            .unwrap()
    };
    let out = admin("upgrade");
    assert_eq!(out.status.code(), Some(3), "{}", text(&out));
    let out = admin("check");
    assert!(out.status.success(), "{}", text(&out));
    assert!(text(&out).contains("indexes: ok"), "{}", text(&out));
    let out = admin("upgrade");
    assert_eq!(out.status.code(), Some(0), "{}", text(&out));

    // Replace the original by the restored copy on the same base URL (URLs
    // recorded anywhere stay valid), and compare.
    let clones = w.dir.path().join("compare");
    std::fs::create_dir_all(&clones).unwrap();
    let original_refs = ls_remote(s, &clones, &w.owner);
    let listen = s.url.trim_start_matches("http://").to_string();
    w.server.stop();
    let restored = Server::start_on(target.path(), &listen, &[]);
    let r = &restored;
    assert_eq!(r.url, w.server.url);
    // The backup has no owner token file: the first start mints a new owner
    // token, and the previous one and every GUI session end.
    let owner = owner_token(&r.data);
    assert_ne!(owner, w.owner);
    assert_eq!(r.get("/me", &w.owner).await.0, StatusCode::UNAUTHORIZED);
    let resp = r
        .http
        .get(format!("{}/api/v1/me", r.url))
        .header("cookie", &cookie)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(ls_remote(r, &clones, &owner), original_refs);
    // The same tokens work (only their hashes were stored).
    let ticket = r.ok(&one, &w.aiko).await;
    assert_eq!(ticket["title"], "Back me up");
    assert_eq!(ticket["lane"], "in_progress");
    let got = r.ok(&pull, &w.aiko).await;
    assert_eq!(got["head_sha"], head.as_str());
    assert_eq!(got["tickets"][0]["url"], t["url"]);
    let reviews = r.ok(&format!("{pull}/reviews"), &w.aiko).await;
    assert_eq!(reviews["items"][0]["verdict"], "approve");
    assert_eq!(reviews["items"][0]["id"], review["id"]);
    let comments = r.ok(&format!("{pull}/comments"), &w.aiko).await;
    assert_eq!(comments["items"][0]["body"], "ready");
    let threads = r.ok(&format!("{pull}/threads"), &w.aiko).await;
    assert_eq!(threads["items"][0]["comments"].as_array().unwrap().len(), 2);
    let linked = r.ok(&format!("{one}/pulls"), &w.aiko).await;
    assert_eq!(linked["items"][0]["url"], p["url"]);
    assert_eq!(linked["items"][0]["closes"], true);
    let cards = r
        .ok(
            &format!("/workspaces/{WS}/board/cards?lane=in_progress"),
            &w.aiko,
        )
        .await;
    assert_eq!(cards["items"][0]["key"], "1");
    // The ticket comment made after the backup is not there.
    let tc = r.ok(&format!("{one}/comments"), &w.aiko).await;
    assert!(tc["items"].as_array().unwrap().is_empty());
    // The account and the upstream are there; the token must be set again.
    let members = r.ok("/members?limit=100", &owner).await;
    assert!(members.to_string().contains("alice-gh"), "{members}");
    assert_eq!(r.ok(&w.repo_path(), &owner).await["auto_land"], false);
    let (st, v) = r
        .post(
            &format!("/repos/{}/upstream/refresh", encode(REPO)),
            &owner,
            json!({}),
        )
        .await;
    assert_eq!(st, 200, "{v}");
    assert!(
        v["upstream"]["following"]["error"]
            .as_str()
            .unwrap_or_default()
            .contains("not on this machine"),
        "{v}"
    );
    // A push to the restored service works and is recorded.
    git_ok(
        &w.work,
        &["remote", "set-url", "origin", &restored.remote(REPO)],
    );
    let tip = w.push_change("ticket/1", "f.txt", "one\ntwo\n");
    let got = r.ok(&pull, &w.aiko).await;
    assert_eq!(got["head_sha"], tip.as_str());
}
