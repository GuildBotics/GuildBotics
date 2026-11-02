//! Git over Smart HTTP against a real service: clone, fetch, push, Basic auth,
//! member kinds, protected branches, and Branvil-owned hooks.

use crate::common;

use common::*;
use serde_json::json;

#[tokio::test]
async fn clone_fetch_and_push_need_the_members_token() {
    let w = World::new().await;
    let base = w.dir.path().join("clones");
    std::fs::create_dir_all(&base).unwrap();
    let remote = w.server.remote(REPO);

    // An agent can clone; the initial main is there.
    git_as_ok(&base, "ken", &w.ken, &["clone", "-q", &remote, "ken"]);
    assert!(base.join("ken/README.md").exists());

    // No credentials, a wrong token, or another member's name are refused.
    assert!(
        !git(&base, &["clone", "-q", &remote, "anon"])
            .status
            .success()
    );
    assert!(
        !git_as(
            &base,
            "ken",
            "bvt_nope_nope",
            &["clone", "-q", &remote, "bad"]
        )
        .status
        .success()
    );
    assert!(
        !git_as(&base, "aiko", &w.ken, &["clone", "-q", &remote, "mismatch"])
            .status
            .success()
    );
    // A runner uses no Git.
    let runner = w.server.member(&w.owner, "runner1", "runner").await;
    assert!(
        !git_as(
            &base,
            "runner1",
            &runner,
            &["clone", "-q", &remote, "runner"]
        )
        .status
        .success()
    );

    // An agent pushes a feature branch.
    let tip = w.push_change("feature", "f.txt", "one");
    let (_, branch) = w
        .server
        .get(&format!("{}/branches/feature", w.repo_path()), &w.aiko)
        .await;
    assert_eq!(branch["sha"], tip.as_str());

    // Fetch sees the pushed branch.
    let ken = base.join("ken");
    git_as_ok(&ken, "ken", &w.ken, &["fetch", "-q", "origin"]);
    assert_eq!(git_ok(&ken, &["rev-parse", "origin/feature"]), tip);
}

#[tokio::test]
async fn protected_branches_and_internal_refs_refuse_pushes() {
    let w = World::new().await;
    let work = &w.work;
    std::fs::write(work.join("direct.txt"), "d").unwrap();
    git_ok(work, &["add", "direct.txt"]);
    git_ok(work, &["commit", "-qm", "direct"]);
    // Direct push to the default branch, even by the owner.
    let out = git_as(work, "alice", &w.owner, &["push", "origin", "HEAD:main"]);
    assert!(!out.status.success());
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("protected"),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    // Deletion and force push of main.
    assert!(
        !git_as(work, "alice", &w.owner, &["push", "origin", ":main"])
            .status
            .success()
    );
    assert!(
        !git_as(
            work,
            "alice",
            &w.owner,
            &["push", "-f", "origin", "HEAD:main"]
        )
        .status
        .success()
    );
    // Internal and unknown refs.
    assert!(
        !git_as(
            work,
            "aiko",
            &w.aiko,
            &["push", "origin", "HEAD:refs/branvil/x"]
        )
        .status
        .success()
    );
    assert!(
        !git_as(
            work,
            "aiko",
            &w.aiko,
            &["push", "origin", "HEAD:refs/pull/1/head"]
        )
        .status
        .success()
    );

    // A configured protected branch is protected too.
    let (_, repo) = w.server.get(&w.repo_path(), &w.owner).await;
    let (st, _) = w
        .server
        .patch(
            &w.repo_path(),
            &w.owner,
            json!({"revision": repo["revision"], "protected_branches": ["release"]}),
        )
        .await;
    assert_eq!(st, 200);
    w.push_change("release-prep", "r.txt", "r");
    assert!(
        !git_as(
            work,
            "aiko",
            &w.aiko,
            &["push", "origin", "HEAD:refs/heads/release"]
        )
        .status
        .success()
    );
    // main is unchanged.
    let (_, main) = w
        .server
        .get(&format!("{}/branches/main", w.repo_path()), &w.owner)
        .await;
    assert_eq!(
        main["sha"].as_str().unwrap(),
        git_ok(work, &["rev-parse", "main~1"])
    );
}

#[tokio::test]
async fn repository_hooks_never_run() {
    let w = World::new().await;
    let bare = w.server.data.join("repos/acme/demo.git");
    let marker = w.dir.path().join("hook-ran");
    let hooks = bare.join("hooks");
    std::fs::create_dir_all(&hooks).unwrap();
    for name in ["pre-receive", "update", "post-receive", "post-update"] {
        let path = hooks.join(name);
        std::fs::write(&path, format!("#!/bin/sh\ntouch '{}'\n", marker.display())).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
    }
    w.push_change("feature", "f.txt", "one");
    assert!(!marker.exists(), "a repository-provided hook ran");
    // The repository points at Branvil's hooks.
    let hooks_path = git_ok(
        &bare,
        &[
            "--git-dir",
            bare.to_str().unwrap(),
            "config",
            "core.hooksPath",
        ],
    );
    assert_eq!(
        std::path::Path::new(&hooks_path),
        w.server.data.join("hooks")
    );
}

/// A push holds a Smart HTTP transfer slot while its hook waits for the
/// service to record it, and recording runs service Git commands: with one
/// slot of each, the push still completes.
#[tokio::test]
async fn a_push_completes_with_one_git_slot_of_each_kind() {
    let dir = tempfile::tempdir().unwrap();
    let owner = init(dir.path(), "alice");
    let config = dir.path().join("data").join("branvil.toml");
    let mut file: toml::Table = std::fs::read_to_string(&config).unwrap().parse().unwrap();
    let limits = file
        .entry("limits")
        .or_insert_with(|| toml::Value::Table(toml::Table::new()))
        .as_table_mut()
        .unwrap();
    limits.insert("git_concurrency".into(), 1.into());
    limits.insert("git_http_concurrency".into(), 1.into());
    limits.insert("git_http_timeout_secs".into(), 20.into());
    std::fs::write(&config, toml::to_string(&file).unwrap()).unwrap();
    let s = Server::start(dir.path());
    s.post("/workspaces", &owner, json!({"name": "acme"})).await;
    let (st, v) = s.post("/repos", &owner, json!({"path": "acme/demo"})).await;
    assert_eq!(st, 201, "{v}");

    let work = dir.path().join("work");
    std::fs::create_dir(&work).unwrap();
    git_ok(&work, &["init", "-q", "-b", "main"]);
    std::fs::write(work.join("a.txt"), "a\n").unwrap();
    git_ok(&work, &["add", "a.txt"]);
    git_ok(&work, &["commit", "-qm", "a"]);
    let remote = format!("{}/git/acme/demo.git", s.url);
    let started = std::time::Instant::now();
    git_as_ok(
        &work,
        "alice",
        &owner,
        &["push", "-q", &remote, "HEAD:refs/heads/feature"],
    );
    assert!(started.elapsed() < std::time::Duration::from_secs(10));
    let branch = s.ok("/repos/acme%2Fdemo/branches/feature", &owner).await;
    assert!(branch["sha"].is_string());
}
