//! Landing is independent for every pull; only its own conditions block it.
use crate::ci::eventually;
use crate::common::*;
use serde_json::{Value, json};
async fn ready(w: &World, n: u64) -> Value {
    eventually("landing pair checked", || async {
        let p = pull_n(w, n).await;
        p["landing"]["head_sha"] == p["head_sha"] && p["landing"]["base_sha"] == p["base_sha"]
    })
    .await;
    pull_n(w, n).await
}
async fn settings(w: &World, fields: Value) {
    let r = w.server.ok(&w.repo_path(), &w.owner).await;
    let mut fields = fields.as_object().unwrap().clone();
    fields.insert("revision".into(), r["revision"].clone());
    let (st, b) = w
        .server
        .patch(&w.repo_path(), &w.owner, Value::Object(fields))
        .await;
    assert_eq!(st, 200, "{b}");
}
async fn try_merge(w: &World, n: u64) -> (reqwest::StatusCode, Value) {
    let p = pull_n(w, n).await;
    w.server
        .post(
            &format!("{}/pulls/{n}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head":p["head_sha"],"expected_base":p["base_sha"]}),
        )
        .await
}
async fn pull_n(w: &World, n: u64) -> Value {
    w.server
        .ok(&format!("{}/pulls/{n}", w.repo_path()), &w.owner)
        .await
}

/// `token` says something of `kind` (`comment`, `review` with a body, or
/// `thread`) on pull request `n`; returns the entry an answer reacts to.
async fn say(w: &World, n: u64, token: &str, kind: &str) -> String {
    let at = format!("{}/pulls/{n}", w.repo_path());
    let head = pull_n(w, n).await["head_sha"].clone();
    let (path, body) = match kind {
        "comment" => ("comments", json!({"body": "Why this way?"})),
        "review" => (
            "reviews",
            json!({"verdict": "approve", "body": "A nit, if you like.", "commit": head}),
        ),
        "thread" => (
            "inline-comments",
            json!({"body": "Here?", "commit": head, "path": format!("{n}.txt"), "line": 1, "side": "RIGHT"}),
        ),
        _ => unreachable!(),
    };
    let (st, v) = w.server.post(&format!("{at}/{path}"), token, body).await;
    assert_eq!(st, 201, "{kind}: {v}");
    v["id"].as_str().unwrap().to_string()
}

/// aiko, the author of pull request `n`, answers the entry `said` `how`:
/// with a reaction, a comment, a review, a new inline comment, or a reply in
/// the thread of `said`.
async fn answer(w: &World, n: u64, said: &str, how: &str) {
    let at = format!("{}/pulls/{n}", w.repo_path());
    let head = pull_n(w, n).await["head_sha"].clone();
    let (path, body) = match how {
        "reaction" => (
            "reactions".to_string(),
            json!({"target_id": said, "reaction": "ack"}),
        ),
        "comment" => ("comments".into(), json!({"body": "Because."})),
        "review" => (
            "reviews".into(),
            json!({"verdict": "comment", "body": "", "commit": head}),
        ),
        "inline" => (
            "inline-comments".into(),
            json!({"body": "Note.", "commit": head, "path": format!("{n}.txt"), "line": 1, "side": "RIGHT"}),
        ),
        "reply" => {
            let activity = w.server.ok(&format!("{at}/activity"), &w.aiko).await;
            let thread = activity["threads"]
                .as_array()
                .unwrap()
                .iter()
                .find(|t| t["comments"][0]["id"] == said)
                .unwrap()["id"]
                .as_str()
                .unwrap()
                .to_string();
            (
                format!("threads/{thread}/replies"),
                json!({"body": "Done."}),
            )
        }
        _ => unreachable!(),
    };
    let (st, v) = w.server.post(&format!("{at}/{path}"), &w.aiko, body).await;
    assert!(st == 200 || st == 201, "{how}: {st} {v}");
}

#[tokio::test]
async fn three_ready_pulls_merge_without_a_queue_and_preserve_reviewed_heads() {
    let w = World::new().await;
    let mut heads = vec![];
    for n in 1..=3 {
        git_ok(&w.work, &["checkout", "-q", "main"]);
        heads.push(w.push_change(&format!("feature{n}"), &format!("{n}.txt"), "feature"));
        w.open_pull(&format!("feature{n}"), json!([])).await;
        assert_eq!(ready(&w, n).await["landing"]["blockers"], json!([]));
    }
    settings(&w, json!({"auto_land":true})).await;
    eventually("all three merge", || async {
        pull_n(&w, 3).await["merged"] == true
    })
    .await;
    for n in 1..=3 {
        let p = pull_n(&w, n).await;
        assert_eq!(p["merged"], true);
        assert_eq!(p["head_sha"], heads[(n - 1) as usize]);
        assert!(p["landing"].is_null());
    }
    let refs = git_as_ok(
        &w.work,
        "alice",
        &w.owner_git,
        &[
            "ls-remote",
            "origin",
            "refs/branvil/gate/*",
            "refs/branvil/failed-gate/*",
        ],
    );
    assert!(refs.trim().is_empty());
}
#[tokio::test]
async fn author_statements_block_merging_until_answered() {
    let w = World::new().await;
    w.push_change("feature", "1.txt", "feature");
    w.open_pull("feature", json!([])).await;
    for (kind, how) in [
        ("comment", "reaction"),
        ("review", "comment"),
        ("thread", "reply"),
    ] {
        let id = say(&w, 1, &w.ken, kind).await;
        let (st, b) = try_merge(&w, 1).await;
        assert_eq!(st, 409, "{b}");
        assert!(
            b["error"]["message"]
                .as_str()
                .unwrap()
                .contains("unanswered")
        );
        answer(&w, 1, &id, how).await;
        assert_eq!(ready(&w, 1).await["landing"]["blockers"], json!([]));
    }
    assert_eq!(try_merge(&w, 1).await.0, 200);
}
#[tokio::test]
async fn approvals_and_checks_are_reported_by_the_shared_condition_function() {
    let w = World::new().await;
    w.push_change("feature", "1.txt", "feature");
    w.open_pull("feature", json!([])).await;
    for (fields, code) in [
        (json!({"required_approvals":1}), "approvals"),
        (
            json!({"required_approvals":0,"required_checks":["test"]}),
            "head_checks",
        ),
    ] {
        settings(&w, fields).await;
        let pull = ready(&w, 1).await;
        let blocker = pull["landing"]["blockers"]
            .as_array()
            .unwrap()
            .iter()
            .find(|b| b["code"] == code)
            .unwrap();
        if code == "approvals" {
            assert_eq!(blocker["count"], 0);
            assert_eq!(blocker["required"], 1);
        } else {
            assert_eq!(blocker["jobs"], json!(["test"]));
        }
        let (st, b) = try_merge(&w, 1).await;
        assert_eq!(st, 409, "{b}");
        assert!(b["error"]["message"].as_str().unwrap().contains(code));
    }
}
#[tokio::test]
async fn head_check_blocker_jobs_only_list_unmet_required_checks() {
    let w = World::new().await;
    let runner = crate::ci::Runner::new(&w, "landing-runner").await;
    settings(
        &w,
        json!({"required_checks": ["failed", "running", "passed"]}),
    )
    .await;
    std::fs::create_dir_all(w.work.join(".branvil")).unwrap();
    w.push_change(
        "feature",
        ".branvil/ci.toml",
        &crate::ci::pipeline(&["failed", "running", "passed"]),
    );
    w.open_pull("feature", json!([])).await;
    for name in ["failed", "running", "passed"] {
        let claim = runner.next_claim().await;
        assert_eq!(claim["job"]["name"], name);
        if name != "running" {
            assert_eq!(
                runner
                    .finish(
                        &claim,
                        if name == "failed" {
                            "failure"
                        } else {
                            "success"
                        }
                    )
                    .await
                    .0,
                200
            );
        }
    }
    let p = ready(&w, 1).await;
    let blocker = p["landing"]["blockers"]
        .as_array()
        .unwrap()
        .iter()
        .find(|b| b["code"] == "head_checks")
        .unwrap();
    assert_eq!(blocker["jobs"], json!(["failed", "running"]));
}

#[tokio::test]
async fn conflicts_notify_once_per_head_and_block_merge() {
    let w = World::new().await;
    w.push_change("feature", "README.md", "feature");
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("advance", "README.md", "base");
    w.open_pull("advance", json!([])).await;
    assert_eq!(try_merge(&w, 1).await.0, 200);
    w.open_pull("feature", json!([])).await;
    eventually("conflict notice", || async {
        pull_n(&w, 2).await["landing"]["told"] == json!(["conflict"])
    })
    .await;
    let p = ready(&w, 2).await;
    assert!(
        p["landing"]["blockers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|b| b["code"] == "conflict" && b["files"] == json!(["README.md"]))
    );
    assert!(!p["unanswered"].as_array().unwrap().is_empty());
    let (st, b) = try_merge(&w, 2).await;
    assert_eq!(st, 409, "{b}");
    assert!(b["error"]["message"].as_str().unwrap().contains("conflict"));
    w.push_change("feature", "2.txt", "new head");
    eventually("second notice", || async {
        w.server
            .ok(&format!("{}/pulls/2/comments", w.repo_path()), &w.owner)
            .await["items"]
            .as_array()
            .unwrap()
            .len()
            == 2
    })
    .await;
}
#[tokio::test]
async fn removed_landing_routes_return_404() {
    let w = World::new().await;
    assert_eq!(
        w.server
            .get(&format!("{}/landing", w.repo_path()), &w.owner)
            .await
            .0,
        404
    );
    assert_eq!(
        w.server
            .post(
                &format!("{}/pulls/1/landing/resume", w.repo_path()),
                &w.owner,
                json!({})
            )
            .await
            .0,
        404
    );
}

#[tokio::test]
async fn upgrade_preserves_landing_conflicts_and_notice_history() {
    let mut w = World::new().await;
    w.push_change("feature", "README.md", "feature");
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("advance", "README.md", "base");
    w.open_pull("advance", json!([])).await;
    assert_eq!(try_merge(&w, 1).await.0, 200);
    w.open_pull("feature", json!([])).await;
    eventually("original conflict notice", || async {
        pull_n(&w, 2).await["landing"]["told"] == json!(["conflict"])
    })
    .await;
    w.server.stop();
    let db = w.server.data.join("branvil.redb");
    as_schema_37(&db);
    let out = branvil_command()
        .args(["admin", "upgrade", "--data-dir"])
        .arg(&w.server.data)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let e = records(&db, "landing")
        .into_iter()
        .find(|e| e["number"] == 2)
        .unwrap();
    assert_eq!(e["conflicts"], json!(["README.md"]));
    assert_eq!(e["told"], json!(["conflict"]));
    let out = branvil_command()
        .args(["admin", "check", "--data-dir"])
        .arg(&w.server.data)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    w.restart(&[]);
    eventually("one migrated conflict notice", || async {
        w.server
            .ok(&format!("{}/pulls/2/comments", w.repo_path()), &w.owner)
            .await["items"]
            .as_array()
            .unwrap()
            .len()
            == 1
    })
    .await;
    assert!(
        ready(&w, 2).await["landing"]["blockers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|b| b["code"] == "conflict")
    );
    w.server.stop();
    as_schema_37(&db);
    let out = branvil_command()
        .args(["admin", "upgrade", "--data-dir"])
        .arg(&w.server.data)
        .output()
        .unwrap();
    assert!(out.status.success());
    let out = branvil_command()
        .args(["admin", "check", "--data-dir"])
        .arg(&w.server.data)
        .output()
        .unwrap();
    assert!(out.status.success());
    w.restart(&[]);
    ready(&w, 2).await;
    assert_eq!(
        w.server
            .ok(&format!("{}/pulls/2/comments", w.repo_path()), &w.owner)
            .await["items"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}
