//! The landing contract across host polling, reviews, and CI adoption.

use crate::common::*;
use serde_json::{Value, json};
use std::time::{Duration, Instant};

async fn pull(w: &World, number: u64) -> Value {
    w.server
        .ok(&format!("{}/pulls/{number}", w.repo_path()), &w.owner)
        .await
}

async fn landing(w: &World, number: u64, state: &str) -> Value {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let p = pull(w, number).await;
        if p["landing"]["next"] == state {
            return p;
        }
        assert!(Instant::now() < deadline, "expected {state}: {p}");
        tokio::time::sleep(Duration::from_millis(30)).await;
    }
}

async fn settings(w: &World, fields: Value) {
    let repo = w.server.ok(&w.repo_path(), &w.owner).await;
    let mut fields = fields.as_object().unwrap().clone();
    fields.insert("revision".into(), repo["revision"].clone());
    let (st, result) = w
        .server
        .patch(&w.repo_path(), &w.owner, Value::Object(fields))
        .await;
    assert_eq!(st, 200, "{result}");
}

async fn merge(w: &World, number: u64) -> Value {
    let p = landing(w, number, "owner_merge").await;
    let (st, result) = w
        .server
        .post(
            &format!("{}/pulls/{number}/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head":p["head_sha"],"expected_base":p["base_sha"]}),
        )
        .await;
    assert_eq!(st, 200, "{result}");
    result
}

#[tokio::test]
async fn an_approval_of_the_original_head_still_lands_after_the_base_advances() {
    let w = World::new().await;
    settings(&w, json!({"required_approvals":1})).await;
    let first_head = w.push_change("first", "first.txt", "first\n");
    w.open_pull("first", json!([])).await;
    git_ok(&w.work, &["checkout", "-q", "main"]);
    let second_head = w.push_change("second", "second.txt", "second\n");
    w.open_pull("second", json!([])).await;
    for (number, head) in [(1, &first_head), (2, &second_head)] {
        assert_eq!(pull(&w, number).await["approvals"], 0);
        let (st, result) = w
            .server
            .post(
                &format!("{}/pulls/{number}/reviews", w.repo_path()),
                &w.ken,
                json!({"verdict":"approve","commit":head}),
            )
            .await;
        assert_eq!(st, 201, "{result}");
    }
    let reviewed = landing(&w, 2, "owner_merge").await;
    assert_eq!(reviewed["approvals"], 1);
    merge(&w, 1).await;
    let gated = landing(&w, 2, "owner_merge").await;
    assert_eq!(gated["head_sha"], second_head);
    assert_eq!(gated["approvals"], 1);
    assert_eq!(gated["review_states"], reviewed["review_states"]);
    let merged = merge(&w, 2).await;
    assert_eq!(merged["head_sha"], second_head);
    assert_eq!(merged["approvals"], 1);
    let reviews = w
        .server
        .ok(&format!("{}/pulls/2/reviews", w.repo_path()), &w.owner)
        .await;
    assert_eq!(reviews["items"].as_array().unwrap().len(), 1);
    assert_eq!(reviews["items"][0]["commit"], second_head);
}

#[tokio::test(flavor = "multi_thread")]
async fn three_ready_pulls_publish_without_waiting_for_a_host_merge() {
    use crate::upstream::{GH, followed, with_aiko_account};
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    for n in 1..=3 {
        git_ok(&w.work, &["checkout", "-q", "main"]);
        w.push_change(&format!("feature{n}"), &format!("{n}.txt"), "feature");
        w.open_pull(&format!("feature{n}"), json!([])).await;
    }
    settings(&w, json!({"auto_land":true})).await;
    crate::ci::eventually("all three published", || async { gh.pulls(GH).len() == 3 }).await;
    for n in 1..=3 {
        let p = pull(&w, n).await;
        assert_eq!(p["landing"]["blockers"], json!([]));
        assert_eq!(p["upstream"]["auto"], true);
    }
    assert!(
        git_as_ok(
            &w.work,
            "alice",
            &w.owner_git,
            &[
                "ls-remote",
                "origin",
                "refs/branvil/gate/*",
                "refs/branvil/failed-gate/*"
            ]
        )
        .trim()
        .is_empty()
    );
}

#[tokio::test]
async fn hold_blocks_local_merge_until_verification_succeeds() {
    use crate::ci::{Runner, eventually, pipeline};
    let w = World::new().await;
    let initial = w.server.ok(&w.repo_path(), &w.owner).await;
    assert_eq!(initial["hold_while_failing"], false);
    assert_eq!(
        w.server
            .patch(
                &w.repo_path(),
                &w.aiko,
                json!({"revision":initial["revision"],"hold_while_failing":true})
            )
            .await
            .0,
        403
    );
    let runner = Runner::new(&w, "hold-runner").await;
    settings(&w, json!({"post_merge_jobs":["verify"]})).await;
    crate::ci::land(
        &w,
        "pipeline",
        &[(".branvil/ci.toml", &pipeline(&["verify"]))],
        Some(&runner),
    )
    .await;
    let last = fail_main(&w, &runner).await;
    let head = w.push_change("held", "feature.txt", "feature");
    w.open_pull("held", json!([])).await;
    runner.drain("success").await;
    landing(&w, 2, "owner_merge").await;
    settings(&w, json!({"hold_while_failing":true})).await;
    let held = landing(&w, 2, "conditions").await;
    assert_eq!(held["landing"]["blockers"][0]["code"], "held");
    assert_eq!(held["landing"]["blockers"][0]["held"]["number"], 1);
    assert!(
        held["landing"]["blockers"][0]["message"]
            .as_str()
            .unwrap()
            .contains("Repair #1")
    );
    let (st, result) = w
        .server
        .post(
            &format!("{}/pulls/2/merge", w.repo_path()),
            &w.owner,
            json!({"expected_head":head,"expected_base":held["base_sha"]}),
        )
        .await;
    assert_eq!(st, 409, "{result}");
    assert_eq!(result["error"]["code"], "not_mergeable");
    settings(&w, json!({"auto_land":true})).await;
    assert_eq!(pull(&w, 2).await["state"], "open");
    let retry = format!(
        "{}/jobs/{}/retry",
        w.repo_path(),
        last["attempt_id"].as_str().unwrap()
    );
    assert_eq!(w.server.post(&retry, &w.owner, json!({})).await.0, 200);
    assert_eq!(pull(&w, 2).await["landing"]["blockers"][0]["code"], "held");
    let c = runner.next_claim().await;
    assert_eq!(runner.finish(&c, "success").await.0, 200);
    eventually("verified main releases held automatic merge", || async {
        pull(&w, 2).await["merged"] == true
    })
    .await;
}

async fn fail_main(w: &World, runner: &crate::ci::Runner) -> Value {
    use crate::ci::eventually;
    let verify_url = format!("{}/verification", w.repo_path());
    let mut last = Value::Null;
    for n in 0..=3 {
        let c = runner.next_claim().await;
        assert_eq!(runner.finish(&c, "failure").await.0, 200);
        eventually("main failure retry", || async {
            let v = w.server.ok(&verify_url, &w.owner).await;
            if n < 3 {
                v["current"]["retry"]["number"] == n + 1
            } else {
                v["state"] == "failing"
            }
        })
        .await;
        last = c;
    }
    eventually("repair created", || async {
        w.server
            .get(&format!("{}/tickets/1", w.repo_path()), &w.owner)
            .await
            .0
            == 200
    })
    .await;
    last
}

#[tokio::test(flavor = "multi_thread")]
async fn held_host_pulls_keep_sending_heads_and_release_unpublished_work() {
    use crate::ci::{Runner, eventually, pipeline};
    use crate::upstream::{GH, followed, refresh, with_aiko_account};
    let (w, gh) = followed().await;
    with_aiko_account(&w).await;
    let runner = Runner::new(&w, "hold-host-runner").await;
    std::fs::create_dir_all(w.work.join(".branvil")).unwrap();
    std::fs::write(w.work.join(".branvil/ci.toml"), pipeline(&["verify"])).unwrap();
    git_ok(&w.work, &["add", ".branvil/ci.toml"]);
    git_ok(&w.work, &["commit", "-qm", "main verification"]);
    git_ok(
        &w.work,
        &["push", "-q", gh.dir(GH).to_str().unwrap(), "main"],
    );
    refresh(&w).await;
    let last = fail_main(&w, &runner).await;
    w.push_change("published", "published.txt", "first");
    w.open_pull("published", json!([])).await;
    runner.drain("success").await;
    settings(&w, json!({"auto_land":true})).await;
    eventually("default permits publication while main fails", || async {
        gh.pulls(GH).len() == 1
    })
    .await;
    settings(&w, json!({"hold_while_failing":true})).await;
    let p = landing(&w, 1, "conditions").await;
    assert_eq!(p["landing"]["blockers"][0]["code"], "held");
    assert!(!p["landing"]["host_url"].is_null());
    let head = w.push_change("published", "published.txt", "second");
    runner.drain("success").await;
    refresh(&w).await;
    assert_eq!(
        gh.tip(GH, &gh.pulls(GH)[0].head).as_deref(),
        Some(head.as_str())
    );
    assert_eq!(pull(&w, 1).await["landing"]["next"], "conditions");
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("unpublished", "unpublished.txt", "waiting");
    w.open_pull("unpublished", json!([])).await;
    runner.drain("success").await;
    assert_eq!(
        landing(&w, 2, "conditions").await["landing"]["blockers"][0]["code"],
        "held"
    );
    assert_eq!(gh.pulls(GH).len(), 1);
    git_ok(&w.work, &["checkout", "-q", "main"]);
    w.push_change("repair", "repair.txt", "fix");
    w.open_pull(
        "repair",
        json!([{"url":format!("{}/{}/-/tickets/1",w.server.url,REPO),"closes":false}]),
    )
    .await;
    runner.drain("success").await;
    eventually("repair PR publishes while main fails", || async {
        gh.pulls(GH).len() == 2
    })
    .await;
    let retry = format!(
        "{}/jobs/{}/retry",
        w.repo_path(),
        last["attempt_id"].as_str().unwrap()
    );
    assert_eq!(w.server.post(&retry, &w.owner, json!({})).await.0, 200);
    let c = runner.next_claim().await;
    assert_eq!(runner.finish(&c, "success").await.0, 200);
    eventually("verification event releases unpublished PR", || async {
        gh.pulls(GH).len() == 3
    })
    .await;
}
