//! Releases over HTTP and Git: who may make one, and that none is pushed.
use crate::common::*;
use serde_json::json;

#[tokio::test]
async fn only_the_owner_releases_and_release_tags_are_never_pushed() {
    let w = World::new().await;
    let main = git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["ls-remote", "origin", "refs/heads/main"],
    );
    let main = main.split_whitespace().next().unwrap().to_owned();
    let feature = w.push_change("feature", "a.txt", "a");
    let path = format!("{}/releases", w.repo_path());
    let view = w.server.ok(&path, &w.aiko).await;
    assert_eq!(view["tag_prefix"], "release/");
    assert_eq!(view["releases"], json!([]));
    assert!(view["next_name"].as_str().unwrap().starts_with("release/"));

    let body = json!({"sha": main});
    let (status, e) = w.server.post(&path, &w.aiko, body.clone()).await;
    assert_eq!(status, 403, "{e}");
    // A main without a pipeline passes its verification, and has no release jobs.
    let (status, made) = w.server.post(&path, &w.owner, body.clone()).await;
    assert_eq!(status, 200, "{made}");
    let tag = made["releases"][0]["name"].as_str().unwrap().to_owned();
    assert_eq!(tag, view["next_name"].as_str().unwrap());
    assert_eq!(made["releases"][0]["sha"], main.as_str());
    assert_eq!(made["points"], json!([]));
    let tags = git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["ls-remote", "origin", "refs/tags/*"],
    );
    assert_eq!(tags.trim(), format!("{main}\trefs/tags/{tag}"));
    let (status, e) = w.server.post(&path, &w.owner, body).await;
    assert_eq!(status, 409, "{e}");
    assert_eq!(e["error"]["code"], "release_not_newer");
    let (status, e) = w
        .server
        .post(&path, &w.owner, json!({"sha": feature}))
        .await;
    assert_eq!(status, 409, "{e}");
    assert_eq!(e["error"]["code"], "release_not_newer");
    let (status, e) = w.server.post(&path, &w.owner, json!({"sha": "main"})).await;
    assert_eq!(status, 400, "{e}");

    // Not even the owner pushes a release tag; other tags keep their rules.
    for (member, token) in [("alice", &w.owner_git), ("aiko", &w.aiko)] {
        let out = git_as(
            &w.work,
            member,
            token,
            &["push", "-q", "origin", "HEAD:refs/tags/release/by-hand"],
        );
        assert!(!out.status.success(), "{member}");
        let said = String::from_utf8_lossy(&out.stderr);
        assert!(said.contains("is a release tag"), "{said}");
    }
    git_as_ok(
        &w.work,
        "aiko",
        &w.aiko,
        &["push", "-q", "origin", "HEAD:refs/tags/v1"],
    );
}

#[tokio::test]
async fn a_release_point_a_runner_reports_holds_back_newer_commits() {
    use crate::ci::{Runner, eventually, pipeline};
    let w = World::new().await;
    let path = "acme/points";
    let created = w
        .server
        .post(
            "/repos",
            &w.owner,
            json!({"path": path, "default_branch": "main", "labels": []}),
        )
        .await;
    assert_eq!(created.0, 201, "{}", created.1);
    let work = w.dir.path().join("points");
    std::fs::create_dir(&work).unwrap();
    git_ok(&work, &["init", "-q", "-b", "main"]);
    std::fs::write(work.join(".guildbotics-ci.toml"), pipeline(&["schema"])).unwrap();
    git_ok(&work, &["add", ".guildbotics-ci.toml"]);
    git_ok(&work, &["commit", "-qm", "raise"]);
    let raised = git_ok(&work, &["rev-parse", "HEAD"]).trim().to_owned();
    let runner = Runner::new(&w, "points-runner").await;
    git_as_ok(
        &work,
        "alice",
        &w.owner_git,
        &["push", "-q", &w.server.remote(path), "main"],
    );
    let c = runner.next_claim().await;
    let (status, finished) = runner
        .post(
            &c,
            "finish",
            json!({"lease": c["lease"], "conclusion": "success",
                "image_digest": crate::ci::DIGEST,
                "release_points": ["storage format 47 → 48"]}),
        )
        .await;
    assert_eq!(status, 200, "{finished}");
    let releases = format!("/repos/{}/releases", encode(path));
    eventually("the verified release point", || async {
        w.server.ok(&releases, &w.owner).await["points"]
            == json!([{"sha": raised, "notes": [{"job": "schema",
                "attempt_id": c["attempt_id"], "message": "storage format 47 → 48"}]}])
    })
    .await;
    let (status, made) = w
        .server
        .post(&releases, &w.owner, json!({"sha": raised}))
        .await;
    assert_eq!(status, 200, "{made}");
    assert_eq!(made["points"], json!([]));
}
